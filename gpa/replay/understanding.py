"""Build a portable, deterministic description of what a Workflow means."""
from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from gpa.replay.targeting import build_target_contract


def build_agent_understanding(workflow, step_subgraphs: dict[str, Any] | None = None) -> dict[str, Any]:
    step_subgraphs = dict(step_subgraphs or {})
    for step in workflow.steps:
        step.metadata = dict(step.metadata or {})
        step.metadata["target_contract"] = build_target_contract(
            step,
            step_subgraphs.get(step.id),
        )
    action_counts = Counter(str(step.action_type or "unknown") for step in workflow.steps)
    apps = sorted({str(step.active_app_name or "").strip() for step in workflow.steps if step.active_app_name})
    provenance = dict(getattr(workflow, "provenance", {}) or {})
    host_set = {
        urlparse(str(step.value or "")).hostname or ""
        for step in workflow.steps
        if step.action_type == "open_url"
    } - {""}
    resolved_urls = provenance.get("resolved_source_urls")
    if isinstance(resolved_urls, dict):
        host_set.update(
            urlparse(str(value or "")).hostname or ""
            for value in resolved_urls.values()
        )
    hosts = sorted(host_set - {""})
    assertions = [
        {
            "step": step.step_number,
            "type": step.action_type,
            "expected": str(step.value or ""),
            "description": str(step.action or ""),
        }
        for step in workflow.steps
        if step.action_type in {
            "assert_text", "assert_not_text", "assert_link", "assert_url", "assert_clipboard", "wait_for_text"
        }
    ]
    suggested_assertions = _suggest_success_criteria(workflow.steps, assertions)
    risky_steps = [
        {
            "step": step.step_number,
            "type": step.action_type,
            "description": str(step.action or ""),
        }
        for step in workflow.steps
        if step.action_type in {"type", "hotkey", "click", "drag"}
    ]
    mutation_signals = _mutation_signals(workflow.steps)
    semantic_plan = [_portable_step(step) for step in workflow.steps]
    return {
        "schema": "gpa.agent-understanding/v1",
        "goal": workflow.task_description or workflow.description or workflow.workflow_title,
        "summary": workflow.description or workflow.workflow_title,
        # Keep the persisted representation deterministic. Reusing the same
        # list/dict objects here makes PyYAML emit anchors after a reload,
        # changing package fingerprints without changing task semantics.
        "source": copy.deepcopy(provenance),
        "required_environment": {
            "applications": list(apps),
            "web_hosts": list(hosts),
        },
        "interaction_profile": {
            "step_count": len(workflow.steps),
            "action_counts": dict(sorted(action_counts.items())),
            "uses_global_keyboard": bool(action_counts["type"] or action_counts["hotkey"]),
            "uses_visual_targeting": bool(
                action_counts["click"] or action_counts["drag"] or action_counts["scroll"]
            ),
        },
        "success_criteria": assertions,
        "suggested_success_criteria": suggested_assertions,
        "semantic_plan": semantic_plan,
        "invariants": [
            "Confirm the active application and page before any keyboard action.",
            "Re-localize semantic targets after platform, viewport, or layout changes.",
            "Stop instead of continuing when a success criterion or focus guard fails.",
        ],
        "risk_controls": {
            "risky_steps": risky_steps,
            "requires_focus_guard": bool(action_counts["type"] or action_counts["hotkey"]),
            "read_only": not mutation_signals,
            "mutation_signals": mutation_signals,
            "requires_explicit_arm": bool(mutation_signals),
        },
        "adaptation": {
            "prefer_semantic_assertions": bool(assertions),
            "recorded_apps": list(apps),
            "recorded_hosts": list(hosts),
            "variables": [variable.to_dict() for variable in workflow.variables],
        },
        "recording_intent": {
            "source": "task_description",
            "narration": str((getattr(workflow, "provenance", {}) or {}).get("narration") or ""),
            "raw_media_persisted": False,
            "normalization": dict(
                ((getattr(workflow, "provenance", {}) or {}).get("recording_analysis") or {})
            ),
        },
    }


