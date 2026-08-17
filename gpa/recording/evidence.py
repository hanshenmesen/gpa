"""Attach verified, privacy-reviewed recording evidence to a Workflow.

The helper is intentionally separate from the long-lived Web server. Video
decoding therefore happens in a short-lived process/CLI and cannot take down
the GPA service if a native codec rejects the input.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gpa.community.media_probe import probe_recording
from gpa.storage import WorkflowStorage

_SCOPE_ALIASES = {
    "browser": "browser-tab",
    "browser-tab-only": "browser-tab",
    "tab": "browser-tab",
    "single-window": "window",
    "app-window": "application-window",
}
_SAFE_SCOPES = {"browser-tab", "window", "application-window", "public-web-evidence"}
_MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}


def _normalized_scope(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    return _SCOPE_ALIASES.get(normalized, normalized)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_name(directory: Path, suffix: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = directory / f"recording.archive-{timestamp}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"recording.archive-{timestamp}-{counter}{suffix}"
        counter += 1
    return candidate


def _validated_source_trace(
    source_trace_path: str | Path | None,
    *,
    workflow_id: str,
    source_run_id: str,
) -> tuple[Path | None, dict[str, Any]]:
    if source_trace_path is None:
        return None, {}
    path = Path(source_trace_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source trace not found: {path}")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Source trace exceeds 2 MiB.")
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Source trace must be valid UTF-8 JSON.") from exc
    if not isinstance(trace, dict) or trace.get("schema") != "gpa.safe-web-source-trace/v1":
        raise ValueError("Source trace schema is not supported.")
    if trace.get("workflow_id") != workflow_id or trace.get("source_run_id") != source_run_id:
        raise ValueError("Source trace identity does not match the Workflow and source run.")
    pages = trace.get("pages") if isinstance(trace.get("pages"), list) else []
    if trace.get("run_success") is not True or not pages or int(trace.get("page_count") or 0) != len(pages):
        raise ValueError("Source trace must describe a successful run with verified pages.")
    for page in pages:
        parsed = urlparse(str((page or {}).get("final_url") or (page or {}).get("url") or ""))
        digest = str((page or {}).get("content_sha256") or "")
        if (
            not isinstance(page, dict)
            or page.get("verified") is not True
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest.casefold())
        ):
            raise ValueError("Source trace contains an invalid or unverified page entry.")
    return path, trace


def attach_recording_evidence(
    workflow_id: str,
    recording_path: str | Path,
    *,
    storage: WorkflowStorage | None = None,
    capture_scope: str,
    capture_method: str,
    privacy_reviewed: bool,
    privacy_note: str,
    source_run_id: str = "",
    browser_family: str = "",
    source_trace_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify, preserve and attach a real recording to an existing Workflow."""
    source = Path(recording_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Recording not found: {source}")
    mime_type = _MEDIA_TYPES.get(source.suffix.casefold())
    if mime_type is None:
        raise ValueError("Recording must be an MP4 or WebM file.")
    scope = _normalized_scope(capture_scope)
    if scope not in _SAFE_SCOPES:
        raise ValueError(
            "Transferable evidence must capture one browser tab or one application window, "
            "or provide a verified public-Web evidence trace."
        )
    if privacy_reviewed is not True:
        raise ValueError("An explicit first/middle/last-frame privacy review is required.")
    note = str(privacy_note or "").strip()
    if not note:
        raise ValueError("A privacy review note is required.")
    source_run_id = str(source_run_id or "").strip()
    trace_source, source_trace = _validated_source_trace(
        source_trace_path,
        workflow_id=str(workflow_id),
        source_run_id=source_run_id,
    )
    if scope == "public-web-evidence" and trace_source is None:
        raise ValueError("Public Web evidence video requires its machine-readable source trace.")

    media_probe = probe_recording(source)
    if media_probe.get("verified") is not True:
        raise ValueError(
            "Recording did not pass real-media decoding: "
            + str(media_probe.get("error") or media_probe.get("status") or "invalid")
        )

    repository = storage or WorkflowStorage()
    workflow, subgraphs = repository.load(workflow_id)
    workflow_directory = repository.workflows_dir / workflow.workflow_id
    destination_name = f"recording{source.suffix.casefold()}"
    destination = workflow_directory / destination_name
    previous = dict((workflow.artifacts or {}).get("recording") or {})
    previous_path = workflow_directory / str(previous.get("path") or "")
    archive_path: Path | None = None
    source_digest = _sha256(source)

    if previous_path.is_file() and _sha256(previous_path) != source_digest:
        archive_path = _archive_name(workflow_directory, previous_path.suffix.casefold())
        shutil.copy2(previous_path, archive_path)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_name}.", dir=workflow_directory
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    frame_count = int(media_probe.get("frame_count") or 0)
    reviewed_positions = sorted({0, max(0, frame_count // 2), max(0, frame_count - 3)})
    captured_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    recording = {
        "kind": (
            "source-evidence-video" if scope == "public-web-evidence" else "screen-recording"
        ),
        "path": destination_name,
        "mime_type": mime_type,
        "bytes": destination.stat().st_size,
        "sha256": source_digest,
        "duration_seconds": float(media_probe.get("duration_seconds") or 0),
        "width": int(media_probe.get("width") or 0),
        "height": int(media_probe.get("height") or 0),
        "fps": float(media_probe.get("fps") or 0),
        "frame_count": frame_count,
        "decoded_sample_count": int(media_probe.get("decoded_sample_count") or 0),
        "capture_scope": scope,
        "capture_method": str(capture_method or "").strip() or "unknown",
        "captured_at": captured_at,
        "source_run_id": source_run_id,
        "evidence_type": (
            "safe-web-source-evidence" if scope == "public-web-evidence" else "screen-recording"
        ),
        "privacy_review": {
            "status": "passed",
            "other_apps_visible": False,
            "scope_confirmed": scope,
            "samples_reviewed": reviewed_positions,
            "review_method": "first-middle-last-frame",
            "reviewed_at": captured_at,
            "note": note,
        },
    }
    artifacts = {**dict(workflow.artifacts or {}), "recording": recording}
    trace_destination = workflow_directory / "source_trace.json"
    if trace_source is not None:
        trace_temporary = trace_destination.with_name(f".{trace_destination.name}.tmp")
        shutil.copyfile(trace_source, trace_temporary)
        os.replace(trace_temporary, trace_destination)
        artifacts["source_trace"] = {
            "kind": "safe-web-source-trace",
            "path": trace_destination.name,
            "mime_type": "application/json",
            "bytes": trace_destination.stat().st_size,
            "sha256": _sha256(trace_destination),
            "schema": str(source_trace.get("schema") or ""),
            "source_run_id": source_run_id,
            "page_count": int(source_trace.get("page_count") or 0),
            "verified_page_count": sum(
                item.get("verified") is True for item in source_trace.get("pages") or []
            ),
        }
        recording["source_trace_path"] = trace_destination.name
        recording["source_page_count"] = int(source_trace.get("page_count") or 0)
    workflow.artifacts = artifacts
    environment = dict(workflow.environment or {})
    browser = dict(environment.get("browser") or {})
    if browser_family:
        browser["family"] = str(browser_family)
    if scope in {"browser-tab", "public-web-evidence"}:
        browser.update({
            "viewport_width": recording["width"],
            "viewport_height": recording["height"],
        })
    if browser:
        environment["browser"] = browser
    environment["capture_surface"] = {
        "scope": scope,
        "method": recording["capture_method"],
        "width": recording["width"],
        "height": recording["height"],
        "source": (
            "safe-web-source-trace" if scope == "public-web-evidence"
            else "decoded-recording-frames"
        ),
    }
    workflow.environment = environment
    repository.save(workflow, subgraphs)
    return {
        "workflow_id": workflow.workflow_id,
        "recording": recording,
        "media_probe": media_probe,
        "source_trace": dict(artifacts.get("source_trace") or {}),
        "archived_previous_recording": str(archive_path) if archive_path else "",
    }


__all__ = ["attach_recording_evidence"]
