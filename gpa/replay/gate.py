"""Deterministic reproduction gate shared by product surfaces."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from gpa.replay.environment import compare_environments
from gpa.replay.understanding import build_reproduction_contract


def build_reproduction_gate(
    workflow,
    *,
    quality: Mapping[str, Any],
    current_environment: Mapping[str, Any],
    safe_web: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine quality, evidence and execution capability into one decision."""
    environment = dict(getattr(workflow, "environment", {}) or {})
    target_environment = dict(current_environment or {})
    environment_diff = compare_environments(environment, target_environment)
    understanding = dict(getattr(workflow, "understanding", {}) or {})
    artifacts = dict(getattr(workflow, "artifacts", {}) or {})
    quality = dict(quality or {})
    safe_web = dict(safe_web or {})
    contract = build_reproduction_contract(
        step_count=len(workflow.steps),
        environment=environment,
        understanding=understanding,
        artifacts=artifacts,
        environment_diff=environment_diff,
        recording_verified=bool(artifacts.get("recording")),
    )
    execution_mode = "safe_web" if safe_web.get("runnable") else "desktop"
    blockers: list[dict[str, str]] = []
    if not quality.get("runnable", True):
        blockers.append({
            "code": "workflow_quality",
            "message": "Workflow quality checks contain blocking issues.",
        })
    if execution_mode == "desktop" and environment_diff.get("safe_to_attempt") is False:
        environment_message = (
            "Recorded or target environment evidence is missing; capture it before desktop replay."
            if environment_diff.get("status") == "unknown"
            else "Recorded and target platforms differ; desktop actions must be replanned."
        )
        blockers.append({
            "code": "environment_replan_required",
            "message": environment_message,
        })
    review_items = [
        {
            "code": str(check_id),
            "message": f"Reproduction evidence is incomplete: {check_id}.",
        }
        for check_id in contract.get("blockers", [])
    ]
    if blockers:
        status = "blocked"
    elif contract.get("publishable_as_verified") and environment_diff.get("differences"):
        status = "adaptation_required"
    elif contract.get("publishable_as_verified"):
        status = "ready"
    else:
        status = "review"
    score = round(
        (float(quality.get("score") or 0) + float(contract.get("score") or 0)) / 2
    )
    decision_material = {
        "workflow_id": workflow.workflow_id,
        "execution_mode": execution_mode,
        "quality_score": quality.get("score"),
        "contract_score": contract.get("score"),
        "environment_diff": environment_diff,
        "blocker_codes": [item["code"] for item in blockers],
    }
    decision_id = hashlib.sha256(
        json.dumps(
            decision_material,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:24]
    return {
        "schema": "gpa.reproduction-gate/v1",
        "decision_id": decision_id,
        "status": status,
        "score": score,
        "can_execute": not blockers,
        "execution_mode": execution_mode,
        "desktop_input": execution_mode == "desktop",
        "blockers": blockers,
        "review_items": review_items,
        "quality": quality,
        "reproduction_contract": contract,
        "environment_diff": environment_diff,
        "safe_web": safe_web,
        "reused_assumptions": list(environment_diff.get("reusable_assumptions") or []),
        "required_adaptations": list(environment_diff.get("adaptation_plan") or []),
        "success_criteria_count": len(understanding.get("success_criteria") or []),
    }