def _suggest_success_criteria(steps, assertions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Propose deterministic criteria without pretending they were verified."""
    if assertions:
        return []
    suggestions: list[dict[str, Any]] = []
    for step in reversed(list(steps)):
        metadata = dict(step.metadata or {})
        target_url = str(metadata.get("target_url") or "").strip()
        target_hint = str(metadata.get("target_hint") or "").strip()
        if step.action_type == "open_url" and str(step.value or "").strip():
            suggestions.append({
                "type": "assert_url",
                "expected": str(step.value),
                "reason": "Confirm the final page remains on the recorded destination.",
                "source_step": step.step_number,
            })
            break
        if target_url:
            host = urlparse(target_url).hostname or target_url
            suggestions.append({
                "type": "assert_url",
                "expected": host,
                "reason": "Confirm the task finishes on the intended site.",
                "source_step": step.step_number,
            })
            break
        if target_hint and step.action_type in {"click", "hotkey", "type"}:
            suggestions.append({
                "type": "assert_text",
                "expected": target_hint,
                "reason": "Review and edit this proposed visible success signal before publishing.",
                "source_step": step.step_number,
            })
            break
    return suggestions


def _portable_step(step) -> dict[str, Any]:
    action_type = str(step.action_type or "unknown")
    if action_type == "open_url":
        phase = "navigate"
        recovery = "Open the canonical URL again and verify the host before continuing."
    elif action_type in {
        "assert_text", "assert_not_text", "assert_link", "assert_url", "assert_clipboard", "wait_for_text"
    }:
        phase = "verify"
        recovery = "Stop and report the unmet criterion; do not infer success."
    elif action_type in {"type", "hotkey"}:
        phase = "act"
        recovery = "Re-check focus and field state before retrying keyboard input."
    elif action_type in {"click", "drag", "scroll"}:
        phase = "act"
        recovery = "Re-localize by semantic/visual evidence instead of recorded coordinates."
    else:
        phase = "observe" if action_type in {"wait"} else "act"
        recovery = "Re-evaluate the current state before retrying."
    value = str(step.value or "")
    host = urlparse(value).hostname or "" if action_type == "open_url" else ""
    return {
        "step": int(step.step_number),
        "phase": phase,
        "action_type": action_type,
        "intent": str(step.action or ""),
        "application": str(step.active_app_name or ""),
        "host": host,
        "expected": value if action_type in {
            "assert_text", "assert_not_text", "assert_link", "assert_url", "assert_clipboard", "wait_for_text"
        } else "",
        "recovery": recovery,
    }


def _mutation_signals(steps) -> list[dict[str, Any]]:
    tokens = {
        "send", "submit", "save", "update", "edit", "delete", "remove", "purchase",
        "checkout", "book", "reserve", "upload", "publish", "post", "发送", "提交", "保存",
        "更新", "编辑", "删除", "移除", "购买", "下单", "预订", "预约", "上传", "发布",
    }
    signals: list[dict[str, Any]] = []
    for step in steps:
        haystack = " ".join((str(step.action or ""), str(step.value or ""))).casefold()
        matched = sorted(token for token in tokens if token in haystack)
        if matched:
            signals.append({
                "step": int(step.step_number),
                "action_type": str(step.action_type or ""),
                "signals": matched,
                "description": str(step.action or ""),
            })
    return signals


def build_reproduction_contract(
    *,
    step_count: int,
    environment: dict[str, Any] | None,
    understanding: dict[str, Any] | None,
    artifacts: dict[str, Any] | None,
    environment_diff: dict[str, Any] | None = None,
    recording_verified: bool | None = None,
) -> dict[str, Any]:
    """Summarize whether another Agent has enough evidence to reproduce a run.

    The contract is intentionally deterministic and contains no model-written
    confidence claims.  It is safe to persist with a Store record and can be
    recomputed for a requesting Agent's current environment.
    """
    environment = dict(environment or {})
    understanding = dict(understanding or {})
    artifacts = dict(artifacts or {})
    environment_diff = dict(environment_diff or {})
    recording = artifacts.get("recording") if isinstance(artifacts.get("recording"), dict) else {}
    interaction = (
        understanding.get("interaction_profile")
        if isinstance(understanding.get("interaction_profile"), dict)
        else {}
    )
    required = (
        understanding.get("required_environment")
        if isinstance(understanding.get("required_environment"), dict)
        else {}
    )
    criteria = understanding.get("success_criteria")
    criteria = criteria if isinstance(criteria, list) else []
    differences = environment_diff.get("differences")
    differences = differences if isinstance(differences, list) else []
    adaptations = environment_diff.get("adaptation_plan")
    adaptations = adaptations if isinstance(adaptations, list) else []

    digest = str(recording.get("sha256") or "")
    recording_metadata_complete = bool(
        recording
        and recording.get("path") in {"recording.webm", "recording.mp4"}
        and recording.get("mime_type") in {"video/webm", "video/mp4"}
        and _positive_int(recording.get("bytes")) > 0
        and re.fullmatch(r"[a-f0-9]{64}", digest)
    )
    # A declared file is not evidence until an isolated media decoder has
    # explicitly verified it.  ``None`` means unknown, never success.
    recording_evidence = recording_metadata_complete and recording_verified is True
    capture_scope = _normalized_capture_scope(recording.get("capture_scope"))
    source_trace = (
        artifacts.get("source_trace")
        if isinstance(artifacts.get("source_trace"), dict)
        else {}
    )
    source_trace_digest = str(source_trace.get("sha256") or "")
    source_trace_required = capture_scope == "public-web-evidence"
    source_trace_evidence = bool(
        not source_trace_required
        or (
            source_trace.get("path") == "source_trace.json"
            and source_trace.get("schema") == "gpa.safe-web-source-trace/v1"
            and _positive_int(source_trace.get("bytes")) > 0
            and re.fullmatch(r"[a-f0-9]{64}", source_trace_digest)
            and str(source_trace.get("source_run_id") or "")
            == str(recording.get("source_run_id") or "")
            and _positive_int(source_trace.get("page_count")) > 0
            and _positive_int(source_trace.get("verified_page_count"))
            == _positive_int(source_trace.get("page_count"))
        )
    )
    privacy_review = (
        recording.get("privacy_review")
        if isinstance(recording.get("privacy_review"), dict)
        else {}
    )
    confirmed_scope = _normalized_capture_scope(privacy_review.get("scope_confirmed"))
    privacy_safe_scopes = {
        "browser", "browser-tab", "window", "application-window", "public-web-evidence"
    }
    recording_privacy = bool(
        recording_evidence
        and capture_scope in privacy_safe_scopes
        and confirmed_scope == capture_scope
        and privacy_review.get("status") == "passed"
        and privacy_review.get("other_apps_visible") is False
    )
    system = environment.get("system") if isinstance(environment.get("system"), dict) else {}
    screen = environment.get("screen") if isinstance(environment.get("screen"), dict) else {}
    browser = environment.get("browser") if isinstance(environment.get("browser"), dict) else {}
    capture_surface = (
        environment.get("capture_surface")
        if isinstance(environment.get("capture_surface"), dict)
        else {}
    )
    # Decoded recording frames are the authoritative geometry for a portable
    # handoff.  A physical monitor may remain in older environment snapshots,
    # but it must not override a newer browser-tab/window capture surface.
    dimension_sources = (
        ("capture_surface", capture_surface),
        ("browser.viewport", {
            "width": browser.get("viewport_width"),
            "height": browser.get("viewport_height"),
        }),
        ("screen", screen),
    )
    dimension_source, recorded_dimensions = next(
        (
            (name, values)
            for name, values in dimension_sources
            if _positive_int(values.get("width")) > 0
            and _positive_int(values.get("height")) > 0
        ),
        ("", {}),
    )
    recorded_environment = bool(
        str(system.get("name") or "").strip()
        and str(system.get("machine") or "").strip()
        and recorded_dimensions
    )
    described_steps = _positive_int(interaction.get("step_count"))
    agent_understanding = bool(
        str(understanding.get("goal") or "").strip()
        and described_steps == _positive_int(step_count)
        and understanding.get("schema") == "gpa.agent-understanding/v1"
    )
    success_criteria = bool(criteria)

    significant_fields = {
        str(item.get("field") or "")
        for item in differences
        if isinstance(item, dict) and item.get("severity") in {"warn", "blocking"}
    }
    planned_fields = {
        str(item.get("field") or "")
        for item in adaptations
        if isinstance(item, dict) and item.get("action")
    }
    adaptation_coverage = not significant_fields or significant_fields <= planned_fields
    current_environment_known = (
        bool(environment_diff.get("current_environment_known"))
        if "current_environment_known" in environment_diff
        else bool(environment_diff.get("matches") or environment_diff.get("differences"))
    )
    checks = [
        _contract_check("workflow_structure", "Workflow 结构", _positive_int(step_count) > 0, 10),
        _contract_check("recording_evidence", "真实运行视频与校验和", recording_evidence, 15),
        _contract_check("source_trace_evidence", "公开来源机读证据链", source_trace_evidence, 5),
        _contract_check("recording_privacy", "安全捕获范围与隐私复核", recording_privacy, 10),
        _contract_check("recorded_environment", "原始系统与捕获环境", recorded_environment, 15),
        _contract_check("agent_understanding", "Agent 可移植理解", agent_understanding, 20),
        _contract_check("success_criteria", "可验证成功条件", success_criteria, 15),
        _contract_check("adaptation_coverage", "环境差异适配覆盖", adaptation_coverage, 10),
    ]
    blockers = [item["id"] for item in checks if not item["passed"]]
    warnings: list[str] = []
    if recording_evidence and not all(
        _positive_int(recording.get(field)) > 0 for field in ("width", "height")
    ):
        warnings.append("recording_dimensions_missing")
    if recording_evidence and not _positive_float(recording.get("duration_seconds")) > 0:
        warnings.append("recording_duration_missing")
    if not current_environment_known:
        warnings.append("current_environment_unknown")
    if environment_diff.get("status") in {"degraded", "blocked"}:
        warnings.append("environment_adaptation_required")
    score = sum(item["weight"] for item in checks if item["passed"])
    publishable = not blockers
    if not publishable:
        status = "incomplete"
    elif environment_diff.get("status") in {"degraded", "blocked"}:
        status = "adaptation_required"
    elif not current_environment_known:
        status = "environment_unknown"
    else:
        status = "verified"
    return {
        "schema": "gpa.reproduction-contract/v1",
        "status": status,
        "score": score,
        "publishable_as_verified": publishable,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "handoff": {
            "required_applications": list(required.get("applications") or []),
            "required_web_hosts": list(required.get("web_hosts") or []),
            "recorded_matches": list(environment_diff.get("matches") or []),
            "differences": [dict(item) for item in differences if isinstance(item, dict)],
            "adaptation_plan": [dict(item) for item in adaptations if isinstance(item, dict)],
            "success_criteria_count": len(criteria),
            "read_only": bool((understanding.get("risk_controls") or {}).get("read_only")),
            "recording_capture_scope": capture_scope or "unknown",
            "recording_capture_method": str(recording.get("capture_method") or "unknown"),
            "recording_privacy_reviewed": recording_privacy,
            "source_trace_required": source_trace_required,
            "source_trace_verified": source_trace_evidence,
            "source_trace_page_count": _positive_int(source_trace.get("page_count")),
            "recorded_capture_dimensions": {
                "source": dimension_source or "unknown",
                "width": _positive_int(recorded_dimensions.get("width")),
                "height": _positive_int(recorded_dimensions.get("height")),
            },
        },
    }


def _contract_check(check_id: str, label: str, passed: bool, weight: int) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "passed": bool(passed),
        "weight": int(weight),
    }


def _normalized_capture_scope(value: Any) -> str:
    scope = str(value or "").strip().casefold()
    aliases = {
        "browser-tab-only": "browser-tab",
        "tab": "browser-tab",
        "single-window": "window",
        "app-window": "application-window",
        "display": "monitor",
        "screen": "monitor",
    }
    return aliases.get(scope, scope)


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0
