"""Workflow evidence enrichment with explicit source precedence."""
from __future__ import annotations

from typing import Any, Mapping

from gpa.replay.environment import capture_environment
from gpa.replay.understanding import build_agent_understanding

CLIENT_SECTIONS = ("browser", "screen", "locale", "capture_surface")
HOST_SECTIONS = ("system", "runtime", "input_safety")


def merge_recorded_environment(
    recorded: Mapping[str, Any] | None,
    client: Mapping[str, Any] | None = None,
    *,
    created_at: str = "",
    allow_host_enrichment: bool = True,
    allow_client_enrichment: bool = True,
) -> dict[str, Any]:
    """Preserve the recorded host and fill only client-observable gaps."""
    existing = dict(recorded or {})
    if not existing:
        if not allow_host_enrichment:
            return {}
        environment = capture_environment(client)
        if created_at:
            environment["captured_at"] = created_at
        environment["evidence_sources"] = [
            "host-runtime",
            *(["browser-client"] if client else []),
        ]
        return environment

    observed = capture_environment(client)
    host_filled_fields: list[str] = []
    client_filled_fields: list[str] = []
    sections = (HOST_SECTIONS if allow_host_enrichment else ()) + (
        CLIENT_SECTIONS if client and allow_client_enrichment else ()
    )
    for section in sections:
        current = dict(existing.get(section) or {})
        for key, value in dict(observed.get(section) or {}).items():
            if current.get(key) in (None, "", 0) and value not in (None, "", 0):
                current[key] = value
                destination = host_filled_fields if section in HOST_SECTIONS else client_filled_fields
                destination.append(f"{section}.{key}")
        if current:
            existing[section] = current
    sources = [str(item) for item in (existing.get("evidence_sources") or []) if item]
    new_sources = [
        *(["host-runtime-enrichment"] if host_filled_fields else []),
        *(["browser-client"] if client_filled_fields else []),
    ]
    for source in new_sources:
        if source not in sources:
            sources.append(source)
    if sources:
        existing["evidence_sources"] = sources
    if host_filled_fields:
        existing["host_enriched_fields"] = sorted(set(host_filled_fields))
    if client_filled_fields:
        existing["client_enriched_fields"] = sorted(set(client_filled_fields))
    return existing


def prepare_workflow_evidence(
    workflow,
    client: Mapping[str, Any] | None = None,
    *,
    step_subgraphs: Mapping[str, Any] | None = None,
    allow_host_enrichment: bool = True,
    allow_client_enrichment: bool = True,
):
    workflow.environment = merge_recorded_environment(
        getattr(workflow, "environment", None),
        client,
        created_at=str(getattr(workflow, "created_at", "") or ""),
        allow_host_enrichment=allow_host_enrichment,
        allow_client_enrichment=allow_client_enrichment,
    )
    workflow.understanding = build_agent_understanding(workflow, dict(step_subgraphs or {}))
    return workflow
