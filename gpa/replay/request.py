"""Validation and normalization for Replay start requests."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ReplayStartRequest:
    client_id: str
    execution_mode: str
    arm_token: str
    gate_decision_id: str
    variables: dict[str, str]
    client_environment: dict[str, Any]
    threshold: float
    retries: int
    countdown_seconds: int
    max_runtime_seconds: int
    space_id: str


def mapping_field(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return dict(value)


def list_field(value: Any, *, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return list(value)


def _finite_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _integer(value: Any, *, field: str) -> int:
    parsed = _finite_float(value, field=field)
    if not parsed.is_integer():
        raise ValueError(f"{field} must be an integer")
    return int(parsed)


def parse_replay_start_request(
    body: Mapping[str, Any],
    *,
    max_retries: int,
) -> ReplayStartRequest:
    """Return a typed request or fail before any replay state is mutated."""

    if not isinstance(body, Mapping):
        raise TypeError("Replay request must be an object")
    execution_mode = str(body.get("execution_mode") or "auto").strip().casefold()
    if execution_mode not in {"auto", "desktop", "safe_web"}:
        raise ValueError("execution_mode must be auto, desktop, or safe_web.")

    raw_variables = mapping_field(body.get("variables", {}), field="variables")
    variables = {str(key): str(value) for key, value in raw_variables.items()}
    client_environment = mapping_field(
        body.get("client_environment", {}),
        field="client_environment",
    )
    threshold = _finite_float(body.get("threshold", 0.5), field="threshold")
    retries = _integer(body.get("retries", 5), field="retries")
    countdown = _integer(body.get("countdown_seconds", 3), field="countdown_seconds")
    runtime = _integer(body.get("max_runtime_seconds", 300), field="max_runtime_seconds")

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1.")
    if not 0 <= retries <= max_retries:
        raise ValueError(f"retries must be between 0 and {max_retries}.")

    return ReplayStartRequest(
        client_id=str(body.get("client_id") or "").strip(),
        execution_mode=execution_mode,
        arm_token=str(body.get("arm_token") or ""),
        gate_decision_id=str(body.get("gate_decision_id") or "").strip().casefold(),
        variables=variables,
        client_environment=client_environment,
        threshold=threshold,
        retries=retries,
        countdown_seconds=max(0, min(30, countdown)),
        max_runtime_seconds=max(10, min(3600, runtime)),
        space_id=str(body.get("space_id") or "").strip(),
    )
