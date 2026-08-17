"""Replay health, trust and secret-boundary checks."""
from __future__ import annotations

import re
from typing import Any, Mapping

_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "cookie", "cookies", "authorization",
    "api_key", "apikey", "access_token", "refresh_token", "session_cookie", "credentials",
}
_SENSITIVE_VALUE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}",
    re.IGNORECASE,
)


def sensitive_findings(value: Any, *, path: str = "") -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            normalized_key = str(key).strip().casefold().replace("-", "_")
            if normalized_key in _SENSITIVE_KEYS and item not in (None, "", False, [], {}):
                findings.append({"path": child, "reason": "sensitive_key"})
            findings.extend(sensitive_findings(item, path=child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            findings.extend(sensitive_findings(item, path=f"{path}[{index}]"))
    elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        findings.append({"path": path or "$", "reason": "credential_like_value"})
    unique = {(item["path"], item["reason"]): item for item in findings}
    return list(unique.values())


def build_replay_health(
    workflow,
    subgraphs: Mapping[str, Any] | None = None,
    *,
    recent_runs: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    subgraphs = dict(subgraphs or {})
    runs = list(recent_runs or [])[:20]
    understanding = dict(getattr(workflow, "understanding", {}) or {})
    environment = dict(getattr(workflow, "environment", {}) or {})
    artifacts = dict(getattr(workflow, "artifacts", {}) or {})
    assertions = list(understanding.get("success_criteria") or [])
    target_steps = [
        step for step in workflow.steps
        if str(step.action_type or "") in {"click", "double_click", "drag", "scroll", "select", "type"}
    ]
    semantic_targets = sum(
        1 for step in target_steps
        if (step.metadata or {}).get("target_contract", {}).get("strategies")
    )
    successful = sum(1 for run in runs if run.get("success") is True)
    completed = sum(1 for run in runs if isinstance(run.get("success"), bool))
    success_rate = round(successful / completed, 4) if completed else None
    secrets = sensitive_findings({
        "environment": environment,
        "understanding": understanding,
        "artifacts": artifacts,
        "steps": [step.to_dict() for step in workflow.steps],
    })
    dimensions = {
        "outcome": 100 if assertions else 35,
        "targeting": round(100 * semantic_targets / len(target_steps)) if target_steps else 100,
        "environment": 100 if environment.get("system") and environment.get("runtime") else 45,
        "privacy": 0 if secrets else 100,
        "evidence": 100 if artifacts.get("recording") else 60,
        "reliability": round(100 * success_rate) if success_rate is not None else 50,
    }
    score = round(sum(dimensions.values()) / len(dimensions))
    blockers = []
    if secrets:
        blockers.append("sensitive_data")
    if not assertions:
        blockers.append("success_criteria")
    if target_steps and semantic_targets < len(target_steps):
        blockers.append("semantic_targets")
    if not environment.get("system"):
        blockers.append("recorded_environment")
    grade = "verified" if score >= 85 and not blockers else "ready" if score >= 70 else "review" if score >= 50 else "blocked"
    return {
        "schema": "gpa.replay-health/v1",
        "score": score,
        "grade": grade,
        "dimensions": dimensions,
        "blockers": blockers,
        "secret_findings": secrets,
        "semantic_target_coverage": {
            "covered": semantic_targets,
            "total": len(target_steps),
        },
        "run_evidence": {
            "sample_size": completed,
            "success_rate": success_rate,
        },
    }


def assert_share_safe(workflow) -> None:
    findings = sensitive_findings({
        "environment": getattr(workflow, "environment", {}) or {},
        "understanding": getattr(workflow, "understanding", {}) or {},
        "artifacts": getattr(workflow, "artifacts", {}) or {},
        "steps": [step.to_dict() for step in workflow.steps],
    })
    if findings:
        paths = ", ".join(item["path"] for item in findings[:5])
        raise ValueError(f"Replay package contains credential or session material: {paths}")


__all__ = ["assert_share_safe", "build_replay_health", "sensitive_findings"]
