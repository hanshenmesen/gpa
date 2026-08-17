"""Durable, auditable human-intervention checkpoints."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

DECISIONS = {"approve", "edit", "reject"}


def create_checkpoint(
    *,
    run_id: str,
    workflow_id: str,
    intervention: Mapping[str, Any],
    completed_steps: list[int],
    gate_decision_id: str = "",
) -> dict[str, Any]:
    payload = {
        "schema": "gpa.replay-checkpoint/v1",
        "checkpoint_id": "",
        "run_id": str(run_id),
        "workflow_id": str(workflow_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "awaiting_review",
        "resume_from_step": max(1, int(intervention.get("step") or 1)),
        "completed_steps": sorted({int(item) for item in completed_steps if int(item) > 0}),
        "gate_decision_id": str(gate_decision_id or ""),
        "intervention": dict(intervention),
        "decision": None,
    }
    material = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    payload["checkpoint_id"] = hashlib.sha256(material.encode()).hexdigest()[:24]
    return payload


def decide_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    decision: str,
    feedback: str = "",
    patch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = dict(checkpoint or {})
    if current.get("schema") != "gpa.replay-checkpoint/v1":
        raise ValueError("Unsupported Replay checkpoint schema.")
    if current.get("status") != "awaiting_review":
        raise ValueError("Replay checkpoint has already been decided.")
    normalized = str(decision or "").strip().casefold()
    if normalized not in DECISIONS:
        raise ValueError("Checkpoint decision must be approve, edit, or reject.")
    patch = dict(patch or {})
    if normalized == "edit" and not patch:
        raise ValueError("An edit decision requires a versioned patch.")
    current["status"] = "resumable" if normalized in {"approve", "edit"} else "rejected"
    current["decision"] = {
        "kind": normalized,
        "feedback": str(feedback or "")[:4000],
        "patch": patch,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    return current


def write_checkpoint(path: str | Path, checkpoint: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(checkpoint), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = ["create_checkpoint", "decide_checkpoint", "write_checkpoint"]
