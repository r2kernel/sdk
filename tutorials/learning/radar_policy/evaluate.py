#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


TUTORIAL_ROOT = Path(__file__).resolve().parents[2]
for path in (TUTORIAL_ROOT / "lib", TUTORIAL_ROOT / "simulator"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from arena import ArenaSimulator  # noqa: E402
from common import trash_layout  # noqa: E402
from learned_radar_policy import RadarPolicyModel, feature_vector, select_radar_observation  # noqa: E402
from navigation_math import radar_detection_in_body  # noqa: E402
from radar_mount import load_radar_mount  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.value = 1_000_000_000

    def now_ns(self) -> int:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += int(seconds * 1_000_000_000)


def eligible_seeds(first_seed: int, episodes: int) -> list[int]:
    seeds: list[int] = []
    seed = first_seed
    while len(seeds) < episodes:
        target = trash_layout(seed=seed, count=1)[0]
        if math.hypot(float(target["x"]) - 0.18, float(target["y"]) - 0.50) > 0.31:
            seeds.append(seed)
        seed += 1
    return seeds


def run_episode(model: RadarPolicyModel, *, seed: int, max_steps: int, dt: float) -> dict[str, object]:
    clock = FakeClock()
    arena = ArenaSimulator(now_ns=clock.now_ns)
    mount = load_radar_mount(TUTORIAL_ROOT / "subsystems/compound.cleaner/subsystem.yaml")
    body_radar_transform = {
        "res": "ok",
        "translation": [mount.translation_x_m, mount.translation_y_m, 0.0],
        "rotation_xyzw": [0.0, 0.0, math.sin(mount.yaw_rad / 2.0), math.cos(mount.yaw_rad / 2.0)],
    }
    arena.reset({"seed": seed, "count": 1})
    first_detection_step = None
    reached_step = None
    active_target_id = None
    waiting_sample_steps = 0
    for step in range(max_steps):
        scan = arena.scan({"mode": "radar", "max_range": 0.30, "fov_deg": 30.0, "visualize": True})
        detection, active_target_id = select_radar_observation(scan.get("detections") or [], active_target_id=active_target_id)
        if detection is None and active_target_id is not None:
            # A held target with no current coordinates is an availability gap,
            # not evidence that the target is gone or a model exploration input.
            arena.apply_drive({"vx": 0.0, "wz": 0.0, "dt": min(dt * 2.2, 0.50)})
            waiting_sample_steps += 1
            clock.advance(dt)
            continue
        if detection is not None:
            detection = radar_detection_in_body(detection, source_frame_id="cleaner_radar",
                                                 body_radar_transform=body_radar_transform)
        if detection is not None and first_detection_step is None:
            first_detection_step = step
        state = arena.get_state({})
        robot = state["robot"]
        phase = (step * dt / 32.0) * math.tau
        features = feature_vector(
            x=float(robot["x"]),
            y=float(robot["y"]),
            heading=float(robot["heading"]),
            phase=phase,
            detection=detection,
            scan_range=0.30,
        )
        action = model.predict(features)
        if detection is not None and float(detection["distance"]) <= 0.18 and abs(float(detection["bearing_deg"])) <= 7.0:
            reached_step = step
            break
        # Keep the command valid across the next policy tick. The simulator
        # intentionally expires a command at its exact TTL boundary.
        arena.apply_drive({"vx": float(action[0]), "wz": float(action[1]), "dt": min(dt * 2.2, 0.50)})
        clock.advance(dt)
    return {
        "seed": seed,
        "initially_out_of_range": True,
        "discovered": first_detection_step is not None,
        "reached": reached_step is not None,
        "discovery_s": None if first_detection_step is None else round(first_detection_step * dt, 3),
        "reach_s": None if reached_step is None else round(reached_step * dt, 3),
        "waiting_sample_steps": waiting_sample_steps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the learned radar policy in the tutorial arena.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--episodes", type=int, default=12)
    parser.add_argument("--first-seed", type=int, default=100)
    parser.add_argument("--max-steps", type=int, default=750)
    parser.add_argument("--dt", type=float, default=0.12)
    parser.add_argument("--require-success-rate", type=float, default=0.75)
    parser.add_argument("--output")
    args = parser.parse_args()
    model = RadarPolicyModel.load(args.model)
    episodes = [
        run_episode(model, seed=seed, max_steps=args.max_steps, dt=args.dt)
        for seed in eligible_seeds(args.first_seed, args.episodes)
    ]
    success_rate = sum(bool(item["reached"]) for item in episodes) / max(len(episodes), 1)
    discovery_rate = sum(bool(item["discovered"]) for item in episodes) / max(len(episodes), 1)
    report = {
        "model": str(Path(args.model).resolve()),
        "episodes": episodes,
        "summary": {
            "episode_count": len(episodes),
            "discovery_rate": discovery_rate,
            "reach_success_rate": success_rate,
            "radar_only_stationary_baseline_success_rate": 0.0,
        },
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if success_rate >= args.require_success_rate else 1


if __name__ == "__main__":
    raise SystemExit(main())
