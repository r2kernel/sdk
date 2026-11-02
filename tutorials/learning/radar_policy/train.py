#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


TUTORIAL_ROOT = Path(__file__).resolve().parents[2]
LIB = TUTORIAL_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from learned_radar_policy import train_behavior_clone  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the cleaner learned-radar behavior-cloning policy.")
    parser.add_argument("--output", required=True, help="Destination JSON model artifact.")
    parser.add_argument("--samples", type=int, default=30_000)
    parser.add_argument("--epochs", type=int, default=120)
    parser.add_argument("--hidden-size", type=int, default=48)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--learning-rate", type=float, default=0.003)
    parser.add_argument("--seed", type=int, default=20260906)
    args = parser.parse_args()
    model, metrics = train_behavior_clone(
        sample_count=args.samples,
        epochs=args.epochs,
        hidden_size=args.hidden_size,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        seed=args.seed,
    )
    model.save(args.output)
    print(json.dumps({"ok": True, "output": str(Path(args.output).resolve()), "metrics": metrics}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
