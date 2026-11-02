from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any


WORLD_TOPIC = "robot_cleaner/world/state"
APP_STATUS_TOPIC = "robot_cleaner/app/status"
WORLD_EVENT_SIZE = 128 * 1024


def robot_cleaner_root(file: str) -> Path:
    for parent in Path(file).resolve().parents:
        if (parent / "subsystems").is_dir() and (parent / "simulator").is_dir():
            return parent
    raise RuntimeError(f"could not resolve cleaner tutorial root from {file}")


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def as_float(params: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def as_bool(params: dict[str, Any], key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on", "close", "closed"}
    return bool(value)


def parse_bool_text(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "start", "started"}:
        return True
    if text in {"0", "false", "no", "off", "stop", "stopped"}:
        return False
    return default


def json_payload(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_json_text(value: dict[str, Any]) -> dict[str, str]:
    return {"value": json.dumps(value, separators=(",", ":"), sort_keys=True)}


def decode_json_text(value: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        return {}, False
    raw = value.get("value")
    if isinstance(raw, str) and set(value.keys()) <= {"value"}:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}, True
        return (decoded if isinstance(decoded, dict) else {}), True
    return dict(value), False


def angle_wrap_radians(value: float) -> float:
    while value > math.pi:
        value -= 2.0 * math.pi
    while value < -math.pi:
        value += 2.0 * math.pi
    return value


def angle_wrap_degrees(value: float) -> float:
    while value > 180.0:
        value -= 360.0
    while value < -180.0:
        value += 360.0
    return value


def trash_layout(seed: int = 7, count: int = 9) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    trash: list[dict[str, Any]] = []
    for index in range(max(count, 0)):
        trash.append(
            {
                "id": f"trash-{index + 1}",
                "x": round(rng.uniform(0.16, 0.88), 3),
                "y": round(rng.uniform(0.16, 0.84), 3),
                "state": "loose",
            }
        )
    return trash


def extract_payload(response: dict[str, Any]) -> Any:
    payload: Any = response
    for _ in range(4):
        if not isinstance(payload, dict):
            return payload
        if "payload_json" in payload:
            payload = payload["payload_json"]
            continue
        if "payload" in payload:
            payload = payload["payload"]
            continue
        if (
            "result" in payload
            and (
                payload.get("kind") == "procedure.response"
                or "request_id" in payload
                or "error_code" in payload
                or "ok" in payload
            )
        ):
            payload = payload["result"]
            continue
        return payload
    return payload


def response_ok(response: dict[str, Any]) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("ok") is False:
        return False
    status = response.get("status")
    return status in (None, 0, "0", "ok", "OK")
