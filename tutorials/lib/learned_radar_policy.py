from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


FEATURE_NAMES = (
    "detected",
    "distance_normalized",
    "bearing_sin",
    "bearing_cos",
    "position_x_centered",
    "position_y_centered",
    "heading_sin",
    "heading_cos",
    "search_phase_sin",
    "search_phase_cos",
    "search_phase_2_sin",
    "search_phase_2_cos",
)
OUTPUT_NAMES = ("linear_velocity", "angular_velocity")
MODEL_SCHEMA = "tutorial.radar_policy.mlp.v1"


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def wrap_radians(value: float) -> float:
    return math.atan2(math.sin(value), math.cos(value))


def select_radar_observation(
    detections: list[dict[str, Any]],
    *,
    active_target_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Keep a retained target's identity, but return coordinates only when fresh."""
    retained = [item for item in detections if isinstance(item, dict) and item.get("id")]
    if active_target_id is not None:
        for item in retained:
            if str(item["id"]) == active_target_id:
                return (item if item.get("fresh") is True else None), active_target_id
    visible = [item for item in retained if item.get("fresh") is True]
    if not visible:
        return None, None
    selected = min(visible, key=lambda item: (float(item["distance"]), abs(float(item["bearing_deg"]))))
    return selected, str(selected["id"])


def feature_vector(
    *,
    x: float,
    y: float,
    heading: float,
    phase: float,
    detection: dict[str, Any] | None,
    scan_range: float,
) -> np.ndarray:
    """Build policy inputs from a body-frame target distance and bearing."""
    detected = detection is not None
    distance = float((detection or {}).get("distance") or scan_range)
    bearing = math.radians(float((detection or {}).get("bearing_deg") or 0.0))
    return np.asarray(
        [
            1.0 if detected else 0.0,
            clamp(distance / max(scan_range, 1e-6), 0.0, 1.25),
            math.sin(bearing) if detected else 0.0,
            math.cos(bearing) if detected else 1.0,
            clamp((float(x) - 0.5) / 0.5, -1.0, 1.0),
            clamp((float(y) - 0.5) / 0.5, -1.0, 1.0),
            math.sin(float(heading)),
            math.cos(float(heading)),
            math.sin(float(phase)),
            math.cos(float(phase)),
            math.sin(float(phase) * 2.0),
            math.cos(float(phase) * 2.0),
        ],
        dtype=np.float64,
    )


def expert_action(features: np.ndarray) -> np.ndarray:
    detected = float(features[0]) > 0.5
    heading = math.atan2(float(features[6]), float(features[7]))
    if detected:
        distance = clamp(float(features[1]) * 0.30, 0.0, 0.375)
        bearing = math.atan2(float(features[2]), float(features[3]))
        angular = clamp(bearing / math.radians(70.0), -0.82, 0.82)
        alignment = max(0.08, math.cos(bearing))
        linear = clamp((distance - 0.145) / 0.20, -0.12, 0.62) * alignment
        return np.asarray([linear, angular], dtype=np.float64)

    x = clamp(float(features[4]) * 0.5 + 0.5, 0.05, 0.95)
    y = clamp(float(features[5]) * 0.5 + 0.5, 0.05, 0.95)
    phase = math.atan2(float(features[8]), float(features[9]))
    waypoint_x = 0.5 + 0.38 * math.sin(phase)
    waypoint_y = 0.5 + 0.34 * math.sin(phase * 2.0)
    desired = math.atan2(waypoint_y - y, waypoint_x - x)
    error = wrap_radians(desired - heading)
    angular = clamp(error / 1.15, -0.72, 0.72)
    linear = 0.50 * max(0.06, math.cos(error))
    return np.asarray([linear, angular], dtype=np.float64)


@dataclass
class RadarPolicyModel:
    weights_1: np.ndarray
    bias_1: np.ndarray
    weights_2: np.ndarray
    bias_2: np.ndarray
    metadata: dict[str, Any]

    def predict(self, features: np.ndarray) -> np.ndarray:
        vector = np.asarray(features, dtype=np.float64)
        if vector.shape != (len(FEATURE_NAMES),):
            raise ValueError(f"expected {len(FEATURE_NAMES)} policy features, got {vector.shape}")
        hidden = np.tanh(vector @ self.weights_1 + self.bias_1)
        action = np.tanh(hidden @ self.weights_2 + self.bias_2)
        if not np.all(np.isfinite(action)):
            raise RuntimeError("policy produced a non-finite action")
        return np.clip(action, -1.0, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MODEL_SCHEMA,
            "features": list(FEATURE_NAMES),
            "outputs": list(OUTPUT_NAMES),
            "architecture": {
                "kind": "mlp",
                "activation": "tanh",
                "input_size": int(self.weights_1.shape[0]),
                "hidden_size": int(self.weights_1.shape[1]),
                "output_size": int(self.weights_2.shape[1]),
            },
            "weights": {
                "layer_1": self.weights_1.tolist(),
                "bias_1": self.bias_1.tolist(),
                "layer_2": self.weights_2.tolist(),
                "bias_2": self.bias_2.tolist(),
            },
            "metadata": self.metadata,
        }

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "RadarPolicyModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema") != MODEL_SCHEMA:
            raise ValueError(f"unsupported radar policy schema: {payload.get('schema')!r}")
        if tuple(payload.get("features") or ()) != FEATURE_NAMES:
            raise ValueError("radar policy feature order does not match the runtime")
        if tuple(payload.get("outputs") or ()) != OUTPUT_NAMES:
            raise ValueError("radar policy output order does not match the runtime")
        weights = payload["weights"]
        model = cls(
            weights_1=np.asarray(weights["layer_1"], dtype=np.float64),
            bias_1=np.asarray(weights["bias_1"], dtype=np.float64),
            weights_2=np.asarray(weights["layer_2"], dtype=np.float64),
            bias_2=np.asarray(weights["bias_2"], dtype=np.float64),
            metadata=dict(payload.get("metadata") or {}),
        )
        if model.weights_1.shape[0] != len(FEATURE_NAMES):
            raise ValueError("radar policy has an invalid input layer")
        if model.weights_2.shape != (model.weights_1.shape[1], len(OUTPUT_NAMES)):
            raise ValueError("radar policy has an invalid output layer")
        return model


