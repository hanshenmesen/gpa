"""Capture and compare replay environments without mutating the desktop."""
from __future__ import annotations

import locale
import os
import platform
import time
from datetime import datetime
from typing import Any, Mapping


def capture_environment(client: Mapping[str, Any] | None = None) -> dict[str, Any]:
    client = dict(client or {})
    screen = client.get("screen") if isinstance(client.get("screen"), Mapping) else {}
    browser = client.get("browser") if isinstance(client.get("browser"), Mapping) else {}
    desktop_automation_enabled = _env_bool("GPA_ENABLE_DESKTOP_AUTOMATION", False)
    return {
        "schema": "gpa.environment/v1",
        "captured_at": datetime.now().astimezone().isoformat(),
        "system": {
            "name": platform.system().casefold(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "runtime": {
            "python": platform.python_version(),
            "executable_family": "cpython" if platform.python_implementation() == "CPython" else platform.python_implementation(),
        },
        "locale": {
            "language": client.get("language") or locale.getlocale()[0] or "",
            "timezone": client.get("timezone") or time.tzname[0] or "",
        },
        "screen": {
            "width": _positive_int(screen.get("width")),
            "height": _positive_int(screen.get("height")),
            "pixel_ratio": _positive_float(screen.get("pixel_ratio"), 1.0),
        },
        "browser": {
            "family": str(browser.get("family") or ""),
            "user_agent": str(browser.get("user_agent") or client.get("user_agent") or "")[:1000],
            "viewport_width": _positive_int(browser.get("viewport_width")),
            "viewport_height": _positive_int(browser.get("viewport_height")),
        },
        "input_safety": {
            "desktop_automation_enabled": desktop_automation_enabled,
            "input_watchdog_enabled": (
                desktop_automation_enabled
                and _env_bool("GPA_ENABLE_INPUT_WATCHDOG", True)
            ),
        },
    }


def compare_environments(recorded: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> dict[str, Any]:
    recorded = dict(recorded or {})
    current = dict(current or {})
    recorded_environment_known = _environment_identity_known(recorded)
    current_environment_known = _environment_identity_known(current)
    evidence_complete = recorded_environment_known and current_environment_known
    missing_evidence = [
        field
        for field, present in (
            ("recorded.system.name", recorded_environment_known),
            ("current.system.name", current_environment_known),
        )
        if not present
    ]
    differences: list[dict[str, Any]] = []
    matches: list[str] = []

    def compare(path: str, severity: str = "info") -> None:
        before = _nested(recorded, path)
        after = _nested(current, path)
        if before in (None, "", 0) or after in (None, "", 0):
            return
        if str(before).casefold() == str(after).casefold():
            matches.append(path)
        else:
            differences.append({
                "field": path,
                "recorded": before,
                "current": after,
                "severity": severity,
            })

    compare("system.name", "blocking")
    compare("system.machine", "warn")
    compare("browser.family", "warn")
    compare("locale.language", "info")
    compare("locale.timezone", "info")
    compare("screen.pixel_ratio", "warn")
    compare("runtime.executable_family", "info")

    recorded_input_safety = recorded.get("input_safety")
    current_input_safety = current.get("input_safety")
    if isinstance(recorded_input_safety, Mapping) and isinstance(current_input_safety, Mapping):
        field = "input_safety.desktop_automation_enabled"
        before = recorded_input_safety.get("desktop_automation_enabled")
        after = current_input_safety.get("desktop_automation_enabled")
        if isinstance(before, bool) and isinstance(after, bool):
            if before == after:
                matches.append(field)
            else:
                differences.append({
                    "field": field,
                    "recorded": before,
                    "current": after,
                    "severity": "warn",
                })

    capture_scope = str(_nested(recorded, "capture_surface.scope") or "").strip().casefold()
    scoped_capture = capture_scope in {
        "browser", "browser-tab", "window", "application-window",
    }
    recorded_width = _positive_int(_nested(recorded, "screen.width"))
    recorded_height = _positive_int(_nested(recorded, "screen.height"))
    current_width = _positive_int(_nested(current, "screen.width"))
    current_height = _positive_int(_nested(current, "screen.height"))
    if not scoped_capture and all((recorded_width, recorded_height, current_width, current_height)):
        width_ratio = current_width / recorded_width
        height_ratio = current_height / recorded_height
        delta = max(abs(1 - width_ratio), abs(1 - height_ratio))
        if delta <= 0.1:
            matches.append("screen.dimensions")
        else:
            differences.append({
                "field": "screen.dimensions",
                "recorded": f"{recorded_width}×{recorded_height}",
                "current": f"{current_width}×{current_height}",
                # Screen-size drift is adaptable because replay coordinates are
                # scaled from the recorded frame. Treat it as a warning and
                # reserve blocking for environment changes that cannot be
                # transformed safely (for example, another operating system).
                "severity": "warn",
                "scale_hint": {
                    "x": round(width_ratio, 4),
                    "y": round(height_ratio, 4),
                },
            })

    recorded_viewport_width = _positive_int(_nested(recorded, "browser.viewport_width"))
    recorded_viewport_height = _positive_int(_nested(recorded, "browser.viewport_height"))
    current_viewport_width = _positive_int(_nested(current, "browser.viewport_width"))
    current_viewport_height = _positive_int(_nested(current, "browser.viewport_height"))
    if all((
        recorded_viewport_width,
        recorded_viewport_height,
        current_viewport_width,
        current_viewport_height,
    )):
        width_ratio = current_viewport_width / recorded_viewport_width
        height_ratio = current_viewport_height / recorded_viewport_height
        delta = max(abs(1 - width_ratio), abs(1 - height_ratio))
        if delta <= 0.1:
            matches.append("browser.viewport")
        else:
            differences.append({
                "field": "browser.viewport",
                "recorded": f"{recorded_viewport_width}×{recorded_viewport_height}",
                "current": f"{current_viewport_width}×{current_viewport_height}",
                "severity": "warn",
                "scale_hint": {
                    "x": round(width_ratio, 4),
                    "y": round(height_ratio, 4),
                },
            })

    blocking = sum(item["severity"] == "blocking" for item in differences)
    warnings = sum(item["severity"] == "warn" for item in differences)
    status = (
        "blocked"
        if blocking
        else "unknown"
        if not evidence_complete
        else "degraded"
        if warnings
        else "compatible"
    )
    adaptation_plan = []
    for item in differences:
        field = item["field"]
        if field == "system.name":
            adaptation_plan.append({
                "field": field,
                "strategy": "platform_replan",
                "action": "Do not reuse desktop coordinates or hotkeys; build a target-platform plan before replay.",
                "required": True,
            })
        elif field == "screen.dimensions":
            adaptation_plan.append({
                "field": field,
                "strategy": "scale_then_relocalize",
                "action": "Apply the recorded-to-current scale hint, then confirm the semantic/visual target before acting.",
                "scale_hint": dict(item.get("scale_hint") or {}),
                "required": True,
            })
        elif field == "browser.viewport":
            adaptation_plan.append({
                "field": field,
                "strategy": "responsive_relocalization",
                "action": (
                    "Re-evaluate responsive layout breakpoints, then locate targets by role/text "
                    "instead of reusing viewport-relative coordinates."
                ),
                "scale_hint": dict(item.get("scale_hint") or {}),
                "required": True,
            })
        elif field == "browser.family":
            adaptation_plan.append({
                "field": field,
                "strategy": "semantic_browser_navigation",
                "action": "Prefer direct URLs and text/URL assertions; avoid browser-chrome coordinates.",
                "required": True,
            })
        elif field == "system.machine":
            adaptation_plan.append({
                "field": field,
                "strategy": "dependency_preflight",
                "action": "Recheck native dependencies and visual models for the current architecture.",
                "required": True,
            })
        elif field == "screen.pixel_ratio":
            adaptation_plan.append({
                "field": field,
                "strategy": "pixel_ratio_normalization",
                "action": "Normalize CSS pixels and screenshot pixels before visual matching.",
                "required": True,
            })
        elif field == "input_safety.desktop_automation_enabled":
            adaptation_plan.append({
                "field": field,
                "strategy": "execution_mode_replan",
                "action": (
                    "Use Safe Web when desktop authority is unavailable; otherwise require an "
                    "explicitly armed, focus-guarded desktop replay."
                ),
                "required": True,
            })
        elif field.startswith("locale."):
            adaptation_plan.append({
                "field": field,
                "strategy": "locale_normalization",
                "action": "Normalize language, timezone, date and number formats before comparing output.",
                "required": False,
            })
    return {
        "schema": "gpa.environment-diff/v1",
        "status": status,
        "matches": matches,
        "differences": differences,
        "blocking_count": blocking,
        "warning_count": warnings,
        # Desktop actions may only reuse recorded assumptions when both hosts
        # have a concrete platform identity. Missing evidence is uncertainty,
        # not compatibility. Safe Web execution is decided separately by its
        # own capability gate and does not consume this desktop safety flag.
        "recorded_environment_known": recorded_environment_known,
        "current_environment_known": current_environment_known,
        "evidence_complete": evidence_complete,
        "missing_evidence": missing_evidence,
        "safe_to_attempt": blocking == 0 and evidence_complete,
        "requires_replan": blocking > 0 or not evidence_complete,
        "reusable_assumptions": [
            {"field": field, "action": "Recorded and current values match; reuse this assumption."}
            for field in matches
        ],
        "adaptation_plan": adaptation_plan,
    }


def _nested(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _environment_identity_known(payload: Mapping[str, Any]) -> bool:
    """Return whether an environment has enough identity for desktop reuse."""
    return bool(str(_nested(payload, "system.name") or "").strip())


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}
