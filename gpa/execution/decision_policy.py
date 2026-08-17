"""Pure decision policies shared by replay execution and recovery.

The executor owns effects such as observing the screen and sending input. This
module owns the deterministic question of what an agent decision means. Keeping
that boundary explicit makes malformed or contradictory model output testable
without starting desktop automation.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Mapping


class StepDecisionKind(str, Enum):
    """The only outcomes the step executor needs to handle."""

    EXECUTE = "execute"
    CORRECT = "correct"
    SUCCEED = "succeed"
    FAIL = "fail"
    STOP = "stop"


class FinalStateKind(str, Enum):
    """Bounded outcomes for final-state verification."""

    COMPLETE = "complete"
    WAIT = "wait"
    CORRECT = "correct"
    FAIL = "fail"


_SUCCESSFUL_SKIP_REASONS = frozenset({"already_done", "redundant"})
_SUCCESSFUL_SKIP_MARKERS = (
    "already done", "already complete", "already completed", "redundant", "no-op",
    "已完成", "已经完成", "重複", "重复", "无需",
)
_TRANSIENT_FINAL_STATE_MARKERS = (
    "saving", "syncing", "in progress", "still processing", "please wait",
    "正在同步", "保存中", "处理中", "處理中", "请稍候", "請稍候",
)


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def decision_requests_correction(decision: Mapping[str, Any]) -> bool:
    """Require both the correction flag and an actionable correction type."""

    action_type = _normalized(decision.get("correction_action_type"))
    return bool(decision.get("requires_correction")) and action_type not in {"", "none"}


def decision_declines_execution(decision: Mapping[str, Any]) -> bool:
    return (
        not decision.get("should_execute", True)
        or _normalized(decision.get("action_type")) == "skip"
    )


def skip_is_successful(decision: Mapping[str, Any]) -> bool:
    if _normalized(decision.get("skip_reason")) in _SUCCESSFUL_SKIP_REASONS:
        return True
    reason = _normalized(decision.get("reason"))
    return any(marker in reason for marker in _SUCCESSFUL_SKIP_MARKERS)


def classify_step_decision(decision: Mapping[str, Any]) -> StepDecisionKind:
    """Resolve contradictory fields using a conservative precedence order."""

    if _normalized(decision.get("action_type")) == "stop":
        return StepDecisionKind.STOP
    if decision_requests_correction(decision):
        return StepDecisionKind.CORRECT
    if decision_declines_execution(decision):
        return StepDecisionKind.SUCCEED if skip_is_successful(decision) else StepDecisionKind.FAIL
    return StepDecisionKind.EXECUTE


def final_state_is_transient(decision: Mapping[str, Any]) -> bool:
    if decision.get("complete") or decision_requests_correction(decision):
        return False
    text = " ".join(
        [_normalized(decision.get("reason")), _normalized(decision.get("correction"))]
    )
    return any(marker in text for marker in _TRANSIENT_FINAL_STATE_MARKERS)


def classify_final_state(decision: Mapping[str, Any]) -> FinalStateKind:
    """Choose whether final verification completes, waits, repairs, or fails."""

    if decision.get("complete"):
        return FinalStateKind.COMPLETE
    if final_state_is_transient(decision):
        return FinalStateKind.WAIT
    if decision_requests_correction(decision):
        return FinalStateKind.CORRECT
    return FinalStateKind.FAIL