def generate_training_data(sample_count: int, *, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    sample_count = max(int(sample_count), 256)
    features = np.empty((sample_count, len(FEATURE_NAMES)), dtype=np.float64)
    targets = np.empty((sample_count, len(OUTPUT_NAMES)), dtype=np.float64)
    for index in range(sample_count):
        detected = bool(rng.random() < 0.58)
        x = float(rng.uniform(0.05, 0.95))
        y = float(rng.uniform(0.05, 0.95))
        heading = float(rng.uniform(-math.pi, math.pi))
        phase = float(rng.uniform(-math.pi, math.pi))
        detection = None
        if detected:
            detection = {
                "distance": float(rng.uniform(0.06, 0.30)),
                "bearing_deg": float(rng.uniform(-180.0, 180.0)),
            }
        vector = feature_vector(
            x=x,
            y=y,
            heading=heading,
            phase=phase,
            detection=detection,
            scan_range=0.30,
        )
        features[index] = vector
        targets[index] = expert_action(vector)
    return features, targets


def train_behavior_clone(
    *,
    sample_count: int = 30_000,
    epochs: int = 120,
    hidden_size: int = 48,
    batch_size: int = 512,
    learning_rate: float = 0.003,
    seed: int = 20260906,
) -> tuple[RadarPolicyModel, dict[str, float]]:
    x, y = generate_training_data(sample_count, seed=seed)
    split = int(len(x) * 0.9)
    x_train, x_validation = x[:split], x[split:]
    y_train, y_validation = y[:split], y[split:]
    rng = np.random.default_rng(seed + 1)
    w1 = rng.normal(0.0, 1.0 / math.sqrt(x.shape[1]), size=(x.shape[1], hidden_size))
    b1 = np.zeros(hidden_size, dtype=np.float64)
    w2 = rng.normal(0.0, 1.0 / math.sqrt(hidden_size), size=(hidden_size, y.shape[1]))
    b2 = np.zeros(y.shape[1], dtype=np.float64)
    parameters = [w1, b1, w2, b2]
    first_moments = [np.zeros_like(value) for value in parameters]
    second_moments = [np.zeros_like(value) for value in parameters]
    step = 0

    for _epoch in range(max(int(epochs), 1)):
        indices = rng.permutation(len(x_train))
        for offset in range(0, len(indices), max(int(batch_size), 1)):
            batch = indices[offset : offset + batch_size]
            inputs = x_train[batch]
            expected = y_train[batch]
            hidden = np.tanh(inputs @ w1 + b1)
            output = np.tanh(hidden @ w2 + b2)
            grad_output = (2.0 / max(len(batch), 1)) * (output - expected) * (1.0 - output * output)
            gradients = [
                inputs.T @ ((grad_output @ w2.T) * (1.0 - hidden * hidden)),
                np.sum((grad_output @ w2.T) * (1.0 - hidden * hidden), axis=0),
                hidden.T @ grad_output,
                np.sum(grad_output, axis=0),
            ]
            step += 1
            for parameter, first, second, gradient in zip(parameters, first_moments, second_moments, gradients):
                first *= 0.9
                first += 0.1 * gradient
                second *= 0.999
                second += 0.001 * gradient * gradient
                corrected_first = first / (1.0 - 0.9**step)
                corrected_second = second / (1.0 - 0.999**step)
                parameter -= learning_rate * corrected_first / (np.sqrt(corrected_second) + 1e-8)

    model = RadarPolicyModel(w1, b1, w2, b2, metadata={})
    train_prediction = np.asarray([model.predict(row) for row in x_train])
    validation_prediction = np.asarray([model.predict(row) for row in x_validation])
    metrics = {
        "train_mse": float(np.mean((train_prediction - y_train) ** 2)),
        "validation_mse": float(np.mean((validation_prediction - y_validation) ** 2)),
        "validation_mae": float(np.mean(np.abs(validation_prediction - y_validation))),
    }
    model.metadata = {
        "algorithm": "behavior_cloning",
        "teacher": "radar_coverage_and_approach_v1",
        "seed": int(seed),
        "training_samples": int(sample_count),
        "epochs": int(epochs),
        "metrics": metrics,
        "inference_dependency": "numpy",
    }
    return model, metrics
