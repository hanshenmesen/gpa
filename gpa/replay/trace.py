"""Compact, portable Replay decision traces."""
from __future__ import annotations

from typing import Any


def step_trace(step: Any, result: Any) -> dict[str, Any]:
    get = result.get if isinstance(result, dict) else lambda key, default=None: getattr(result, key, default)
    localization = get("localization")
    if localization is not None and not isinstance(localization, dict):
        localization = {
            "x": getattr(localization, "x", 0),
            "y": getattr(localization, "y", 0),
            "confidence": getattr(localization, "confidence", 0),
            "method": getattr(localization, "method", ""),
        }
    state = get("state", "")
    state = str(getattr(state, "name", state) or "").casefold()
    decision = dict(get("agent_decision", {}) or {})
    phases = [
        {"phase": "observe", "evidence": list(get("observation_metrics", []) or [])},
        {"phase": "locate", "result": localization},
        {"phase": "decide", "result": decision},
        {"phase": "act", "status": state, "retries": int(get("retries", 0) or 0)},
        {
            "phase": "verify",
            "passed": get("postcondition_verified"),
            "reason": str(get("postcondition_reason", "") or ""),
            "source": str(get("evidence_source", "") or ""),
        },
    ]
    confidence = float((localization or {}).get("confidence") or decision.get("confidence") or 0)
    error = str(get("error", "") or "")
    return {
        "step": int(getattr(step, "step_number", get("step_number", 0)) or 0),
        "step_id": str(getattr(step, "id", "") or ""),
        "action_type": str(getattr(step, "action_type", "") or ""),
        "intent": str(getattr(step, "action", "") or ""),
        "target": dict((getattr(step, "metadata", {}) or {}).get("target_contract") or {}),
        "status": "failed" if error or state == "failed" else "passed",
        "confidence": round(confidence, 4),
        "phases": phases,
        "corrections": list(get("corrections", []) or []),
        "error": error,
        "intervention": _intervention(step, confidence, error, decision),
    }


def build_run_trace(workflow: Any, results: list[Any]) -> dict[str, Any]:
    by_number = {int(getattr(step, "step_number", 0)): step for step in workflow.steps}
    traces = []
    for result in results:
        number = int(result.get("step_number", 0) if isinstance(result, dict) else getattr(result, "step_number", 0))
        step = by_number.get(number)
        if step is not None:
            traces.append(step_trace(step, result))
    interventions = [item["intervention"] for item in traces if item.get("intervention")]
    sources = sorted({
        str(phase.get("source") or "")
        for item in traces
        for phase in item.get("phases", [])
        if phase.get("source")
    })
    chapters = _chapters(traces)
    return {
        "schema": "gpa.replay-trace/v1",
        "step_count": len(traces),
        "verified_steps": sum(1 for item in traces if item["status"] == "passed"),
        "failed_steps": sum(1 for item in traces if item["status"] == "failed"),
        "interventions": interventions,
        "evidence_sources": sources,
        "chapters": chapters,
        "steps": traces,
    }


def _chapters(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in traces:
        target = item.get("target") or {}
        app = str(target.get("application") or "General")
        if current is None or current["application"] != app:
            current = {
                "application": app,
                "start_step": item["step"],
                "end_step": item["step"],
                "status": item["status"],
            }
            chapters.append(current)
        else:
            current["end_step"] = item["step"]
            if item["status"] == "failed":
                current["status"] = "failed"
    return chapters


def _intervention(step: Any, confidence: float, error: str, decision: dict[str, Any]) -> dict[str, Any] | None:
    ambiguous = "ambig" in error.casefold() or "multiple" in error.casefold()
    if not error and (confidence <= 0 or confidence >= 0.55):
        return None
    reason = error or str(decision.get("reason") or "Target confidence is too low.")
    return {
        "schema": "gpa.human-intervention/v1",
        "step": int(getattr(step, "step_number", 0) or 0),
        "step_id": str(getattr(step, "id", "") or ""),
        "status": "awaiting_review",
        "reason": reason,
        "kind": "choose_target" if ambiguous or getattr(step, "action_type", "") in {"click", "drag", "scroll"} else "review_step",
        "resume_policy": "apply_versioned_patch_then_rerun",
    }


__all__ = ["build_run_trace", "step_trace"]
