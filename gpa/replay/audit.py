"""Isolated cross-Agent reproduction audit for portable GPA packages."""
from __future__ import annotations

import hashlib
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from gpa.community.media_probe import probe_recording
from gpa.community.package import import_workflow_package, inspect_workflow_package
from gpa.execution.safe_web import SafeWebRunner, safe_web_compatibility
from gpa.replay.environment import capture_environment, compare_environments
from gpa.replay.understanding import build_reproduction_contract
from gpa.storage.workflow import WorkflowStorage


def audit_reproduction_package(
    package_path: str | Path,
    *,
    target_environment: Mapping[str, Any] | None = None,
    workspace: str | Path | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    """Import a package into an isolated repository and verify it end to end.

    The audit never imports into the user's normal workflow directory. Safe Web
    execution is attempted only when the imported workflow is deterministic,
    public-Web-only, and free of desktop input.
    """
    source = Path(package_path).resolve()
    manifest = inspect_workflow_package(source)
    package_sha256 = _sha256(source)
    supplied_target = dict(target_environment or {})
    effective_target = supplied_target or capture_environment()
    effective_target.setdefault("schema", "gpa.environment/v1")

    temporary = tempfile.TemporaryDirectory(prefix="gpa-other-agent-") if workspace is None else None
    workspace_context = temporary if temporary is not None else nullcontext(str(Path(workspace).resolve()))
    with workspace_context as workspace_value:
        isolated_root = Path(workspace_value) / "workflows"
        storage = WorkflowStorage(isolated_root)
        imported = import_workflow_package(source, storage=storage)
        workflow, subgraphs = storage.load(imported.workflow_id)

        recording_report = _verify_imported_recording(workflow)
        environment_diff = compare_environments(workflow.environment, effective_target)
        contract = build_reproduction_contract(
            step_count=len(workflow.steps),
            environment=dict(workflow.environment or {}),
            understanding=dict(workflow.understanding or {}),
            artifacts=dict(workflow.artifacts or {}),
            environment_diff=environment_diff,
            recording_verified=recording_report["verified"],
        )
        compatibility = safe_web_compatibility(workflow)
        execution = _execution_report(workflow, compatibility, execute=execute)
        understanding = dict(workflow.understanding or {})
        interaction = dict(understanding.get("interaction_profile") or {})
        criteria = [
            dict(item) for item in (understanding.get("success_criteria") or [])
            if isinstance(item, dict)
        ]
        cross_agent_reproducible = bool(
            contract.get("publishable_as_verified")
            and recording_report["verified"]
            and execution.get("success") is True
        )
        status = "passed" if cross_agent_reproducible else (
            "not_run" if not execution.get("attempted") else "failed"
        )
        return {
            "schema": "gpa.isolated-reproduction-audit/v1",
            "status": status,
            "cross_agent_reproducible": cross_agent_reproducible,
            "package": {
                "path_name": source.name,
                "sha256": package_sha256,
                "bytes": source.stat().st_size,
                "format": manifest.get("format"),
                "format_version": manifest.get("format_version"),
            },
            "isolation": {
                "separate_workflow_repository": True,
                "source_workflow_directory_reused": False,
                "imported_workflow_id": imported.workflow_id,
                "step_count": len(workflow.steps),
                "subgraph_count": len(subgraphs),
            },
            "recording": recording_report,
            "recorded_environment": dict(workflow.environment or {}),
            "target_environment": effective_target,
            "environment_diff": environment_diff,
            "agent_understanding": {
                "schema": understanding.get("schema"),
                "goal": understanding.get("goal"),
                "summary": understanding.get("summary"),
                "action_counts": dict(interaction.get("action_counts") or {}),
                "required_environment": dict(understanding.get("required_environment") or {}),
                "success_criteria_count": len(criteria),
                "risk_controls": dict(understanding.get("risk_controls") or {}),
            },
            "reproduction_contract": contract,
            "execution": execution,
        }


def _verify_imported_recording(workflow) -> dict[str, Any]:
    metadata = dict((workflow.artifacts or {}).get("recording") or {})
    name = str(metadata.get("path") or "")
    if name not in {"recording.mp4", "recording.webm"}:
        return {"verified": False, "error": "No supported recording artifact."}
    path = workflow.storage_dir / name
    if not path.is_file():
        return {"verified": False, "error": "Imported recording file is missing."}
    digest = _sha256(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(16)
    container_valid = (
        len(header) >= 8 and header[4:8] == b"ftyp"
        if name.endswith(".mp4") else header.startswith(b"\x1a\x45\xdf\xa3")
    )
    structure_verified = bool(
        digest == str(metadata.get("sha256") or "")
        and size == int(metadata.get("bytes") or -1)
        and container_valid
    )
    media_probe = probe_recording(path) if structure_verified else {
        "schema": "gpa.recording-media-probe/v1",
        "status": "skipped",
        "verified": False,
        "error": "Recording structure verification failed before media decoding.",
    }
    verified = bool(structure_verified and media_probe.get("verified"))
    source_run_id = metadata.get("source_run_id") or metadata.get("run_id")
    return {
        "verified": verified,
        "structure_verified": structure_verified,
        "media_verified": bool(media_probe.get("verified")),
        "media_probe": media_probe,
        "path_name": name,
        "mime_type": metadata.get("mime_type"),
        "sha256": digest,
        "bytes": size,
        "duration_seconds": metadata.get("duration_seconds"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "container_valid": container_valid,
        "source_run_id": source_run_id,
        # Keep the legacy key for packages produced before source_run_id was
        # named explicitly.
        "run_id": source_run_id,
    }


def _execution_report(workflow, compatibility: dict, *, execute: bool) -> dict[str, Any]:
    if not execute:
        return {
            "attempted": False,
            "success": None,
            "mode": "safe_web" if compatibility.get("runnable") else "agent_first",
            "reason": "Execution disabled for this audit.",
            "safe_web": compatibility,
        }
    if not compatibility.get("runnable"):
        return {
            "attempted": False,
            "success": None,
            "mode": "agent_first",
            "reason": compatibility.get("reason"),
            "safe_web": compatibility,
        }
    started = time.monotonic()
    result = SafeWebRunner(workflow).run()
    sources = sorted({
        str(item.evidence_source or "")
        for item in result.step_results if item.evidence_source
    })
    verified = sum(item.postcondition_verified is True for item in result.step_results)
    return {
        "attempted": True,
        "success": result.success,
        "mode": result.execution_mode,
        "desktop_input": False,
        "steps_run": result.n_steps,
        "steps_failed": result.n_failed,
        "semantic_assertions_verified": verified,
        "evidence_sources": sources,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "error": result.error,
        "safe_web": compatibility,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["audit_reproduction_package"]
