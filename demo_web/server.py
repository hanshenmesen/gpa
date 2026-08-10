"""Local web UI server for GPA."""
from __future__ import annotations

import copy
import base64
import binascii
import json
import importlib.util
import os
import pathlib
import re
import signal
import shutil
import sys
import threading
import time
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

PORT = 8765
CLIENT_HEARTBEAT_TIMEOUT = 20.0
REPLAY_ARM_TTL_SECONDS = 15.0
DESKTOP_AUTOMATION_ENV = "GPA_ENABLE_DESKTOP_AUTOMATION"
DESKTOP_AUTOMATION_ENABLED = os.environ.get(DESKTOP_AUTOMATION_ENV) == "1"
INPUT_WATCHDOG_ENV = "GPA_ENABLE_INPUT_WATCHDOG"
if DESKTOP_AUTOMATION_ENABLED:
    os.environ.setdefault(INPUT_WATCHDOG_ENV, "1")
PRELOAD_VISUAL_MODELS_ENV = "GPA_PRELOAD_VISUAL_MODELS"
PRELOAD_VISUAL_MODELS_ENABLED = os.environ.get(PRELOAD_VISUAL_MODELS_ENV, "1") != "0"
VISUAL_WARMUP_COMPONENT_TIMEOUT = float(os.environ.get("GPA_VISUAL_WARMUP_COMPONENT_TIMEOUT", "45"))
REQUIRE_VISUAL_WARMUP_ENV = "GPA_REQUIRE_VISUAL_WARMUP"
REQUIRE_VISUAL_WARMUP_READY = os.environ.get(REQUIRE_VISUAL_WARMUP_ENV, "1") != "0"
REPLAY_AGENT_FIRST_ENV = "GPA_REPLAY_AGENT_FIRST"
REPLAY_AGENT_FIRST = os.environ.get(REPLAY_AGENT_FIRST_ENV, "0").strip().lower() in {"1", "true", "yes", "y"}
HEALTH_CACHE_TTL_SECONDS = 10.0
ROOT = pathlib.Path(__file__).parent
PROJECT_ROOT = ROOT.parent
WORKFLOWS_DIR = PROJECT_ROOT / "storage" / "workflows"
RUNS_DIR = PROJECT_ROOT / "storage" / "runs"
REPLAY_SPACES_DIR = PROJECT_ROOT / "storage" / "replay_spaces"
COMMUNITY_DIR = PROJECT_ROOT / "storage" / "community"
COMMUNITY_MAX_PACKAGE_BYTES = 64 * 1024 * 1024
COMMUNITY_MAX_JSON_BYTES = ((COMMUNITY_MAX_PACKAGE_BYTES + 2) // 3) * 4 + 256 * 1024
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STATE_LOCK = threading.Lock()
HEALTH_CACHE_LOCK = threading.Lock()
REPLAY_SERVICE_LOCK = threading.Lock()
REPLAY_SERVICE_CACHE = {"key": None, "value": None}
HEALTH_CACHE = {"expires_at": 0.0, "value": None}
SHUTDOWN_EVENT = threading.Event()
STATE = {
    "recorder": None,
    "recording": {
        "status": "idle",
        "active": False,
        "started_at": None,
        "finished_at": None,
        "workflow_id": "",
        "task_description": "",
        "event_count": 0,
        "error": "",
    },
    "last_recording": None,
    "run": {
        "active": False,
        "status": "idle",
        "run_id": "",
        "workflow_id": "",
        "started_at": None,
        "finished_at": None,
        "success": None,
        "error": "",
        "steps_run": 0,
        "steps_failed": 0,
        "current_step": None,
        "total_steps": 0,
        "countdown_remaining": 0,
        "max_runtime_seconds": 300,
        "elapsed_seconds": 0,
        "stop_requested": False,
    },
    "preview": None,
    "run_stop_event": None,
    "run_thread": None,
    "run_started_monotonic": None,
    "replay_arm": {
        "token": "",
        "workflow_id": "",
        "client_id": "",
        "expires_at": 0.0,
        "issued_at": "",
    },
    "client": {
        "id": "",
        "last_seen_monotonic": 0.0,
        "last_seen_at": "",
    },
    "visual_warmup": {
        "enabled": PRELOAD_VISUAL_MODELS_ENABLED,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": 0.0,
        "loaded": [],
        "errors": [],
    },
    "logs": [],
}

DEPENDENCIES = {
    "record": [
        ("PIL", "Pillow"),
        ("mss", "mss"),
        ("pynput", "pynput"),
    ],
    "build": [
        ("PIL", "Pillow"),
        ("numpy", "numpy"),
        ("openai", "openai"),
    ],
    "replay": [
        ("PIL", "Pillow"),
        ("mss", "mss"),
        ("pyautogui", "pyautogui"),
        ("rapidfuzz", "rapidfuzz"),
        ("scipy", "scipy"),
        ("openai", "openai"),
    ],
}

if sys.platform == "darwin":
    DEPENDENCIES["record"].extend([
        ("Quartz", "pyobjc-framework-Quartz"),
        ("AppKit", "pyobjc-framework-Cocoa"),
    ])
    DEPENDENCIES["replay"].extend([
        ("Quartz", "pyobjc-framework-Quartz"),
        ("AppKit", "pyobjc-framework-Cocoa"),
    ])

OPTIONAL_VISUAL_DEPENDENCIES = [
    ("torch", "torch"),
    ("ultralytics", "ultralytics"),
    ("huggingface_hub", "huggingface-hub"),
    ("transformers", "transformers"),
    ("sentence_transformers", "sentence-transformers"),
]


class MissingDependencyError(RuntimeError):
    def __init__(self, group: str, missing: list[dict]):
        self.group = group
        self.missing = missing
        packages = ", ".join(item["package"] for item in missing)
        super().__init__(f"Missing dependencies for {group}: {packages}")


class RuntimePermissionError(RuntimeError):
    def __init__(self, group: str, issues: list[dict]):
        self.group = group
        self.issues = issues
        labels = ", ".join(item["label"] for item in issues)
        super().__init__(f"Missing macOS permissions for {group}: {labels}")


class PayloadTooLargeError(ValueError):
    pass


def _log(message: str, level: str = "info") -> None:
    with STATE_LOCK:
        STATE["logs"].insert(
            0,
            {
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            },
        )
        del STATE["logs"][80:]


def _audit_event(event: str, **fields) -> None:
    try:
        audit_dir = PROJECT_ROOT / "storage"
        audit_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            **fields,
        }
        with open(audit_dir / "server_events.jsonl", "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _json_response(handler: BaseHTTPRequestHandler, payload, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    _send_local_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(data)


def _binary_response(
    handler: BaseHTTPRequestHandler,
    data: bytes,
    *,
    content_type: str,
    filename: str,
) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    _send_local_cors_headers(handler)
    handler.send_header("Access-Control-Expose-Headers", "Content-Disposition, Content-Length")
    handler.end_headers()
    handler.wfile.write(data)


def _error(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    _json_response(handler, {"ok": False, "error": message}, status)


def _send_local_cors_headers(handler: BaseHTTPRequestHandler) -> None:
    headers = getattr(handler, "headers", None)
    origin = str(headers.get("Origin") or "").strip() if hasattr(headers, "get") else ""
    if origin in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _missing_dependencies(group: str) -> list[dict]:
    missing = [
        {"module": module, "package": package}
        for module, package in DEPENDENCIES[group]
        if not _module_available(module)
    ]
    if group in {"build", "replay"}:
        try:
            from gpa.config import LLM_API_KEY
            if not LLM_API_KEY:
                missing.append({"module": "GPA_LLM_API_KEY", "package": "local .env"})
        except Exception:
            missing.append({"module": "GPA_LLM_API_KEY", "package": "local .env"})
    return missing


def _dependency_message(group: str, missing: list[dict]) -> str:
    packages = " ".join(item["package"] for item in missing if item["package"] != "local .env")
    modules = ", ".join(item["module"] for item in missing)
    install_hint = f" Install packages with: {sys.executable} -m pip install --user {packages}." if packages else ""
    key_hint = " Configure GPA_LLM_API_KEY in .env." if any(item["module"] == "GPA_LLM_API_KEY" for item in missing) else ""
    return f"{group} is not ready. Missing runtime requirement(s): {modules}.{install_hint}{key_hint}"


def _ensure_dependencies(group: str) -> None:
    missing = _missing_dependencies(group)
    if missing:
        raise MissingDependencyError(group, missing)


def _set_visual_warmup_status(status: str, **updates) -> None:
    with STATE_LOCK:
        STATE["visual_warmup"].update({"status": status, **updates})


def _visual_warmup_payload() -> dict:
    with STATE_LOCK:
        return dict(STATE["visual_warmup"])


def _warm_visual_models() -> None:
    if not PRELOAD_VISUAL_MODELS_ENABLED:
        _set_visual_warmup_status("disabled", enabled=False)
        return

    started = time.monotonic()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    _set_visual_warmup_status(
        "warming",
        enabled=True,
        started_at=started_at,
        finished_at=None,
        duration_seconds=0.0,
        loaded=[],
        errors=[],
    )
    _log("Visual model warmup started.")

    loaded: list[str] = []
    errors: list[dict] = []
    steps = [
        ("yolo", "_load_yolo"),
        ("ocr", "_load_ocr"),
        ("clip", "_load_clip"),
        ("e5", "_load_e5"),
    ]
    try:
        from gpa.core import ui_parser
    except Exception as exc:
        errors.append({"component": "ui_parser", "error": str(exc)})
    else:
        for label, attr in steps:
            try:
                _run_with_timeout(
                    getattr(ui_parser, attr),
                    timeout_seconds=VISUAL_WARMUP_COMPONENT_TIMEOUT,
                    label=label,
                )
                loaded.append(label)
                _set_visual_warmup_status(
                    "warming",
                    loaded=list(loaded),
                    errors=list(errors),
                    duration_seconds=round(time.monotonic() - started, 3),
                )
            except Exception as exc:
                errors.append({"component": label, "error": str(exc)})
                _log(f"Visual model warmup skipped {label}: {exc}", "warn")

    status = "ready" if not errors else ("partial" if loaded else "failed")
    finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    _set_visual_warmup_status(
        status,
        loaded=loaded,
        errors=errors,
        finished_at=finished_at,
        duration_seconds=round(time.monotonic() - started, 3),
    )
    if status == "ready":
        _log(f"Visual model warmup ready in {round(time.monotonic() - started, 1)}s.")
    else:
        _log(f"Visual model warmup {status}: {len(errors)} issue(s).", "warn")


def _run_with_timeout(func, *, timeout_seconds: float, label: str):
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["value"] = func()
        except Exception as exc:
            result["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=max(0.0, timeout_seconds))
    if worker.is_alive():
        raise TimeoutError(f"{label} warmup exceeded {timeout_seconds:g}s")
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result.get("value")


def _start_visual_warmup() -> None:
    if not PRELOAD_VISUAL_MODELS_ENABLED:
        _set_visual_warmup_status("disabled", enabled=False)
        return
    with STATE_LOCK:
        status = STATE["visual_warmup"].get("status")
    if status in {"warming", "ready", "partial"}:
        return
    threading.Thread(target=_warm_visual_models, daemon=True).start()


def _ensure_visual_warmup_ready() -> None:
    if not PRELOAD_VISUAL_MODELS_ENABLED:
        _set_visual_warmup_status("disabled", enabled=False)
        return
    _warm_visual_models()
    warmup = _visual_warmup_payload()
    if REQUIRE_VISUAL_WARMUP_READY and warmup.get("status") != "ready":
        errors = warmup.get("errors") or []
        details = "; ".join(
            f"{item.get('component')}: {item.get('error')}"
            for item in errors
            if isinstance(item, dict)
        )
        raise RuntimeError(
            "Visual model warmup did not complete; refusing to start server. "
            f"status={warmup.get('status')}, loaded={warmup.get('loaded')}. "
            f"{details} "
            f"Set {REQUIRE_VISUAL_WARMUP_ENV}=0 to allow partial startup."
        )


def _permission_item(label: str, ready, message: str = "") -> dict:
    status = "unknown" if ready is None else ("ready" if ready else "blocked")
    return {"label": label, "ready": ready, "status": status, "message": message}


def _check_accessibility_permission() -> dict:
    if sys.platform != "darwin":
        return _permission_item("Accessibility", True)
    try:
        from ApplicationServices import AXIsProcessTrusted
        ready = bool(AXIsProcessTrusted())
        return _permission_item(
            "Accessibility",
            ready,
            "" if ready else "Enable Accessibility for the terminal/Python host that runs GPA.",
        )
    except Exception as exc:
        return _permission_item("Accessibility", None, f"Could not check Accessibility: {exc}")


def _check_screen_recording_permission() -> dict:
    if sys.platform != "darwin":
        return _permission_item("Screen Recording", True)
    try:
        import Quartz
        if hasattr(Quartz, "CGPreflightScreenCaptureAccess"):
            ready = bool(Quartz.CGPreflightScreenCaptureAccess())
            return _permission_item(
                "Screen Recording",
                ready,
                "" if ready else "Enable Screen Recording for the terminal/Python host that runs GPA.",
            )
    except Exception:
        pass
    try:
        import mss
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            sct.grab({"left": monitor["left"], "top": monitor["top"], "width": 1, "height": 1})
        return _permission_item("Screen Recording", True)
    except Exception as exc:
        return _permission_item("Screen Recording", False, f"Screen capture failed: {exc}")


def _check_input_monitoring_permission() -> dict:
    if sys.platform != "darwin":
        return _permission_item("Input Monitoring", True)
    try:
        from pynput import keyboard
        listener = keyboard.Listener(on_press=lambda key: None)
        listener.start()
        listener.stop()
        return _permission_item(
            "Input Monitoring",
            True,
            "Keyboard listener can start. If recorded keystrokes are missing, re-enable Input Monitoring for the Python/Codex host.",
        )
    except Exception as exc:
        return _permission_item("Input Monitoring", False, f"Input listener failed: {exc}")


def _permission_health() -> dict:
    items = {
        "accessibility": _check_accessibility_permission(),
        "screen_recording": _check_screen_recording_permission(),
        "input_monitoring": _check_input_monitoring_permission(),
    }
    return {
        "ok": all(item["ready"] is not False for item in items.values()),
        "items": items,
    }


def _blocking_permission_issues(group: str) -> list[dict]:
    permissions = _permission_health()["items"]
    keys = {
        "record": ["screen_recording", "input_monitoring"],
        "build": ["screen_recording"],
        "replay": ["accessibility", "screen_recording"],
    }.get(group, [])
    return [permissions[key] for key in keys if permissions[key]["ready"] is False]


def _permission_message(group: str, issues: list[dict]) -> str:
    labels = ", ".join(item["label"] for item in issues)
    details = " ".join(item["message"] for item in issues if item["message"])
    return f"{group} is blocked by macOS permission(s): {labels}. {details}".strip()


def _ensure_permissions(group: str) -> None:
    issues = _blocking_permission_issues(group)
    if issues:
        raise RuntimePermissionError(group, issues)


def _dependency_health() -> dict:
    groups = {}
    for group in DEPENDENCIES:
        missing = _missing_dependencies(group)
        groups[group] = {
            "ready": not missing,
            "missing": missing,
            "message": "" if not missing else _dependency_message(group, missing),
        }

    optional_missing = [
        {"module": module, "package": package}
        for module, package in OPTIONAL_VISUAL_DEPENDENCIES
        if not _module_available(module)
    ]
    if not (_module_available("ocrmac") or _module_available("easyocr")):
        optional_missing.append({"module": "ocrmac|easyocr", "package": "ocrmac or easyocr"})

    return {
        "ok": all(item["ready"] for item in groups.values()),
        "desktop_automation": {
            "enabled": DESKTOP_AUTOMATION_ENABLED,
            "env": DESKTOP_AUTOMATION_ENV,
            "message": (
                "Desktop automation is enabled."
                if DESKTOP_AUTOMATION_ENABLED
                else f"Desktop automation is disabled. Set {DESKTOP_AUTOMATION_ENV}=1 before starting the server to allow recording or replay."
            ),
        },
        "groups": groups,
        "optional_visual": {
            "ready": not optional_missing,
            "missing": optional_missing,
            "message": "" if not optional_missing else (
                "Visual model dependencies are incomplete; recording can start, "
                "but click-step subgraphs may be skipped until these packages are installed."
            ),
        },
        "permissions": _permission_health(),
        "python_executable": sys.executable,
    }


def _cached_dependency_health() -> dict:
    now = time.monotonic()
    with HEALTH_CACHE_LOCK:
        cached = HEALTH_CACHE.get("value")
        if cached is not None and now < float(HEALTH_CACHE.get("expires_at") or 0.0):
            return copy.deepcopy(cached)
    value = _dependency_health()
    with HEALTH_CACHE_LOCK:
        HEALTH_CACHE["value"] = value
        HEALTH_CACHE["expires_at"] = time.monotonic() + HEALTH_CACHE_TTL_SECONDS
    return copy.deepcopy(value)


def _handle_missing_dependency(handler: BaseHTTPRequestHandler, exc: MissingDependencyError) -> None:
    message = _dependency_message(exc.group, exc.missing)
    _log(message, "error")
    _json_response(
        handler,
        {
            "ok": False,
            "error": message,
            "missing_dependencies": exc.missing,
            "dependency_group": exc.group,
        },
        424,
    )


def _handle_permission_error(handler: BaseHTTPRequestHandler, exc: RuntimePermissionError) -> None:
    message = _permission_message(exc.group, exc.issues)
    _log(message, "error")
    _json_response(
        handler,
        {
            "ok": False,
            "error": message,
            "permission_issues": exc.issues,
            "permission_group": exc.group,
        },
        424,
    )


def _client_connected(now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    with STATE_LOCK:
        last_seen = float(STATE["client"].get("last_seen_monotonic") or 0.0)
    return last_seen > 0 and now - last_seen <= CLIENT_HEARTBEAT_TIMEOUT


def _client_status() -> dict:
    now = time.monotonic()
    with STATE_LOCK:
        client = dict(STATE["client"])
    last_seen = float(client.get("last_seen_monotonic") or 0.0)
    connected = last_seen > 0 and now - last_seen <= CLIENT_HEARTBEAT_TIMEOUT
    return {
        "id": client.get("id", ""),
        "connected": connected,
        "last_seen_at": client.get("last_seen_at", ""),
        "seconds_since_seen": round(now - last_seen, 2) if last_seen else None,
        "timeout_seconds": CLIENT_HEARTBEAT_TIMEOUT,
    }


def _mark_client_seen(client_id: str = "") -> dict:
    now = time.monotonic()
    seen_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with STATE_LOCK:
        if client_id:
            STATE["client"]["id"] = client_id
        elif not STATE["client"].get("id"):
            STATE["client"]["id"] = str(uuid.uuid4())
        STATE["client"]["last_seen_monotonic"] = now
        STATE["client"]["last_seen_at"] = seen_at
    return _client_status()


def _client_disconnect() -> None:
    with STATE_LOCK:
        STATE["client"]["last_seen_monotonic"] = 0.0


def _client_heartbeat(handler: BaseHTTPRequestHandler) -> None:
    body = _read_json(handler)
    client = _mark_client_seen(str(body.get("client_id") or ""))
    _json_response(handler, {"ok": True, "client": client})


def _issue_replay_arm(workflow_id: str) -> dict:
    token = str(uuid.uuid4())
    issued_at = time.strftime("%Y-%m-%d %H:%M:%S")
    with STATE_LOCK:
        if SHUTDOWN_EVENT.is_set():
            return {}
        client_id = STATE["client"].get("id", "")
        STATE["replay_arm"] = {
            "token": token,
            "workflow_id": workflow_id,
            "client_id": client_id,
            "expires_at": time.monotonic() + REPLAY_ARM_TTL_SECONDS,
            "issued_at": issued_at,
        }
    _audit_event("replay_arm_issued", workflow_id=workflow_id, client_id=client_id)
    return {
        "arm_token": token,
        "workflow_id": workflow_id,
        "expires_in_seconds": REPLAY_ARM_TTL_SECONDS,
    }


def _consume_replay_arm(workflow_id: str, token: str) -> tuple[bool, str]:
    now = time.monotonic()
    with STATE_LOCK:
        arm = dict(STATE.get("replay_arm") or {})
        STATE["replay_arm"] = {
            "token": "",
            "workflow_id": "",
            "client_id": "",
            "expires_at": 0.0,
            "issued_at": "",
        }
    if not token:
        return False, "Replay start rejected: missing arm token. Reload the console and press Run again."
    if not arm.get("token"):
        return False, "Replay start rejected: no armed replay is pending."
    if token != arm.get("token"):
        return False, "Replay start rejected: stale or mismatched arm token."
    if workflow_id != arm.get("workflow_id"):
        return False, "Replay start rejected: arm token belongs to a different workflow."
    if now > float(arm.get("expires_at") or 0.0):
        return False, "Replay start rejected: arm token expired. Press Run again."
    return True, ""


def _arm_replay(handler: BaseHTTPRequestHandler) -> None:
    if SHUTDOWN_EVENT.is_set():
        _error(handler, "Service is shutting down; replay cannot be armed.", 503)
        return
    body = _read_json(handler)
    workflow_id = str(body.get("workflow_id") or "").strip()
    if not workflow_id:
        _error(handler, "workflow_id is required.", 400)
        return
    if not _client_connected():
        _audit_event("replay_arm_rejected", workflow_id=workflow_id, reason="no_client_heartbeat")
        _error(handler, "Replay arm requires an active console heartbeat. Reload the console and try again.", 409)
        return
    with STATE_LOCK:
        if STATE["run"]["active"]:
            _error(handler, "A replay is already running.", 409)
            return
    try:
        workflow, subgraphs = _storage().load(workflow_id)
    except FileNotFoundError:
        _error(handler, f"Workflow not found: {workflow_id}", 404)
        return
    quality_error, quality_issues = _workflow_blocking_quality(workflow, subgraphs)
    if quality_error:
        _reject_workflow_quality(handler, workflow_id, quality_error, quality_issues)
        return
    payload = _issue_replay_arm(workflow_id)
    if not payload:
        _error(handler, "Service is shutting down; replay cannot be armed.", 503)
        return
    _log(f"Replay armed: {workflow_id}", "warn")
    _json_response(handler, {"ok": True, **payload})


def _panic_desktop_actions() -> None:
    try:
        from gpa.execution.actions import panic_stop
        panic_stop()
    except Exception as exc:
        _log(f"Could not arm desktop action panic stop: {exc}", "warn")


def _abort_desktop_actions() -> None:
    try:
        from gpa.execution.actions import abort_actions
        abort_actions()
    except Exception as exc:
        _log(f"Could not abort desktop actions: {exc}", "warn")


def _has_active_replay() -> bool:
    with STATE_LOCK:
        return bool(STATE["run"].get("active") and STATE.get("run_stop_event") is not None)


def _mark_active_replay_stopping(error: str) -> tuple[bool, str, str]:
    with STATE_LOCK:
        stop_event = STATE.get("run_stop_event")
        active = bool(STATE["run"].get("active") and stop_event is not None)
        run_id = STATE["run"].get("run_id", "")
        workflow_id = STATE["run"].get("workflow_id", "")
        if stop_event is not None:
            stop_event.set()
        if active:
            _set_run_status(
                "panic_stopping",
                success=None,
                error=error,
                stop_requested=True,
            )
        else:
            STATE["run_stop_event"] = None
            STATE["run_started_monotonic"] = None
    return active, run_id, workflow_id


def _abort_active_replay(error: str) -> tuple[bool, str, str]:
    active, run_id, workflow_id = _mark_active_replay_stopping(error)
    if active:
        _abort_desktop_actions()
    return active, run_id, workflow_id


def _wait_for_replay_worker(timeout_seconds: float = 3.0) -> bool:
    """Wait outside STATE_LOCK for the replay worker to observe cancellation."""
    with STATE_LOCK:
        worker = STATE.get("run_thread")
    if worker is None or worker is threading.current_thread():
        return True
    try:
        worker.join(timeout=max(0.0, timeout_seconds))
    except RuntimeError:
        # Be defensive if shutdown races with the tiny pre-start window of a
        # custom/fake thread object. Normal replay startup starts under lock.
        return not worker.is_alive()
    return not worker.is_alive()


def _replay_run_slot_busy() -> bool:
    with STATE_LOCK:
        worker = STATE.get("run_thread")
        return bool(
            STATE["run"].get("active")
            or (worker is not None and worker.is_alive())
        )


def _begin_replay_worker(
    worker: threading.Thread,
    run_state: dict,
    stop_event: threading.Event,
) -> str:
    """Atomically publish and start a replay unless shutdown or another run won."""
    with STATE_LOCK:
        current = STATE.get("run_thread")
        if SHUTDOWN_EVENT.is_set():
            return "Service is shutting down; replay cannot start."
        if STATE["run"].get("active") or (current is not None and current.is_alive()):
            return "A replay is already running."
        STATE["run"] = run_state
        STATE["run_stop_event"] = stop_event
        STATE["run_started_monotonic"] = None
        STATE["run_thread"] = worker
        try:
            worker.start()
        except BaseException:
            STATE["run_thread"] = None
            STATE["run_stop_event"] = None
            STATE["run_started_monotonic"] = None
            STATE["run"] = {**run_state, "active": False, "status": "failed"}
            raise
    return ""


def _read_json(handler: BaseHTTPRequestHandler, *, max_bytes: int | None = None) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    if max_bytes is not None and length > max_bytes:
        raise PayloadTooLargeError(f"Request body exceeds {max_bytes} bytes.")
    return json.loads(handler.rfile.read(length) or b"{}")


def _require_local_write_origin(handler: BaseHTTPRequestHandler) -> bool:
    origin = str(handler.headers.get("Origin") or "").strip()
    allowed = {
        "",
        f"http://127.0.0.1:{PORT}",
        f"http://localhost:{PORT}",
    }
    if origin in allowed:
        return True
    _error(handler, "Community write requests are restricted to the local GPA console.", 403)
    return False


def _storage():
    import gpa.storage.workflow as workflow_module
    from gpa.storage.workflow import WorkflowStorage

    workflow_module.WORKFLOWS_DIR = WORKFLOWS_DIR
    return WorkflowStorage()


def _replay_service():
    from gpa.replay.service import ReplayService

    key = (str(WORKFLOWS_DIR.resolve()), str(REPLAY_SPACES_DIR.resolve()))
    with REPLAY_SERVICE_LOCK:
        if REPLAY_SERVICE_CACHE["key"] != key:
            REPLAY_SERVICE_CACHE["key"] = key
            REPLAY_SERVICE_CACHE["value"] = ReplayService(
                _storage(),
                spaces_root=REPLAY_SPACES_DIR,
            )
        return REPLAY_SERVICE_CACHE["value"]


def _transition_replay_space(space_id: str, state: str, *, error: str = "") -> None:
    if not space_id:
        return
    try:
        _replay_service().spaces.transition(space_id, state, error=error)
    except Exception as exc:
        _log(f"Replay Space transition failed: {space_id}: {state}: {exc}", "warn")


def _prepare_replay_space(workflow_id: str, space_id: str = "") -> str:
    from gpa.replay.platforms import current_platform

    service = _replay_service()
    host_platform = current_platform()
    if space_id:
        space = service.spaces.get(space_id)
        if space.get("replay_id") != workflow_id:
            raise ValueError("Replay Space belongs to a different Replay.")
        if space.get("platform") != host_platform:
            raise ValueError("Replay Space was planned for a different platform.")
        if space.get("state") != "planned":
            raise ValueError("Replay Space is not ready to arm.")
        manifest = service.get_replay(workflow_id)
        _, compatibility = service.platform_planner.plan_steps(manifest, host_platform)
        if not compatibility.runnable:
            raise ValueError(
                "Replay is incompatible with this platform: "
                + ", ".join(compatibility.missing_capabilities)
            )
        return space_id

    plan = service.plan(workflow_id, platform=host_platform)
    if not plan.compatibility.runnable:
        raise ValueError(
            "Replay is incompatible with this platform: "
            + ", ".join(plan.compatibility.missing_capabilities)
        )
    return plan.space_id


def _community_repository():
    from gpa.community.repository import CommunityRepository

    return CommunityRepository(COMMUNITY_DIR, max_package_bytes=COMMUNITY_MAX_PACKAGE_BYTES)


def _demo_community_workflows():
    from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowVariable

    created_at = "2026-07-15T00:00:00+00:00"
    return [
        (
            Workflow(
                workflow_id="demo_web_research",
                workflow_name="demo_web_research",
                workflow_title="网页资料快速检索",
                description="用可配置关键词打开网页搜索，适合作为研究任务的起点。",
                task_description="根据关键词打开网页搜索结果，继续进行资料收集。",
                category="research",
                created_at=created_at,
                variables=[
                    WorkflowVariable(
                        "query",
                        "GUI+automation+best+practices",
                        "需要检索的关键词，空格可写为加号。",
                    )
                ],
                steps=[
                    WorkflowStep(
                        1,
                        "打开关键词搜索结果",
                        id="demo-web-research-open",
                        action_type="open_url",
                        value="https://www.google.com/search?q={{query}}",
                        active_app_name="Google Chrome",
                    )
                ],
            ),
            ["research", "browser", "demo"],
        ),
        (
            Workflow(
                workflow_id="demo_project_dashboard",
                workflow_name="demo_project_dashboard",
                workflow_title="项目看板快速打开",
                description="保存常用项目地址，一步进入团队看板或代码仓库。",
                task_description="在浏览器中打开指定项目主页。",
                category="project",
                created_at=created_at,
                variables=[
                    WorkflowVariable(
                        "project_url",
                        "https://github.com/",
                        "项目看板、代码仓库或任务系统的网址。",
                    )
                ],
                steps=[
                    WorkflowStep(
                        1,
                        "打开项目主页",
                        id="demo-project-dashboard-open",
                        action_type="open_url",
                        value="{{project_url}}",
                        active_app_name="Google Chrome",
                    )
                ],
            ),
            ["project", "browser", "demo"],
        ),
        (
            Workflow(
                workflow_id="demo_meeting_prep",
                workflow_name="demo_meeting_prep",
                workflow_title="会议准备工作区",
                description="依次打开日历和会议文档，快速进入会前准备状态。",
                task_description="打开日历与会议文档工作区。",
                category="meeting",
                created_at=created_at,
                steps=[
                    WorkflowStep(
                        1,
                        "打开日历",
                        id="demo-meeting-calendar-open",
                        action_type="open_url",
                        value="https://calendar.google.com/",
                        active_app_name="Google Chrome",
                    ),
                    WorkflowStep(
                        2,
                        "打开会议文档",
                        id="demo-meeting-doc-open",
                        action_type="open_url",
                        value="https://docs.google.com/document/u/0/",
                        active_app_name="Google Chrome",
                    ),
                ],
            ),
            ["meeting", "productivity", "demo"],
        ),
        (
            Workflow(
                workflow_id="demo_daily_brief",
                workflow_name="demo_daily_brief",
                workflow_title="每日技术简报入口",
                description="按固定顺序打开两个公开技术信息源，开始每日浏览。",
                task_description="打开 Hacker News 和 GitHub Trending。",
                category="daily",
                created_at=created_at,
                steps=[
                    WorkflowStep(
                        1,
                        "打开 Hacker News",
                        id="demo-daily-hn-open",
                        action_type="open_url",
                        value="https://news.ycombinator.com/",
                        active_app_name="Google Chrome",
                    ),
                    WorkflowStep(
                        2,
                        "打开 GitHub Trending",
                        id="demo-daily-github-open",
                        action_type="open_url",
                        value="https://github.com/trending",
                        active_app_name="Google Chrome",
                    ),
                ],
            ),
            ["daily", "browser", "demo"],
        ),
    ]


def _ensure_demo_community_records() -> list[dict]:
    import gpa.storage.workflow as workflow_module
    from gpa.community.package import export_workflow_package
    from gpa.storage.workflow import WorkflowStorage

    repository = _community_repository()
    workspace = COMMUNITY_DIR / ".demo-seed"
    workflows_dir = workspace / "workflows"
    packages_dir = workspace / "packages"
    previous_workflows_dir = workflow_module.WORKFLOWS_DIR
    shutil.rmtree(workspace, ignore_errors=True)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_module.WORKFLOWS_DIR = workflows_dir
    seeded = []
    try:
        storage = WorkflowStorage()
        for workflow, tags in _demo_community_workflows():
            storage.save(workflow, {})
            package_path = export_workflow_package(
                workflow.workflow_id,
                packages_dir,
                storage=storage,
            )
            seeded.append(
                repository.publish_package(
                    package_path,
                    author="GPA Examples",
                    tags=tags,
                    license_id="CC0-1.0",
                    privacy_reviewed=True,
                )
            )
    finally:
        workflow_module.WORKFLOWS_DIR = previous_workflows_dir
        shutil.rmtree(workspace, ignore_errors=True)
    return seeded


def _has_visual_context_for_payload(subgraph) -> bool:
    if subgraph is None:
        return False
    target = getattr(subgraph, "target_node", None)
    if target is None:
        return False
    content = str(getattr(target, "content", "") or "").strip().lower()
    if content.startswith("recorded coordinate") or content.startswith("manual coordinate"):
        return False
    return bool(
        getattr(target, "content", None)
        or getattr(target, "icon_emb", None) is not None
        or getattr(target, "text_emb", None) is not None
    )


def _quality_is_browser_app(app_name: str) -> bool:
    lowered = str(app_name or "").casefold()
    return any(
        token in lowered
        for token in ("chrome", "safari", "edge", "firefox", "brave", "browser", "浏览器")
    )


def _quality_is_wechat_app(app_name: str) -> bool:
    lowered = str(app_name or "").casefold()
    return "wechat" in lowered or "微信" in lowered


def _quality_mentions_chatgpt(text: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())
    return "chatgpt" in compact or "openai" in compact


def _quality_mentions_wechat(text: str) -> bool:
    lowered = str(text or "").casefold()
    return "wechat" in lowered or "微信" in lowered or "文件传输助手" in lowered or "file transfer" in lowered


def _quality_has_navigation_intent(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(
        token in lowered
        for token in ("open", "navigate", "url", "website", "site", "address", "打开", "导航", "网址", "网站", "地址")
    )


def _quality_has_send_intent(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(token in lowered for token in ("send", "发送", "发给", "发到"))


def _quality_step_is_wechat_delivery(step, step_text: str) -> bool:
    if not (_quality_mentions_wechat(step_text) or _quality_is_wechat_app(getattr(step, "active_app_name", ""))):
        return False
    lowered = str(step_text or "").casefold()
    if getattr(step, "action_type", "") in {"type", "hotkey"}:
        return True
    return any(
        token in lowered
        for token in ("send", "paste", "recipient", "发送", "粘贴", "文件传输助手", "file transfer")
    )


def _workflow_quality_payload(workflow, subgraphs: dict) -> dict:
    issues = []
    variable_values = {
        str(getattr(item, "name", "") or ""): str(getattr(item, "default_value", "") or "")
        for item in getattr(workflow, "variables", [])
    }

    def _resolve_step_value(value: str) -> str:
        resolved = str(value or "")
        for name, default in variable_values.items():
            resolved = resolved.replace(f"{{{{{name}}}}}", default)
        return resolved

    browser_goal_text = " ".join([
        getattr(workflow, "task_description", "") or "",
        getattr(workflow, "description", "") or "",
        getattr(workflow, "workflow_title", "") or "",
    ]).casefold()
    browser_goal = any(token in browser_goal_text for token in ("browser", "chrome", "safari", "web", "网页", "浏览器", "acmtechnews"))
    workflow_action_text = " ".join(
        [
            browser_goal_text,
            *[
                " ".join([str(step.action or ""), _resolve_step_value(str(step.value or ""))]).casefold()
                for step in workflow.steps
            ],
        ]
    )
    mentions_chatgpt = "chatgpt" in workflow_action_text or "chat.openai.com" in workflow_action_text
    requests_wechat_delivery = (
        _quality_mentions_wechat(workflow_action_text)
        and _quality_has_send_intent(workflow_action_text)
    )
    chatgpt_nav_tokens = ("chatgpt", "chatgpt.com", "chat.openai.com", "openai.com")
    opens_chatgpt = False
    has_wechat_delivery_step = False
    for step in workflow.steps:
        step_action = str(step.action or "").casefold()
        step_value = _resolve_step_value(str(step.value or "")).casefold()
        step_text = " ".join([step_action, step_value])
        if _quality_step_is_wechat_delivery(step, step_text):
            has_wechat_delivery_step = True
        has_chatgpt_target = any(token in step_text for token in chatgpt_nav_tokens)
        has_url_value = any(token in step_value for token in ("http://", "https://", "chatgpt.com", "chat.openai.com"))
        has_navigation_intent = any(
            token in step_action
            for token in ("open", "navigate", "url", "website", "site", "address", "打开", "导航", "网址", "网站", "地址")
        )
        if step.action_type == "open_url" and has_chatgpt_target:
            opens_chatgpt = True
            break
        if step.action_type == "type" and has_chatgpt_target and (has_url_value or has_navigation_intent):
            opens_chatgpt = True
            break
    if mentions_chatgpt and not opens_chatgpt:
        issues.append({
            "severity": "blocking",
            "step": None,
            "code": "missing_chatgpt_navigation",
            "message": "Workflow references ChatGPT but has no explicit ChatGPT navigation step; replay could type into the wrong browser tab.",
        })
    if requests_wechat_delivery and not has_wechat_delivery_step:
        issues.append({
            "severity": "blocking",
            "step": None,
            "code": "missing_wechat_delivery",
            "message": "Workflow goal says to send the result to WeChat, but no WeChat paste/send step was recorded.",
        })

    for item in workflow.steps:
        action = str(item.action or "")
        action_lower = action.casefold()
        value = _resolve_step_value(str(item.value or ""))
        step_text = " ".join([action, value])
        app = str(item.active_app_name or "").strip()
        app_lower = app.casefold()
        step_prefix = f"Step {item.step_number}"

        if app_lower == "codex":
            issues.append({
                "severity": "blocking",
                "step": item.step_number,
                "code": "targets_console",
                "message": f"{step_prefix} targets Codex; replaying into the console is unsafe. Remove the step or set the intended app.",
            })

        if item.action_type in {"click", "scroll", "drag"}:
            subgraph = subgraphs.get(item.id)
            if subgraph is None:
                issues.append({
                    "severity": "blocking",
                    "step": item.step_number,
                    "code": "missing_target_context",
                    "message": f"{step_prefix} has no replay target context.",
                })
            elif not _has_visual_context_for_payload(subgraph):
                issues.append({
                    "severity": "warn",
                    "step": item.step_number,
                    "code": "coordinate_only",
                    "message": f"{step_prefix} can only replay a recorded coordinate; verify the app/window layout before running.",
                })

        if (
            _quality_mentions_chatgpt(step_text)
            and not _quality_has_navigation_intent(step_text)
            and app
            and not _quality_is_browser_app(app)
        ):
            issues.append({
                "severity": "blocking",
                "step": item.step_number,
                "code": "target_app_mismatch",
                "message": f"{step_prefix} references ChatGPT but targets {app}; replay could operate in the wrong app.",
            })

        if _quality_mentions_wechat(step_text) and app and not _quality_is_wechat_app(app):
            issues.append({
                "severity": "blocking",
                "step": item.step_number,
                "code": "target_app_mismatch",
                "message": f"{step_prefix} references WeChat but targets {app}; replay could operate in the wrong app.",
            })

        if float(item.pause_duration or 0) > 3.0:
            issues.append({
                "severity": "info",
                "step": item.step_number,
                "code": "long_pause",
                "message": f"{step_prefix} waits {float(item.pause_duration):.1f}s after execution; consider replacing the wait with a readiness check.",
            })

        if (
            browser_goal
            and item.action_type in {"click", "type", "hotkey"}
            and app_lower in {"google chrome", "chrome", "safari", "microsoft edge", "brave browser", "firefox"}
            and any(token in action_lower for token in ("address", "search", "url", "navigate", "browser", "地址", "搜索"))
        ):
            issues.append({
                "severity": "info",
                "step": item.step_number,
                "code": "browser_semantic_repair",
                "message": f"{step_prefix} looks like browser navigation and will be optimized into direct URL opening when possible.",
            })

    severity_rank = {"blocking": 3, "warn": 2, "info": 1}
    worst = "ok"
    for issue in issues:
        if severity_rank.get(issue["severity"], 0) > severity_rank.get(worst, 0):
            worst = issue["severity"]
    return {
        "status": worst,
        "runnable": worst != "blocking",
        "issue_count": len(issues),
        "blocking_count": sum(1 for item in issues if item["severity"] == "blocking"),
        "warn_count": sum(1 for item in issues if item["severity"] == "warn"),
        "issues": issues,
    }


def _workflow_payload(workflow, subgraphs: dict) -> dict:
    return {
        "id": workflow.workflow_id,
        "name": workflow.workflow_name,
        "title": workflow.workflow_title,
        "description": workflow.description,
        "task_description": getattr(workflow, "task_description", ""),
        "category": workflow.category,
        "created_at": workflow.created_at,
        "variables": [
            {
                "name": item.name,
                "default_value": item.default_value,
                "description": item.description,
            }
            for item in workflow.variables
        ],
        "steps": [
            {
                "number": item.step_number,
                "id": item.id,
                "action": item.action,
                "action_type": item.action_type,
                "value": item.value,
                "pause_duration": item.pause_duration,
                "active_app_name": item.active_app_name,
                "has_subgraph": item.id in subgraphs,
                "click_coordinates": subgraphs[item.id].click_coordinates if item.id in subgraphs else None,
                "fallback_strategy": "visual_graph" if item.id in subgraphs and len(subgraphs[item.id].ui_graph.nodes) > 1 else (
                    "scaled_coordinate" if item.id in subgraphs else "logic_only"
                ),
                "metadata": item.metadata or {},
            }
            for item in workflow.steps
        ],
        "subgraph_count": len(subgraphs),
        "quality": _workflow_quality_payload(workflow, subgraphs),
    }


def _workflow_blocking_quality(workflow, subgraphs: dict) -> tuple[str, list[dict]]:
    quality = _workflow_quality_payload(workflow, subgraphs)
    if quality.get("runnable", True):
        return "", []
    blocking = [
        item
        for item in quality.get("issues", [])
        if item.get("severity") == "blocking"
    ]
    detail = "; ".join(str(item.get("message") or item.get("code") or "") for item in blocking).strip()
    message = "Replay blocked by workflow quality checks."
    if detail:
        message = f"{message} {detail}"
    return message, blocking


def _reject_workflow_quality(handler: BaseHTTPRequestHandler, workflow_id: str, message: str, issues: list[dict]) -> None:
    _audit_event(
        "replay_start_rejected",
        workflow_id=workflow_id,
        reason="workflow_quality_blocking",
        issue_codes=[item.get("code", "") for item in issues],
    )
    _log(f"Replay rejected by workflow quality checks: {workflow_id}: {message}", "error")
    _json_response(
        handler,
        {
            "ok": False,
            "error": message,
            "quality": {
                "runnable": False,
                "issues": issues,
            },
        },
        422,
    )


def _preview_payload() -> dict | None:
    preview = STATE.get("preview")
    if not preview:
        return None
    return {
        "preview_id": preview["preview_id"],
        "created_at": preview["created_at"],
        "workflow": _workflow_payload(preview["workflow"], preview["subgraphs"]),
    }


def _coerce_coordinates(value) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        return [float(value[0]), float(value[1])]
    except (TypeError, ValueError):
        return None


def _coordinate_subgraph(step_id: str, coords: list[float]):
    from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode

    x, y = coords
    box_size = 16.0
    image_width = max(1, int(x) * 2)
    image_height = max(1, int(y) * 2)
    node = UINode(
        id=0,
        pos=[
            max(0.0, x - box_size / 2),
            max(0.0, y - box_size / 2),
            box_size,
            box_size,
        ],
        elem_type="icon",
        content=f"manual coordinate for {step_id}",
    )
    graph = UIGraph(nodes=[node], image_size=[image_width, image_height])
    return StepSubgraph(
        target_element_id=0,
        click_coordinates=coords,
        ui_graph=graph,
        window_bounds=[0, 0, image_width, image_height],
        scale_factor=1.0,
    )


def _apply_workflow_payload(workflow, subgraphs: dict, payload: dict):
    from gpa.storage.workflow import WorkflowStep, WorkflowVariable

    workflow.workflow_name = str(payload.get("name", workflow.workflow_name)).strip() or workflow.workflow_name
    workflow.workflow_title = str(payload.get("title", workflow.workflow_title)).strip() or workflow.workflow_title
    workflow.description = str(payload.get("description", workflow.description))
    workflow.task_description = str(payload.get("task_description", getattr(workflow, "task_description", "")))
    workflow.category = str(payload.get("category", workflow.category))
    workflow.variables = [
        WorkflowVariable(
            name=str(item.get("name", "")).strip(),
            default_value=str(item.get("default_value", "")),
            description=str(item.get("description", "")),
        )
        for item in payload.get("variables", [])
        if str(item.get("name", "")).strip()
    ]

    old_steps = {step.id: step for step in workflow.steps}
    new_steps = []
    new_subgraphs = {}
    for index, item in enumerate(payload.get("steps", []), 1):
        step_id = str(item.get("id") or uuid.uuid4())
        old = old_steps.get(step_id)
        incoming_metadata = item.get("metadata")
        if isinstance(incoming_metadata, dict):
            step_metadata = dict(incoming_metadata)
        elif old is not None:
            step_metadata = dict(old.metadata or {})
        else:
            step_metadata = {}
        step = WorkflowStep(
            step_number=index,
            id=step_id,
            action=str(item.get("action", old.action if old else f"Step {index}")),
            action_type=str(item.get("action_type", old.action_type if old else "click")),
            value=str(item.get("value", old.value if old else "")),
            pause_duration=float(item.get("pause_duration", old.pause_duration if old else 0.5) or 0),
            active_app_name=str(item.get("active_app_name", old.active_app_name if old else "")),
            metadata=step_metadata,
        )
        new_steps.append(step)
        coords = _coerce_coordinates(item.get("click_coordinates"))
        if step_id in subgraphs:
            sg = subgraphs[step_id]
            if coords is not None:
                sg.click_coordinates = coords
                target = sg.target_node
                if target is not None:
                    target.pos[0] = max(0.0, sg.click_coordinates[0] - target.pos[2] / 2)
                    target.pos[1] = max(0.0, sg.click_coordinates[1] - target.pos[3] / 2)
            new_subgraphs[step_id] = sg
        elif step.action_type in ("click", "scroll", "drag") and coords is not None:
            new_subgraphs[step_id] = _coordinate_subgraph(step_id, coords)
    workflow.steps = new_steps
    return workflow, new_subgraphs


def _save_run_history(workflow_id: str, run_id: str, run_state: dict, result=None) -> pathlib.Path:
    run_dir = RUNS_DIR / workflow_id
    run_dir.mkdir(parents=True, exist_ok=True)
    steps = []
    if result is not None:
        for item in result.step_results:
            loc = item.localization
            steps.append({
                "step_number": item.step_number,
                "state": item.state.name.lower(),
                "retries": item.retries,
                "error": item.error,
                "duration_seconds": getattr(item, "duration_seconds", 0.0),
                "agent_decision_ms": getattr(item, "agent_decision_ms", 0.0),
                "agent_decision": getattr(item, "agent_decision", {}),
                "corrections": getattr(item, "corrections", []),
                "observation_metrics": getattr(item, "observation_metrics", []),
                "localization": None if loc is None else {
                    "x": loc.x,
                    "y": loc.y,
                    "confidence": loc.confidence,
                    "method": loc.method,
                },
            })
    payload = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "status": run_state.get("status"),
        "success": run_state.get("success"),
        "started_at": run_state.get("started_at"),
        "finished_at": run_state.get("finished_at"),
        "elapsed_seconds": run_state.get("elapsed_seconds"),
        "steps_run": run_state.get("steps_run"),
        "steps_failed": run_state.get("steps_failed"),
        "error": run_state.get("error"),
        "steps": steps,
    }
    path = run_dir / f"{run_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def _list_run_history(workflow_id: str = "") -> list[dict]:
    roots = [RUNS_DIR / workflow_id] if workflow_id else [p for p in RUNS_DIR.iterdir()] if RUNS_DIR.exists() else []
    runs = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.glob("*.json"):
            try:
                payload = json.loads(path.read_text())
            except Exception:
                continue
            runs.append({
                "run_id": payload.get("run_id", path.stem),
                "workflow_id": payload.get("workflow_id", root.name),
                "status": payload.get("status", ""),
                "success": payload.get("success"),
                "started_at": payload.get("started_at"),
                "finished_at": payload.get("finished_at"),
                "elapsed_seconds": payload.get("elapsed_seconds", 0),
                "steps_run": payload.get("steps_run", 0),
                "steps_failed": payload.get("steps_failed", 0),
                "error": payload.get("error", ""),
                "steps": payload.get("steps", []),
            })
    return sorted(runs, key=lambda item: item.get("finished_at") or item.get("started_at") or "", reverse=True)[:25]


def _set_recording_status(status: str, **updates) -> None:
    active = status in {"starting", "recording", "stopping", "building"}
    STATE["recording"].update({"status": status, "active": active, **updates})


def _public_state() -> dict:
    with STATE_LOCK:
        recorder = STATE["recorder"]
        if recorder is not None:
            try:
                STATE["recording"]["event_count"] = len(recorder._recording.events)
            except Exception:
                pass
        if STATE["run_started_monotonic"] and STATE["run"]["active"]:
            STATE["run"]["elapsed_seconds"] = int(time.monotonic() - STATE["run_started_monotonic"])
        state = {
            "recording": dict(STATE["recording"]),
            "run": dict(STATE["run"]),
            "preview": _preview_payload(),
            "visual_warmup": dict(STATE["visual_warmup"]),
            "logs": list(STATE["logs"]),
        }
    workflow_count = len(list(WORKFLOWS_DIR.glob("*/workflow.yaml"))) if WORKFLOWS_DIR.exists() else 0
    state.update(
        {
            "ok": True,
            "project_root": str(PROJECT_ROOT),
            "workflows_dir": str(WORKFLOWS_DIR),
            "workflow_count": workflow_count,
            "python": sys.version.split()[0],
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "health": _cached_dependency_health(),
            "replay": {
                "agent_first": REPLAY_AGENT_FIRST,
                "agent_first_env": REPLAY_AGENT_FIRST_ENV,
            },
            "client": _client_status(),
        }
    )
    return state


def _require_desktop_automation(handler: BaseHTTPRequestHandler, operation: str) -> bool:
    if DESKTOP_AUTOMATION_ENABLED:
        return True
    message = (
        f"{operation} is blocked because desktop automation is disabled. "
        f"Restart the server with {DESKTOP_AUTOMATION_ENV}=1 only when you are ready to let GPA control the desktop."
    )
    _log(message, "warn")
    _json_response(
        handler,
        {
            "ok": False,
            "error": message,
            "desktop_automation": {
                "enabled": False,
                "env": DESKTOP_AUTOMATION_ENV,
            },
        },
        423,
    )
    return False


def _start_recording(handler: BaseHTTPRequestHandler) -> None:
    if not _require_desktop_automation(handler, "Recording"):
        return
    body = _read_json(handler)
    workflow_id = (body.get("workflow_id") or "").strip()
    task_description = str(body.get("task_description") or "").strip()
    if not workflow_id:
        workflow_id = time.strftime("web_%Y%m%d_%H%M%S")

    with STATE_LOCK:
        if STATE["recording"]["active"]:
            _error(handler, "Recording is already active.", 409)
            return
        _set_recording_status(
            "starting",
            workflow_id=workflow_id,
            task_description=task_description,
            started_at=None,
            finished_at=None,
            event_count=0,
            error="",
        )

    try:
        _ensure_dependencies("record")
        _ensure_permissions("record")
        from gpa.recording.recorder import Recorder

        recorder = Recorder()
        recorder.start()
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        with STATE_LOCK:
            STATE["recorder"] = recorder
            _set_recording_status(
                "recording",
                started_at=started_at,
                workflow_id=workflow_id,
                task_description=task_description,
                event_count=0,
                error="",
            )
        suffix = f" · goal: {task_description[:80]}" if task_description else ""
        _log(f"Recording started: {workflow_id}{suffix}")
        _json_response(
            handler,
            {
                "ok": True,
                "workflow_id": workflow_id,
                "task_description": task_description,
                "started_at": started_at,
            },
        )
    except MissingDependencyError as exc:
        with STATE_LOCK:
            STATE["recorder"] = None
            _set_recording_status("failed", error=str(exc), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _handle_missing_dependency(handler, exc)
    except RuntimePermissionError as exc:
        with STATE_LOCK:
            STATE["recorder"] = None
            _set_recording_status("failed", error=str(exc), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _handle_permission_error(handler, exc)
    except Exception as exc:
        with STATE_LOCK:
            STATE["recorder"] = None
            _set_recording_status("failed", error=str(exc), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _log(f"Recording start failed: {exc}", "error")
        _error(handler, str(exc), 500)


def _stop_recording(handler: BaseHTTPRequestHandler) -> None:
    body = _read_json(handler)
    build = body.get("build", True)
    preview = body.get("preview", True)
    workflow_id_override = (body.get("workflow_id") or "").strip()
    task_description_override = str(body.get("task_description") or "").strip()

    with STATE_LOCK:
        recorder = STATE["recorder"]
        workflow_id = workflow_id_override or STATE["recording"]["workflow_id"]
        task_description = task_description_override or STATE["recording"].get("task_description", "")
        if not STATE["recording"]["active"] or recorder is None:
            _error(handler, "No active recording.", 409)
            return
        _set_recording_status("stopping", workflow_id=workflow_id)

    try:
        recording = recorder.stop()
        event_count = len(recording.events)
        saved = None
        workflow = None
        with STATE_LOCK:
            STATE["recorder"] = None
            _set_recording_status("building" if build and event_count > 0 else "idle", event_count=event_count)
            STATE["last_recording"] = recording

        if build and event_count > 0:
            try:
                _ensure_dependencies("build")
                _ensure_permissions("build")
            except MissingDependencyError as exc:
                message = _dependency_message(exc.group, exc.missing)
                with STATE_LOCK:
                    _set_recording_status("idle", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), error=message)
                _log(f"Recording stopped; build skipped. {message}", "warn")
                _json_response(
                    handler,
                    {
                        "ok": True,
                        "event_count": event_count,
                        "workflow_id": workflow_id,
                        "saved_path": "",
                        "built": False,
                        "warning": message,
                        "missing_dependencies": exc.missing,
                    },
                )
                return
            except RuntimePermissionError as exc:
                message = _permission_message(exc.group, exc.issues)
                with STATE_LOCK:
                    _set_recording_status("idle", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), error=message)
                _log(f"Recording stopped; build skipped. {message}", "warn")
                _json_response(
                    handler,
                    {
                        "ok": True,
                        "event_count": event_count,
                        "workflow_id": workflow_id,
                        "saved_path": "",
                        "built": False,
                        "warning": message,
                        "permission_issues": exc.issues,
                    },
                )
                return

            from gpa.recording.builder import build_workflow

            result = build_workflow(
                recording,
                workflow_id=workflow_id,
                task_description=task_description,
            )
            workflow = result.workflow
            if preview:
                preview_id = str(uuid.uuid4())
                with STATE_LOCK:
                    STATE["preview"] = {
                        "preview_id": preview_id,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "workflow": workflow,
                        "subgraphs": result.step_subgraphs,
                    }
                _log(f"Recording preview ready: {workflow.workflow_id} ({len(workflow.steps)} steps)")
            else:
                saved = _storage().save(workflow, result.step_subgraphs)
                _log(f"Recording built workflow: {workflow.workflow_id} ({len(workflow.steps)} steps)")
        elif event_count == 0:
            _log("Recording stopped with 0 events", "warn")
        else:
            _log(f"Recording stopped without build: {event_count} events")

        _json_response(
            handler,
            {
                "ok": True,
                "event_count": event_count,
                "workflow_id": workflow.workflow_id if workflow else workflow_id,
                "saved_path": str(saved) if saved else "",
                "built": workflow is not None,
                "preview": preview and workflow is not None,
                "preview_id": STATE["preview"]["preview_id"] if preview and workflow is not None else "",
            },
        )
        with STATE_LOCK:
            _set_recording_status("idle", finished_at=time.strftime("%Y-%m-%d %H:%M:%S"), error="")
    except Exception as exc:
        with STATE_LOCK:
            STATE["recorder"] = None
            _set_recording_status("failed", error=str(exc), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _log(f"Recording stop failed: {exc}", "error")
        _error(handler, str(exc), 500)


def _set_run_status(status: str, **updates) -> None:
    active = status in {"countdown", "running", "stopping", "panic_stopping"}
    STATE["run"].update({"status": status, "active": active, **updates})


def _stop_run_for_client_disconnect(stop_event: threading.Event, runtime_state: dict) -> bool:
    if runtime_state.get("client_disconnected"):
        return True
    if _client_connected():
        return False
    if not runtime_state.get("client_stale"):
        runtime_state["client_stale"] = True
        _log(
            "Console heartbeat is stale; replay continues. Use Stop or Panic if needed.",
            "warn",
        )
    return False


def _start_replay_watchdog(
    run_id: str,
    stop_event: threading.Event,
    runtime_state: dict,
    max_runtime_seconds: int,
) -> None:
    def watchdog() -> None:
        while True:
            time.sleep(0.2)
            with STATE_LOCK:
                active = STATE["run"].get("active") and STATE["run"].get("run_id") == run_id
                started = STATE["run_started_monotonic"]
            if not active:
                return
            if _stop_run_for_client_disconnect(stop_event, runtime_state):
                return
            if (
                started
                and max_runtime_seconds > 0
                and time.monotonic() - started > max_runtime_seconds
            ):
                runtime_state["timed_out"] = True
                stop_event.set()
                _abort_desktop_actions()
                with STATE_LOCK:
                    STATE["run"]["stop_requested"] = True
                    STATE["run"]["error"] = "Replay exceeded maximum runtime."
                return

    threading.Thread(target=watchdog, daemon=True).start()


def _run_workflow_thread(
    run_id: str,
    workflow_id: str,
    variables: dict,
    threshold: float,
    retries: int,
    countdown_seconds: int,
    max_runtime_seconds: int,
    stop_event: threading.Event,
    space_id: str = "",
) -> None:
    try:
        from gpa.execution.executor import Executor

        workflow, subgraphs = _storage().load(workflow_id)
        quality_error, quality_issues = _workflow_blocking_quality(workflow, subgraphs)
        if quality_error:
            with STATE_LOCK:
                _set_run_status(
                    "failed",
                    finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    success=False,
                    error=quality_error,
                    total_steps=len(workflow.steps),
                    stop_requested=True,
                    current_step=None,
                )
                run_snapshot = dict(STATE["run"])
                STATE["run_stop_event"] = None
                STATE["run_started_monotonic"] = None
            _save_run_history(workflow_id, run_id, run_snapshot)
            _transition_replay_space(space_id, "failed", error=quality_error)
            _log(f"Replay blocked by workflow quality checks: {workflow_id}: {quality_error}", "error")
            return
        with STATE_LOCK:
            STATE["run"]["total_steps"] = len(workflow.steps)

        runtime_state = {"timed_out": False, "client_disconnected": False, "client_stale": False}
        _start_replay_watchdog(run_id, stop_event, runtime_state, max_runtime_seconds)

        def client_disconnected() -> bool:
            return _stop_run_for_client_disconnect(stop_event, runtime_state)

        for remaining in range(countdown_seconds, 0, -1):
            if stop_event.is_set() or client_disconnected():
                with STATE_LOCK:
                    _set_run_status(
                        "cancelled",
                        finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                        success=False,
                        error=(
                            "Replay stopped because the console page disconnected."
                            if runtime_state["client_disconnected"]
                            else "Replay cancelled during countdown."
                        ),
                        countdown_remaining=0,
                        stop_requested=True,
                    )
                    STATE["run_stop_event"] = None
                    STATE["run_started_monotonic"] = None
                _log(f"Replay cancelled during countdown: {workflow_id}", "warn")
                _transition_replay_space(space_id, "stopped", error=STATE["run"].get("error", ""))
                return
            with STATE_LOCK:
                STATE["run"]["countdown_remaining"] = remaining
            time.sleep(1)

        with STATE_LOCK:
            STATE["run_started_monotonic"] = time.monotonic()
            _set_run_status("running", countdown_remaining=0)
        _replay_service().spaces.transition(space_id, "running")
        _log(f"Replay started: {workflow_id}")

        def should_stop() -> bool:
            if stop_event.is_set():
                return True
            if client_disconnected():
                return True
            with STATE_LOCK:
                started = STATE["run_started_monotonic"]
            if started and max_runtime_seconds > 0 and time.monotonic() - started > max_runtime_seconds:
                runtime_state["timed_out"] = True
                stop_event.set()
                with STATE_LOCK:
                    STATE["run"]["stop_requested"] = True
                    STATE["run"]["error"] = "Replay exceeded maximum runtime."
                return True
            return False

        def on_step_start(step) -> None:
            with STATE_LOCK:
                STATE["run"]["current_step"] = {
                    "number": step.step_number,
                    "action": step.action,
                    "action_type": step.action_type,
                }

        def on_agent_decision(step, decision: dict) -> None:
            with STATE_LOCK:
                current = STATE["run"].get("current_step") or {
                    "number": step.step_number,
                    "action": step.action,
                    "action_type": step.action_type,
                }
                current["agent_decision"] = {
                    "action_type": decision.get("action_type", ""),
                    "confidence": decision.get("confidence", 0),
                    "reason": decision.get("reason", ""),
                }
                STATE["run"]["current_step"] = current
            reason = str(decision.get("reason", "")).strip()
            _log(
                f"Replay decision step {step.step_number}: "
                f"{decision.get('action_type', step.action_type)}"
                + (f" · {reason[:120]}" if reason else "")
            )

        executor = Executor(
            workflow,
            subgraphs,
            variables=variables,
            readiness_threshold=threshold,
            max_retries=retries,
            should_stop=should_stop,
            on_step_start=on_step_start,
            on_agent_decision=on_agent_decision,
            agent_first=REPLAY_AGENT_FIRST,
        )
        result = executor.run()
        with STATE_LOCK:
            current_run_status = STATE["run"].get("status", "")
            current_run_error = STATE["run"].get("error", "")

        if runtime_state["client_disconnected"]:
            status = "client_disconnected"
            success = False
            error = "Replay stopped because the console page disconnected."
        elif runtime_state["timed_out"]:
            status = "timed_out"
            success = False
            error = "Replay exceeded maximum runtime."
        elif current_run_status == "panic_stopping":
            status = "panic_stopped"
            success = False
            error = current_run_error or "Emergency stop requested."
        elif stop_event.is_set():
            status = "cancelled"
            success = False
            error = "Replay stopped by user."
        elif result.success:
            status = "succeeded"
            success = True
            error = ""
        else:
            status = "failed"
            success = False
            error = result.error

        with STATE_LOCK:
            _set_run_status(
                status,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                success=success,
                error=error,
                steps_run=result.n_steps,
                steps_failed=result.n_failed,
                stop_requested=stop_event.is_set(),
                current_step=None,
            )
            run_snapshot = dict(STATE["run"])
            STATE["run_stop_event"] = None
            STATE["run_started_monotonic"] = None
        _save_run_history(workflow_id, run_id, run_snapshot, result)
        if success:
            _transition_replay_space(space_id, "completed")
        elif status in {"cancelled", "panic_stopped", "client_disconnected"}:
            _transition_replay_space(space_id, "stopped", error=error)
        else:
            _transition_replay_space(space_id, "failed", error=error)
        if success:
            _log(f"Replay completed: {workflow_id} ({result.n_steps} steps)")
        elif status == "cancelled":
            _log(f"Replay stopped: {workflow_id}", "warn")
        elif status == "panic_stopped":
            _log(f"Replay emergency-stopped: {workflow_id}", "error")
        elif status == "timed_out":
            _log(f"Replay timed out: {workflow_id}", "error")
        else:
            _log(f"Replay failed: {workflow_id}: {error}", "error")
    except Exception as exc:
        _abort_desktop_actions()
        with STATE_LOCK:
            _set_run_status(
                "failed",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                success=False,
                error=str(exc),
            )
            run_snapshot = dict(STATE["run"])
            STATE["run_stop_event"] = None
            STATE["run_started_monotonic"] = None
        _save_run_history(workflow_id, run_id, run_snapshot)
        _transition_replay_space(space_id, "failed", error=str(exc))
        _log(f"Replay crashed: {workflow_id}: {exc}", "error")
    finally:
        current = threading.current_thread()
        with STATE_LOCK:
            if STATE.get("run_thread") is current:
                STATE["run_thread"] = None


def _start_replay(handler: BaseHTTPRequestHandler, workflow_id: str) -> None:
    if SHUTDOWN_EVENT.is_set():
        _error(handler, "Service is shutting down; replay cannot start.", 503)
        return
    if not _require_desktop_automation(handler, "Replay"):
        return
    body = _read_json(handler)
    arm_token = str(body.get("arm_token") or "")
    variables = body.get("variables") or {}
    threshold = float(body.get("threshold", 0.5))
    retries = int(body.get("retries", 5))
    countdown_seconds = max(0, min(30, int(float(body.get("countdown_seconds", 3)))))
    max_runtime_seconds = max(10, min(3600, int(float(body.get("max_runtime_seconds", 300)))))
    space_id = str(body.get("space_id") or "").strip()
    try:
        _ensure_dependencies("replay")
        _ensure_permissions("replay")
        workflow, subgraphs = _storage().load(workflow_id)
    except MissingDependencyError as exc:
        _handle_missing_dependency(handler, exc)
        return
    except RuntimePermissionError as exc:
        _handle_permission_error(handler, exc)
        return
    except FileNotFoundError:
        _error(handler, f"Workflow not found: {workflow_id}", 404)
        return
    quality_error, quality_issues = _workflow_blocking_quality(workflow, subgraphs)
    if quality_error:
        _reject_workflow_quality(handler, workflow_id, quality_error, quality_issues)
        return

    try:
        space_id = _prepare_replay_space(workflow_id, space_id)
    except FileNotFoundError as exc:
        _error(handler, str(exc), 404)
        return
    except ValueError as exc:
        _error(handler, str(exc), 422)
        return

    if not _client_connected():
        _audit_event("replay_start_rejected", workflow_id=workflow_id, reason="no_client_heartbeat")
        _error(
            handler,
            "Replay requires an active console page heartbeat. Reopen the console and try again.",
            409,
        )
        return

    with STATE_LOCK:
        worker = STATE.get("run_thread")
        if STATE["run"]["active"] or (worker is not None and worker.is_alive()):
            _audit_event("replay_start_rejected", workflow_id=workflow_id, reason="already_running")
            _error(handler, "A replay is already running.", 409)
            return

    armed, arm_error = _consume_replay_arm(workflow_id, arm_token)
    if not armed:
        _audit_event("replay_start_rejected", workflow_id=workflow_id, reason=arm_error)
        _log(f"Replay rejected: {workflow_id}: {arm_error}", "error")
        _error(handler, arm_error, 409)
        return

    try:
        _replay_service().spaces.transition(space_id, "armed")
    except Exception as exc:
        _error(handler, f"Replay Space could not be armed: {exc}", 409)
        return

    run_id = str(uuid.uuid4())
    stop_event = threading.Event()
    run_state = {
        "active": True,
        "status": "countdown" if countdown_seconds > 0 else "running",
        "run_id": run_id,
        "workflow_id": workflow_id,
        "space_id": space_id,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": None,
        "success": None,
        "error": "",
        "steps_run": 0,
        "steps_failed": 0,
        "current_step": None,
        "total_steps": len(workflow.steps),
        "countdown_remaining": countdown_seconds,
        "max_runtime_seconds": max_runtime_seconds,
        "elapsed_seconds": 0,
        "stop_requested": False,
    }
    thread = threading.Thread(
        target=_run_workflow_thread,
        args=(
            run_id,
            workflow_id,
            {k: str(v) for k, v in variables.items()},
            threshold,
            retries,
            countdown_seconds,
            max_runtime_seconds,
            stop_event,
            space_id,
        ),
        daemon=True,
    )
    start_error = _begin_replay_worker(thread, run_state, stop_event)
    if start_error:
        reason = "shutting_down" if SHUTDOWN_EVENT.is_set() else "already_running"
        status = 503 if reason == "shutting_down" else 409
        _audit_event("replay_start_rejected", workflow_id=workflow_id, reason=reason)
        _transition_replay_space(space_id, "failed", error=start_error)
        _error(handler, start_error, status)
        return
    _audit_event("replay_start_accepted", workflow_id=workflow_id, run_id=run_id, space_id=space_id)
    _json_response(
        handler,
        {
            "ok": True,
            "run_id": run_id,
            "workflow_id": workflow_id,
            "space_id": space_id,
            "countdown_seconds": countdown_seconds,
            "max_runtime_seconds": max_runtime_seconds,
        },
    )


def _stop_replay(handler: BaseHTTPRequestHandler) -> None:
    with STATE_LOCK:
        stop_event = STATE.get("run_stop_event")
        if not STATE["run"]["active"] or stop_event is None:
            _error(handler, "No active replay.", 409)
            return
        stop_event.set()
        _set_run_status("stopping", stop_requested=True, error="Stop requested by user.")
        run_id = STATE["run"]["run_id"]
        workflow_id = STATE["run"]["workflow_id"]
    _abort_desktop_actions()
    _log(f"Replay stop requested: {workflow_id}", "warn")
    _json_response(handler, {"ok": True, "run_id": run_id, "workflow_id": workflow_id})


def _panic_replay(handler: BaseHTTPRequestHandler, *, release_inputs: bool = True) -> None:
    should_panic = False
    with STATE_LOCK:
        stop_event = STATE.get("run_stop_event")
        if stop_event is not None:
            stop_event.set()
        run_id = STATE["run"].get("run_id", "")
        workflow_id = STATE["run"].get("workflow_id", "")
        if STATE["run"].get("active"):
            should_panic = True
            _set_run_status(
                "panic_stopping",
                success=None,
                error="Emergency stop requested.",
                stop_requested=True,
            )
        else:
            STATE["run_stop_event"] = None
            STATE["run_started_monotonic"] = None
    if should_panic:
        if release_inputs:
            _panic_desktop_actions()
        else:
            _abort_desktop_actions()
        _log("Emergency replay stop requested", "error")
    else:
        _log("Emergency replay stop ignored; no active replay.", "warn")
    _json_response(handler, {"ok": True, "run_id": run_id, "workflow_id": workflow_id})


def _client_disconnect_request(handler: BaseHTTPRequestHandler) -> None:
    _client_disconnect()
    if _has_active_replay():
        _log("Console page disconnected; active replay is aborted.", "warn")
        _panic_replay(handler, release_inputs=False)
    else:
        _log("Console page disconnected; no active replay.", "warn")
        _json_response(handler, {"ok": True, "run_id": "", "workflow_id": ""})


def _update_workflow(handler: BaseHTTPRequestHandler, workflow_id: str) -> None:
    body = _read_json(handler)
    try:
        workflow, subgraphs = _storage().load(workflow_id)
        workflow, subgraphs = _apply_workflow_payload(workflow, subgraphs, body.get("workflow", body))
        saved = _storage().save(workflow, subgraphs)
        _log(f"Workflow updated: {workflow.workflow_id}")
        _json_response(
            handler,
            {
                "ok": True,
                "workflow": _workflow_payload(workflow, subgraphs),
                "saved_path": str(saved),
            },
        )
    except FileNotFoundError:
        _error(handler, f"Workflow not found: {workflow_id}", 404)
    except Exception as exc:
        _log(f"Workflow update failed: {workflow_id}: {exc}", "error")
        _error(handler, str(exc), 500)


def _save_preview(handler: BaseHTTPRequestHandler) -> None:
    body = _read_json(handler)
    with STATE_LOCK:
        preview = STATE.get("preview")
        if not preview:
            _error(handler, "No preview is active.", 409)
            return
        workflow = preview["workflow"]
        subgraphs = preview["subgraphs"]
    try:
        workflow, subgraphs = _apply_workflow_payload(workflow, subgraphs, body.get("workflow", {}))
        saved = _storage().save(workflow, subgraphs)
        with STATE_LOCK:
            STATE["preview"] = None
        _log(f"Preview saved workflow: {workflow.workflow_id}")
        _json_response(
            handler,
            {
                "ok": True,
                "workflow_id": workflow.workflow_id,
                "workflow": _workflow_payload(workflow, subgraphs),
                "saved_path": str(saved),
            },
        )
    except Exception as exc:
        _log(f"Preview save failed: {exc}", "error")
        _error(handler, str(exc), 500)


def _discard_preview(handler: BaseHTTPRequestHandler) -> None:
    with STATE_LOCK:
        had_preview = STATE.get("preview") is not None
        STATE["preview"] = None
    if had_preview:
        _log("Recording preview discarded", "warn")
    _json_response(handler, {"ok": True})


def _publish_community_record(handler: BaseHTTPRequestHandler) -> None:
    if not _require_local_write_origin(handler):
        return
    try:
        body = _read_json(handler, max_bytes=COMMUNITY_MAX_JSON_BYTES)
        if body.get("privacy_reviewed") is not True:
            raise ValueError("Explicit privacy review is required before publishing.")
        workflow_id = str(body.get("workflow_id") or "").strip()
        package_base64 = body.get("package_base64")
        if bool(workflow_id) == bool(package_base64):
            raise ValueError("Provide exactly one of workflow_id or package_base64.")
        author = str(body.get("author") or "Anonymous")
        tags = body.get("tags") or []
        license_id = str(body.get("record_license") or "CC-BY-4.0")
        repository = _community_repository()

        if workflow_id:
            storage = _storage()
            with tempfile.TemporaryDirectory() as tmpdir:
                from gpa.community.package import export_workflow_package

                package_path = export_workflow_package(
                    workflow_id,
                    pathlib.Path(tmpdir) / "record.gpa-record.zip",
                    storage=storage,
                )
                record = repository.publish_package(
                    package_path,
                    author=author,
                    tags=tags,
                    license_id=license_id,
                    privacy_reviewed=True,
                )
        else:
            encoded = str(package_base64 or "")
            if len(encoded) > ((COMMUNITY_MAX_PACKAGE_BYTES + 2) // 3) * 4 + 8:
                raise PayloadTooLargeError("Encoded package exceeds the upload limit.")
            try:
                package_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("package_base64 is not valid base64.") from exc
            record = repository.publish_package(
                package_bytes,
                author=author,
                tags=tags,
                license_id=license_id,
                privacy_reviewed=True,
            )

        _audit_event(
            "community_record_published",
            record_id=record["record_id"],
            workflow_id=record["workflow_id"],
            duplicate=record.get("duplicate", False),
        )
        _log(
            f"Community record {'reused' if record.get('duplicate') else 'published'}: "
            f"{record['record_id']}"
        )
        _json_response(handler, {"ok": True, "record": record}, 200 if record.get("duplicate") else 201)
    except PayloadTooLargeError as exc:
        _error(handler, str(exc), 413)
    except FileNotFoundError as exc:
        _error(handler, str(exc), 404)
    except (ValueError, json.JSONDecodeError) as exc:
        _error(handler, str(exc), 422)
    except Exception as exc:
        _log(f"Community publish failed: {exc}", "error")
        _error(handler, str(exc), 500)


def _import_community_record(handler: BaseHTTPRequestHandler, record_id: str) -> None:
    if not _require_local_write_origin(handler):
        return
    try:
        body = _read_json(handler, max_bytes=64 * 1024)
        requested_id = str(body.get("workflow_id") or "").strip() or None
        storage = _storage()
        result = _community_repository().import_record(
            record_id,
            workflow_id=requested_id,
            storage=storage,
        )
        workflow, subgraphs = storage.load(result.workflow_id)
        _audit_event(
            "community_record_imported",
            record_id=record_id,
            workflow_id=result.workflow_id,
            already_saved=result.already_saved,
        )
        action = "already saved" if result.already_saved else "saved"
        _log(f"Community record {action}: {record_id} → {result.workflow_id}")
        _json_response(
            handler,
            {
                "ok": True,
                "workflow_id": result.workflow_id,
                "was_renamed": result.was_renamed,
                "already_saved": result.already_saved,
                "workflow": _workflow_payload(workflow, subgraphs),
            },
            200 if result.already_saved else 201,
        )
    except FileNotFoundError as exc:
        _error(handler, str(exc), 404)
    except (ValueError, json.JSONDecodeError) as exc:
        _error(handler, str(exc), 422)
    except Exception as exc:
        _log(f"Community import failed: {record_id}: {exc}", "error")
        _error(handler, str(exc), 500)


def _submit_community_feedback(handler: BaseHTTPRequestHandler, record_id: str) -> None:
    if not _require_local_write_origin(handler):
        return
    try:
        body = _read_json(handler, max_bytes=64 * 1024)
        failed_step = body.get("failed_step")
        if failed_step is not None:
            failed_step = int(failed_step)
        feedback = _community_repository().add_feedback(
            record_id,
            success=body.get("success"),
            failed_step=failed_step,
            note=str(body.get("note") or ""),
            environment=body.get("environment") or {},
            feedback_id=str(body.get("feedback_id") or ""),
        )
        _audit_event(
            "community_feedback_submitted",
            record_id=record_id,
            success=feedback["success"],
        )
        _json_response(handler, {"ok": True, "feedback": feedback}, 200 if feedback.get("duplicate") else 201)
    except FileNotFoundError as exc:
        _error(handler, str(exc), 404)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        _error(handler, str(exc), 422)
    except Exception as exc:
        _log(f"Community feedback failed: {record_id}: {exc}", "error")
        _error(handler, str(exc), 500)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_OPTIONS(self):
        if not _require_local_write_origin(self):
            return
        self.send_response(204)
        _send_local_cors_headers(self)
        self.end_headers()

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in ("/", "/index.html", "/replays", "/replays.html"):
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
            return

        if path in ("/store", "/store.html"):
            data = (ROOT / "store.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/community/records":
            try:
                query = parse_qs(parsed.query)
                records = _community_repository().list_records(
                    query=(query.get("q") or [""])[0],
                    tag=(query.get("tag") or [""])[0],
                )
                _json_response(self, {"ok": True, "records": records})
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        if path.startswith("/api/community/records/"):
            suffix = path.removeprefix("/api/community/records/").strip("/")
            parts = [unquote(part) for part in suffix.split("/") if part]
            if len(parts) == 2 and parts[1] == "download":
                try:
                    record_id = parts[0]
                    package_path = _community_repository().package_path(record_id)
                    data = package_path.read_bytes()
                    _community_repository().register_download(record_id)
                    _binary_response(
                        self,
                        data,
                        content_type="application/zip",
                        filename=f"{record_id}.gpa-record.zip",
                    )
                except FileNotFoundError as exc:
                    _error(self, str(exc), 404)
                except ValueError as exc:
                    _error(self, str(exc), 400)
                except Exception as exc:
                    _error(self, str(exc), 500)
                return
            if len(parts) == 1:
                try:
                    record = _community_repository().get_record(parts[0], include_feedback=True)
                    _json_response(self, {"ok": True, "record": record})
                except FileNotFoundError as exc:
                    _error(self, str(exc), 404)
                except ValueError as exc:
                    _error(self, str(exc), 400)
                except Exception as exc:
                    _error(self, str(exc), 500)
                return

        if path == "/api/replays":
            try:
                query = parse_qs(parsed.query)
                platform_name = (query.get("platform") or [""])[0] or None
                _json_response(self, {
                    "ok": True,
                    "replays": _replay_service().list_replays(platform=platform_name),
                })
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        if path.startswith("/api/replay-spaces/"):
            space_id = unquote(path.removeprefix("/api/replay-spaces/")).strip("/")
            try:
                space = _replay_service().spaces.get(space_id)
                _json_response(self, {"ok": True, "space": space})
            except FileNotFoundError as exc:
                _error(self, str(exc), 404)
            except ValueError as exc:
                _error(self, str(exc), 400)
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        if path.startswith("/api/replays/"):
            replay_id = unquote(path.removeprefix("/api/replays/")).strip("/")
            try:
                manifest = _replay_service().get_replay(replay_id)
                _json_response(self, {"ok": True, "replay": manifest.to_dict()})
            except FileNotFoundError:
                _error(self, f"Replay not found: {replay_id}", 404)
            except ValueError as exc:
                _error(self, str(exc), 400)
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        if self.path == "/api/status":
            _json_response(self, _public_state())
            return

        if self.path == "/api/workflows":
            try:
                workflows = _storage().list_workflows()
                _json_response(self, {"ok": True, "workflows": workflows})
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        if self.path == "/api/preview":
            with STATE_LOCK:
                preview = _preview_payload()
            _json_response(self, {"ok": True, "preview": preview})
            return

        if self.path.startswith("/api/runs"):
            workflow_id = ""
            if "?" in self.path:
                path, query = self.path.split("?", 1)
                if path != "/api/runs":
                    self.send_response(404)
                    self.end_headers()
                    return
                for part in query.split("&"):
                    key, _, value = part.partition("=")
                    if key == "workflow_id":
                        workflow_id = unquote(value)
            elif self.path != "/api/runs":
                self.send_response(404)
                self.end_headers()
                return
            _json_response(self, {"ok": True, "runs": _list_run_history(workflow_id)})
            return

        if self.path.startswith("/api/workflows/"):
            workflow_id = unquote(self.path.removeprefix("/api/workflows/")).strip("/")
            try:
                workflow, subgraphs = _storage().load(workflow_id)
                _json_response(self, {"ok": True, "workflow": _workflow_payload(workflow, subgraphs)})
            except FileNotFoundError:
                _error(self, f"Workflow not found: {workflow_id}", 404)
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not _require_local_write_origin(self):
            return
        if path == "/api/replays/intent":
            try:
                body = _read_json(self, max_bytes=256 * 1024)
                intent = _replay_service().parse_intent(
                    str(body.get("goal") or ""),
                    body.get("steps") or [],
                )
                _json_response(self, {"ok": True, "intent": intent})
            except PayloadTooLargeError as exc:
                _error(self, str(exc), 413)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                _error(self, str(exc), 422)
            except Exception as exc:
                _error(self, str(exc), 500)
            return
        if path.startswith("/api/replays/") and path.endswith("/plan"):
            replay_id = unquote(path.removeprefix("/api/replays/").removesuffix("/plan")).strip("/")
            try:
                body = _read_json(self, max_bytes=64 * 1024)
                plan = _replay_service().plan(
                    replay_id,
                    platform=str(body.get("platform") or "") or None,
                )
                _audit_event(
                    "replay_space_planned",
                    replay_id=replay_id,
                    space_id=plan.space_id,
                    platform=plan.platform,
                    compatibility=plan.compatibility.status,
                )
                _json_response(self, {"ok": True, "plan": plan.to_dict()}, 201)
            except FileNotFoundError:
                _error(self, f"Replay not found: {replay_id}", 404)
            except PayloadTooLargeError as exc:
                _error(self, str(exc), 413)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                _error(self, str(exc), 422)
            except Exception as exc:
                _error(self, str(exc), 500)
            return
        if path in {"/api/community/records", "/api/community/publish"}:
            _publish_community_record(self)
            return
        if path.startswith("/api/community/records/"):
            suffix = path.removeprefix("/api/community/records/").strip("/")
            parts = [unquote(part) for part in suffix.split("/") if part]
            if len(parts) == 2 and parts[1] == "import":
                _import_community_record(self, parts[0])
                return
            if len(parts) == 2 and parts[1] == "feedback":
                _submit_community_feedback(self, parts[0])
                return
        if path == "/api/client/heartbeat":
            _client_heartbeat(self)
            return
        if path == "/api/client/disconnect":
            _client_disconnect_request(self)
            return
        if path == "/api/record/start":
            _start_recording(self)
            return
        if path == "/api/record/stop":
            _stop_recording(self)
            return
        if self.path.startswith("/api/workflows/") and self.path.endswith("/run"):
            workflow_id = unquote(self.path.removeprefix("/api/workflows/").removesuffix("/run")).strip("/")
            _start_replay(self, workflow_id)
            return
        if self.path == "/api/run/arm":
            _arm_replay(self)
            return
        if self.path == "/api/run/stop":
            _stop_replay(self)
            return
        if self.path == "/api/run/panic":
            _panic_replay(self)
            return
        if self.path == "/api/preview/save":
            _save_preview(self)
            return
        if self.path == "/api/preview/discard":
            _discard_preview(self)
            return
        if self.path.startswith("/api/workflows/") and self.path.endswith("/update"):
            workflow_id = unquote(self.path.removeprefix("/api/workflows/").removesuffix("/update")).strip("/")
            _update_workflow(self, workflow_id)
            return
        if self.path.startswith("/api/workflows/") and self.path.endswith("/delete"):
            workflow_id = unquote(self.path.removeprefix("/api/workflows/").removesuffix("/delete")).strip("/")
            try:
                _storage().delete(workflow_id)
                _community_repository().forget_saved_workflow(workflow_id)
                _log(f"Workflow deleted: {workflow_id}", "warn")
                _json_response(self, {"ok": True, "workflow_id": workflow_id})
            except Exception as exc:
                _error(self, str(exc), 500)
            return

        self.send_response(404)
        self.end_headers()


def start_server():
    _ensure_visual_warmup_ready()
    try:
        demos = _ensure_demo_community_records()
        _log(f"Replay Store examples ready: {len(demos)}")
    except Exception as exc:
        _log(f"Replay Store example seeding skipped: {exc}", "warn")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    try:
        server = start_server()
    except Exception as exc:
        print(f"Server failed to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    shutting_down = False

    def shutdown_server() -> None:
        global shutting_down
        with STATE_LOCK:
            if shutting_down:
                return
            shutting_down = True
            SHUTDOWN_EVENT.set()
        active, _, _ = _abort_active_replay("Service shutdown requested.")
        if active:
            _panic_desktop_actions()
        if not _wait_for_replay_worker():
            _log("Replay worker did not stop within the shutdown grace period.", "warn")
        server.shutdown()
        server.server_close()

    def handle_shutdown_signal(signum, frame) -> None:
        shutdown_server()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    print(f"Server running at http://127.0.0.1:{PORT}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_server()
