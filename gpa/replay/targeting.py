"""Portable semantic target contracts and deterministic actionability checks."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

COORDINATE_ACTIONS = {"click", "double_click", "drag", "scroll", "select"}


def build_target_contract(step, subgraph=None) -> dict[str, Any]:
    """Describe a GUI target without treating recorded coordinates as identity."""
    metadata = dict(getattr(step, "metadata", {}) or {})
    target = getattr(subgraph, "target_node", None) if subgraph is not None else None
    graph = getattr(subgraph, "ui_graph", None) if subgraph is not None else None
    role = str(getattr(target, "elem_type", "") or metadata.get("target_role") or "").strip()
    content = str(getattr(target, "content", "") or "").strip()
    name = str(metadata.get("target_name") or metadata.get("target_hint") or content).strip()
    context: list[str] = []
    if graph is not None and target is not None:
        try:
            for node in graph.neighbors_of(target.id):
                value = str(getattr(node, "content", "") or "").strip()
                if value and value.casefold() != name.casefold() and value not in context:
                    context.append(value)
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    coordinates = list(getattr(subgraph, "click_coordinates", []) or []) if subgraph else []
    action_type = str(getattr(step, "action_type", "") or "").casefold()
    strategies = [
        item
        for item, enabled in (
            ("role_and_name", bool(role and name)),
            ("label_or_text", bool(name)),
            ("relative_context", bool(context)),
            ("recorded_structure", bool(target is not None)),
            ("visual_similarity", bool(subgraph is not None)),
            ("scaled_coordinates", len(coordinates) >= 2),
        )
        if enabled
    ]
    semantic_state = {
        "role": role,
        "name": name,
        "relative_context": context[:6],
    }
    state_signature = hashlib.sha256(
        json.dumps(semantic_state, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()[:20]
    return {
        "schema": "gpa.semantic-target/v2",
        "application": str(getattr(step, "active_app_name", "") or ""),
        "role": role,
        "name": name,
        "relative_context": context[:6],
        "state_signature": state_signature,
        "state_snapshot": semantic_state,
        "recorded_url": str(metadata.get("target_url") or ""),
        "strategies": strategies,
        "coordinate_fallback": coordinates[:2],
        "coordinate_fallback_allowed": bool(
            action_type in COORDINATE_ACTIONS
            and len(coordinates) >= 2
            and (name or role or context)
        ),
        "actionability": {
            "requires_unique": action_type not in {"scroll"},
            "requires_visible": action_type in COORDINATE_ACTIONS,
            "requires_stable": action_type in COORDINATE_ACTIONS,
            "requires_enabled": action_type in {"click", "double_click", "select"},
            "requires_unobscured": action_type in {"click", "double_click", "drag", "select"},
        },
    }


def enrich_step_target(step, subgraph=None) -> dict[str, Any]:
    contract = build_target_contract(step, subgraph)
    step.metadata = {**dict(getattr(step, "metadata", {}) or {}), "target_contract": contract}
    return contract


def evaluate_actionability(
    contract: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed when a required target property is false or unknown."""
    requirements = dict(contract.get("actionability") or {})
    checks = {
        "unique": int(observation.get("candidate_count", 0)) == 1,
        "visible": observation.get("visible") is True,
        "stable": observation.get("stable") is True,
        "enabled": observation.get("enabled") is True,
        "unobscured": observation.get("unobscured") is True,
    }
    required = {
        name: checks[name]
        for name in checks
        if requirements.get(f"requires_{name}") is True
    }
    failed = [name for name, passed in required.items() if not passed]
    confidence = float(observation.get("confidence") or 0.0)
    ambiguous = int(observation.get("candidate_count", 0)) > 1
    status = "ready" if not failed and confidence >= 0.6 else "needs_review" if ambiguous else "blocked"
    return {
        "schema": "gpa.actionability/v1",
        "status": status,
        "can_act": status == "ready",
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "checks": required,
        "failed_checks": failed,
        "requires_human": status == "needs_review",
    }


__all__ = ["build_target_contract", "enrich_step_target", "evaluate_actionability"]
