"""Shared validation for the isolated desktop Replay JSON-lines protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

SCHEMA = "gpa.desktop-replay-worker/v1"


class DesktopWorkerProtocolError(RuntimeError):
    """Raised when the child process emits an invalid or out-of-order event."""


def _non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DesktopWorkerProtocolError(f"Desktop replay worker has an invalid {field} value.")
    return value


def validate_desktop_worker_result(payload: object) -> dict[str, Any]:
    """Validate the child-process result before it mutates replay state."""
    if not isinstance(payload, dict):
        raise DesktopWorkerProtocolError("Desktop replay worker returned a non-object result.")
    if not isinstance(payload.get("success"), bool):
        raise DesktopWorkerProtocolError(
            "Desktop replay worker result has an invalid success flag."
        )
    if not isinstance(payload.get("error", ""), str):
        raise DesktopWorkerProtocolError(
            "Desktop replay worker result has an invalid error message."
        )
    n_steps = _non_negative_integer(payload.get("n_steps"), "n_steps")
    n_failed = _non_negative_integer(payload.get("n_failed"), "n_failed")
    if n_failed > n_steps:
        raise DesktopWorkerProtocolError(
            "Desktop replay worker result reports more failures than steps."
        )
    for field in ("steps", "llm_metrics"):
        if not isinstance(payload.get(field), list):
            raise DesktopWorkerProtocolError(
                f"Desktop replay worker result has an invalid {field} value."
            )
    return dict(payload)


@dataclass
class DesktopReplayProtocol:
    """Consume one valid worker event stream in strict lifecycle order."""

    ready_seen: bool = False
    result_seen: bool = False
    last_step_number: int = 0

    def accept(self, event: object) -> Optional[tuple[str, dict[str, Any]]]:
        if not isinstance(event, dict) or event.get("schema") != SCHEMA:
            return None
        event_name = str(event.get("event") or "")
        if event_name == "ready":
            if self.ready_seen:
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker emitted duplicate ready events."
                )
            if self.result_seen:
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker emitted ready out of order."
                )
            self.ready_seen = True
            payload = dict(event)
            payload["total_steps"] = _non_negative_integer(
                event.get("total_steps"), "total_steps"
            )
            return event_name, payload
        if event_name in {"step_start", "agent_decision", "result"}:
            if not self.ready_seen or self.result_seen:
                raise DesktopWorkerProtocolError(
                    f"Desktop replay worker emitted {event_name} out of order."
                )
        if event_name == "step_start":
            step = event.get("step")
            if not isinstance(step, dict):
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker emitted an invalid step_start payload."
                )
            number = _non_negative_integer(step.get("number"), "step number")
            if number == 0:
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker has an invalid step number value."
                )
            if number <= self.last_step_number:
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker emitted a duplicate or out-of-order step number."
                )
            self.last_step_number = number
            return event_name, dict(event)
        if event_name == "agent_decision":
            if not isinstance(event.get("decision"), dict):
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker emitted an invalid agent_decision payload."
                )
            step_number = _non_negative_integer(
                event.get("step_number"), "agent decision step number"
            )
            if step_number == 0 or step_number != self.last_step_number:
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker emitted an agent decision for the wrong step."
                )
            return event_name, dict(event)
        if event_name == "result":
            self.result_seen = True
            payload = dict(event)
            payload["result"] = validate_desktop_worker_result(event.get("result"))
            return event_name, payload
        if event_name == "crash":
            if self.result_seen:
                raise DesktopWorkerProtocolError(
                    "Desktop replay worker crashed after emitting a result."
                )
            return event_name, dict(event)
        return None


__all__ = [
    "DesktopReplayProtocol",
    "DesktopWorkerProtocolError",
    "SCHEMA",
    "validate_desktop_worker_result",
]
