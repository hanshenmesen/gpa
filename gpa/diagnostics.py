"""Redacted, user-exportable diagnostics for support and compatibility reports."""
from __future__ import annotations

import io
import json
import platform
import re
import sys
import time
import zipfile
from typing import Any, Mapping

from gpa import __release__, __version__

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:Bearer\s+)?(?:sk|ghp|github_pat|gpa_pair)_[A-Za-z0-9._~-]{8,}|"
    r"\b[A-Fa-f0-9]{64}\b"
)
_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+")
_SAFE_PRIVACY_FLAGS = {
    "raw_logs_included",
    "screenshots_included",
    "recordings_included",
    "environment_variables_included",
    "credentials_included",
}


def redact(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Recursively remove credential-like material and user-home identifiers."""
    if depth > 12:
        return "[TRUNCATED]"
    if key in _SAFE_PRIVACY_FLAGS and isinstance(value, bool):
        return value
    if _SECRET_KEY.search(str(key or "")):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key)[:120]: redact(child, key=str(child_key), depth=depth + 1)
            for child_key, child in list(value.items())[:300]
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in list(value)[:300]]
    if isinstance(value, str):
        text = _HOME_PATH.sub("/Users/[USER]", value)
        text = _SECRET_VALUE.sub("[REDACTED]", text)
        return text[:4000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def diagnostic_report(
    *,
    dependency_health: Mapping[str, Any],
    runtime: Mapping[str, Any],
    crash: Mapping[str, Any],
    recent_runs: list[Mapping[str, Any]],
    workflow_count: int,
    cloud: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a small snapshot without reading environment variables or raw media."""
    runs = []
    for item in recent_runs[:20]:
        runs.append({
            "run_id": item.get("run_id"),
            "workflow_id": item.get("workflow_id"),
            "status": item.get("status"),
            "success": item.get("success"),
            "failure_code": item.get("failure_code") or item.get("reason_code"),
            "failed_step": item.get("failed_step") or item.get("current_step"),
            "updated_at": item.get("updated_at") or item.get("completed_at"),
            "recovery_attempts": item.get("recovery_attempts"),
        })
    cloud_state = dict(cloud or {})
    report = {
        "schema": "gpa.support-diagnostics/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "product": {"version": __version__, "release": __release__},
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "library": {"workflow_count": max(0, int(workflow_count))},
        "runtime": dict(runtime),
        "dependency_health": dict(dependency_health),
        "crash_recovery": dict(crash),
        "recent_runs": runs,
        "cloud": {
            "status": cloud_state.get("status") or "disconnected",
            "last_sync_at": cloud_state.get("last_sync_at") or 0,
            "has_error": bool(cloud_state.get("last_error")),
            "inbox_count": len(cloud_state.get("inbox") or []),
        },
        "privacy": {
            "raw_logs_included": False,
            "screenshots_included": False,
            "recordings_included": False,
            "environment_variables_included": False,
            "credentials_included": False,
        },
    }
    return redact(report)


def support_bundle(report: Mapping[str, Any]) -> bytes:
    """Return a deterministic ZIP containing the redacted JSON and instructions."""
    sanitized = redact(dict(report))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("gpa-diagnostics.json", date_time=(2026, 1, 1, 0, 0, 0))
        info.external_attr = 0o600 << 16
        archive.writestr(info, json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n")
        readme = zipfile.ZipInfo("README.txt", date_time=(2026, 1, 1, 0, 0, 0))
        readme.external_attr = 0o600 << 16
        archive.writestr(
            readme,
            "GPA support diagnostics\n\n"
            "This bundle excludes screenshots, recordings, raw logs, environment variables, "
            "API keys and account tokens. Review gpa-diagnostics.json before sharing.\n",
        )
    return output.getvalue()


__all__ = ["diagnostic_report", "redact", "support_bundle"]
