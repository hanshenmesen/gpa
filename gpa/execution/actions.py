"""Low-level action execution: click, type, hotkey, scroll.

Uses pyautogui for mouse/keyboard control on macOS.
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional

import pyautogui

from gpa.runtime_config import RuntimeConfigurationError, env_bool

logger = logging.getLogger(__name__)

# Fail safe: move mouse to corner to abort
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05     # small pause between pyautogui calls

def _never_abort() -> bool:
    return False


_abort_checker: Callable[[], bool] = _never_abort
_panic_stop = threading.Event()
_panic_stop.set()
_held_keys: set[str] = set()
_held_mouse_buttons: set[str] = set()
_input_state_lock = threading.RLock()
_action_generation = 0
_generation_had_input = False
_input_activity_seen = False
_watchdog_process: Optional[subprocess.Popen] = None
_watchdog_sentinel: Optional[str] = None
_quarantine_process: Optional[subprocess.Popen] = None
_action_processes: set[subprocess.Popen] = set()
_thread_state = threading.local()
DESKTOP_AUTOMATION_ENV = "GPA_ENABLE_DESKTOP_AUTOMATION"
INPUT_WATCHDOG_ENV = "GPA_ENABLE_INPUT_WATCHDOG"
KEYBOARD_QUARANTINE_SECONDS_ENV = "GPA_KEYBOARD_QUARANTINE_SECONDS"
PROTECTED_INPUT_APPS_ENV = "GPA_PROTECTED_INPUT_APPS"
ALLOW_PROTECTED_INPUT_APPS_ENV = "GPA_ALLOW_PROTECTED_INPUT_APPS"
DEFAULT_KEYBOARD_QUARANTINE_SECONDS = 1.5
DEFAULT_PROTECTED_INPUT_APPS = ("chatgpt", "codex")

_KEY_MAP = {
    "cmd": "command",
    "command": "command",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "enter": "enter",
    "return": "enter",
    "tab": "tab",
    "esc": "escape",
    "escape": "escape",
    "space": "space",
    "backspace": "backspace",
    "delete": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
}

_PANIC_RELEASE_KEYS: tuple[str, ...] = ("command", "ctrl", "alt", "shift")


class ActionAborted(RuntimeError):
    """Raised when replay is stopped before or during a desktop action."""


def set_abort_checker(checker: Optional[Callable[[], bool]]) -> None:
    if checker is None:
        if hasattr(_thread_state, "abort_checker"):
            delattr(_thread_state, "abort_checker")
    else:
        _thread_state.abort_checker = checker


def set_expected_target_app(app_name: Optional[str]) -> None:
    value = str(app_name or "").strip()
    if value:
        _thread_state.expected_target_app = value
    elif hasattr(_thread_state, "expected_target_app"):
        delattr(_thread_state, "expected_target_app")


def bind_action_token(token: Optional[int]) -> None:
    _thread_state.action_token = token


def clear_action_token() -> None:
    if hasattr(_thread_state, "action_token"):
        delattr(_thread_state, "action_token")


def desktop_automation_enabled() -> bool:
    try:
        return env_bool(DESKTOP_AUTOMATION_ENV, False)
    except RuntimeConfigurationError as exc:
        logger.error("Desktop automation is disabled: %s", exc)
        return False


def _mark_input_activity() -> None:
    global _generation_had_input, _input_activity_seen
    with _input_state_lock:
        _generation_had_input = True
        _input_activity_seen = True


def abort_actions(
    token: Optional[int] = None,
    *,
    stop_watchdog: bool = True,
    quarantine: bool = False,
) -> bool:
    """Invalidate the current desktop-action generation without emitting input."""
    global _action_generation, _generation_had_input
    with _input_state_lock:
        current = _action_generation
        if token is not None and token != current:
            return False
        had_input = _generation_had_input
        _panic_stop.set()
        _action_generation += 1
        _generation_had_input = False
        action_processes = list(_action_processes)
    for proc in action_processes:
        terminate_process(proc)
    if stop_watchdog:
        _stop_input_watchdog()
    if quarantine and had_input:
        _start_keyboard_quarantine()
    return True


def finish_actions(token: Optional[int] = None) -> bool:
    """End a successful replay while keeping the crash watchdog armed."""
    return abort_actions(token, stop_watchdog=False, quarantine=True)


def arm_actions() -> int:
    global _action_generation, _generation_had_input
    with _input_state_lock:
        _action_generation += 1
        _generation_had_input = False
        token = _action_generation
    if desktop_automation_enabled():
        _panic_stop.clear()
        _start_input_watchdog()
    else:
        _panic_stop.set()
    bind_action_token(token)
    return token


def panic_stop(token: Optional[int] = None) -> None:
    with _input_state_lock:
        has_tracked_inputs = bool(_held_keys or _held_mouse_buttons)
        had_input = _generation_had_input or _input_activity_seen
    # A stale token means another thread already advanced the action generation
    # before this one unwound. The web console's Stop handler, for example, calls
    # abort_actions() (no token) the instant the user clicks Stop, and only later
    # does the replay worker reach its finally block and call panic_stop(token)
    # with what is now a stale token. We must NOT bail in that case: the physical
    # safety cleanup below — releasing stuck modifier keys and installing the
    # keyboard quarantine that swallows input events already queued in the macOS
    # window server — is exactly what prevents a burst of buffered keystrokes and
    # clicks from flushing onto the desktop after a manual interrupt. The panic
    # flag itself is already armed by whichever call advanced the generation, so
    # we do not re-set it here (that would risk clobbering a freshly armed run).
    abort_actions(token)
    if has_tracked_inputs or had_input:
        _start_keyboard_quarantine()
    if has_tracked_inputs:
        _release_inputs_safely()


def emergency_release_inputs() -> None:
    """Best-effort physical release for a replacement process after a worker crash."""
    abort_actions()
    _release_modifiers_with_quartz()
    for key in ("command", "ctrl", "shift", "alt", "option"):
        try:
            pyautogui.keyUp(key)
        except Exception:
            logger.debug("Could not force-release key %s", key, exc_info=True)
    for button in ("left", "right", "middle"):
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            logger.debug("Could not force-release mouse button %s", button, exc_info=True)


def actions_stopped() -> bool:
    return _panic_stop.is_set() or not desktop_automation_enabled()


def _ensure_not_aborted() -> None:
    if not desktop_automation_enabled():
        raise ActionAborted(
            f"Desktop automation is disabled. Set {DESKTOP_AUTOMATION_ENV}=1 before starting a trusted replay."
        )
    token = getattr(_thread_state, "action_token", None)
    with _input_state_lock:
        generation = _action_generation
    if token is not None and token != generation:
        raise ActionAborted("Replay action token is stale; refusing desktop input.")
    checker = getattr(_thread_state, "abort_checker", _abort_checker)
    if _panic_stop.is_set() or checker():
        raise ActionAborted("Replay stopped before desktop action could complete.")


def ensure_action_allowed() -> None:
    _ensure_not_aborted()


def _normalise_key(key: str) -> str:
    return _KEY_MAP.get(str(key or "").strip().lower(), str(key or "").strip().lower())


def _key_down(key: str) -> None:
    _ensure_not_aborted()
    key = _normalise_key(key)
    with _input_state_lock:
        _held_keys.add(key)
    try:
        _mark_input_activity()
        pyautogui.keyDown(key)
        _ensure_not_aborted()
    except BaseException:
        # Panic may release the pre-registered key while keyDown is still
        # blocked inside the OS. If the down event lands afterwards, emit one
        # final up so the physical event sequence cannot end in a stuck key.
        try:
            pyautogui.keyUp(key)
        except Exception:
            logger.debug("Could not release key %s after interrupted keyDown", key, exc_info=True)
        with _input_state_lock:
            _held_keys.discard(key)
        raise


def _key_up(key: str) -> None:
    key = _normalise_key(key)
    try:
        _mark_input_activity()
        pyautogui.keyUp(key)
    finally:
        with _input_state_lock:
            _held_keys.discard(key)


def _mouse_down(button: str = "left") -> None:
    _ensure_not_aborted()
    button = str(button or "left")
    with _input_state_lock:
        _held_mouse_buttons.add(button)
    try:
        _mark_input_activity()
        pyautogui.mouseDown(button=button)
        _ensure_not_aborted()
    except BaseException:
        # See _key_down: cancellation can overtake a blocked mouseDown call.
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            logger.debug(
                "Could not release mouse button %s after interrupted mouseDown",
                button,
                exc_info=True,
            )
        with _input_state_lock:
            _held_mouse_buttons.discard(button)
        raise


def start_action_process(command: list[str], **popen_kwargs) -> subprocess.Popen:
    """Atomically validate, spawn, and register an action-producing child."""
    popen_kwargs.setdefault("start_new_session", True)
    with _input_state_lock:
        _ensure_not_aborted()
        proc = subprocess.Popen(command, **popen_kwargs)
        _action_processes.add(proc)
        _sync_watchdog_processes_locked()
        return proc


def track_action_process(proc: subprocess.Popen) -> None:
    with _input_state_lock:
        _action_processes.add(proc)
        _sync_watchdog_processes_locked()


def untrack_action_process(proc: subprocess.Popen) -> None:
    with _input_state_lock:
        _action_processes.discard(proc)
        _sync_watchdog_processes_locked()


def _sync_watchdog_processes_locked() -> None:
    sentinel = _watchdog_sentinel
    if not sentinel or not os.path.exists(sentinel):
        return
    pids = sorted(
        {
            int(proc.pid)
            for proc in _action_processes
            if getattr(proc, "pid", None) and proc.poll() is None
        }
    )
    temp_path = f"{sentinel}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(str(pid) for pid in pids))
        os.replace(temp_path, sentinel)
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def terminate_process(proc: subprocess.Popen) -> None:
    """Terminate a process group started for a desktop action and reap it."""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except Exception:
            return
    try:
        proc.wait(timeout=0.5)
    except Exception:
        pass


def _run_action_subprocess(
    command: list[str],
    *,
    timeout: float,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run an action-producing child while honoring replay cancellation."""
    proc = start_action_process(
        command,
        stdout=stdout,
        stderr=stderr,
        text=text,
    )
    started = time.monotonic()
    try:
        while proc.poll() is None:
            _ensure_not_aborted()
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.02)
        output, error = proc.communicate()
        _ensure_not_aborted()
        completed = subprocess.CompletedProcess(command, proc.returncode, output, error)
        if proc.returncode:
            raise subprocess.CalledProcessError(
                proc.returncode,
                command,
                output=output,
                stderr=error,
            )
        return completed
    except BaseException:
        terminate_process(proc)
        raise
    finally:
        untrack_action_process(proc)


def _mouse_up(button: str = "left") -> None:
    button = str(button or "left")
    try:
        _mark_input_activity()
        pyautogui.mouseUp(button=button)
    finally:
        with _input_state_lock:
            _held_mouse_buttons.discard(button)


def _release_inputs_safely() -> None:
    """Release any key/button state that may be stuck after an abort."""
    with _input_state_lock:
        keys = list(dict.fromkeys(_held_keys))
        buttons = list(dict.fromkeys(_held_mouse_buttons))
    if any(key in _PANIC_RELEASE_KEYS for key in keys):
        _release_modifiers_with_quartz()
    for key in keys:
        try:
            pyautogui.keyUp(key)
        except Exception:
            logger.debug("Could not release key %s during panic stop", key, exc_info=True)
        finally:
            with _input_state_lock:
                _held_keys.discard(key)
    for button in buttons:
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            logger.debug("Could not release mouse button %s during panic stop", button, exc_info=True)
        finally:
            with _input_state_lock:
                _held_mouse_buttons.discard(button)


def _release_modifiers_with_quartz() -> None:
    if sys.platform != "darwin":
        return
    try:
        import Quartz
    except Exception:
        return
    # left/right command, shift, option, control, caps lock, fn
    keycodes = (0x37, 0x36, 0x38, 0x3C, 0x3A, 0x3D, 0x3B, 0x3E, 0x39, 0x3F)
    for keycode in keycodes:
        try:
            event = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except Exception:
            logger.debug("Could not release Quartz keycode %s", keycode, exc_info=True)


def _keyboard_quarantine_seconds() -> float:
    raw = os.environ.get(KEYBOARD_QUARANTINE_SECONDS_ENV)
    if raw is None:
        return DEFAULT_KEYBOARD_QUARANTINE_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_KEYBOARD_QUARANTINE_SECONDS


def _start_keyboard_quarantine(duration: Optional[float] = None) -> None:
    """Briefly swallow macOS keyboard and mouse (click/drag/scroll) events during
    shutdown/abort cleanup.

    On abort we must swallow both keyboard *and* mouse button/scroll events. Only
    filtering keyboard events lets OS-queued clicks, drags, and scroll wheel events
    flush onto the desktop as a burst after replay is stopped. Pure mouse-move
    events are intentionally left untouched so the pointer never freezes and the
    pyautogui fail-safe corner stays reachable.
    """
    global _quarantine_process
    if sys.platform != "darwin":
        return
    seconds = _keyboard_quarantine_seconds() if duration is None else max(0.0, float(duration))
    if seconds <= 0:
        return
    with _input_state_lock:
        if _quarantine_process is not None and _quarantine_process.poll() is None:
            return
    fd, ready_path = tempfile.mkstemp(prefix=f"gpa-keyboard-quarantine-{os.getpid()}-", suffix=".ready")
    os.close(fd)
    try:
        os.remove(ready_path)
    except OSError:
        pass
    code = r'''
import os
import signal
import sys
import time

duration = max(0.0, float(sys.argv[1]))
ready_path = sys.argv[2]

def mark_ready():
    try:
        with open(ready_path, "w", encoding="utf-8"):
            pass
    except OSError:
        pass

def cleanup():
    try:
        os.remove(ready_path)
    except OSError:
        pass

try:
    import Quartz
except Exception:
    mark_ready()
    time.sleep(duration)
    cleanup()
    raise SystemExit(0)

def swallow_input(proxy, event_type, event, refcon):
    return None

mask = 0
for _event_name in (
    "kCGEventKeyDown",
    "kCGEventKeyUp",
    "kCGEventFlagsChanged",
    "kCGEventLeftMouseDown",
    "kCGEventLeftMouseUp",
    "kCGEventRightMouseDown",
    "kCGEventRightMouseUp",
    "kCGEventOtherMouseDown",
    "kCGEventOtherMouseUp",
    "kCGEventLeftMouseDragged",
    "kCGEventRightMouseDragged",
    "kCGEventOtherMouseDragged",
    "kCGEventScrollWheel",
):
    _event_id = getattr(Quartz, _event_name, None)
    if _event_id is not None:
        mask |= (1 << _event_id)
tap = Quartz.CGEventTapCreate(
    Quartz.kCGSessionEventTap,
    Quartz.kCGHeadInsertEventTap,
    Quartz.kCGEventTapOptionDefault,
    mask,
    swallow_input,
    None,
)
if tap is None:
    mark_ready()
    time.sleep(duration)
    cleanup()
    raise SystemExit(0)

source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
Quartz.CFRunLoopAddSource(
    Quartz.CFRunLoopGetCurrent(),
    source,
    Quartz.kCFRunLoopCommonModes,
)
Quartz.CGEventTapEnable(tap, True)
mark_ready()
deadline = time.monotonic() + duration
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    Quartz.CFRunLoopRunInMode(
        Quartz.kCFRunLoopDefaultMode,
        min(0.05, remaining),
        False,
    )
cleanup()
'''
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(seconds), ready_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with _input_state_lock:
            _quarantine_process = proc
        deadline = time.monotonic() + min(0.35, seconds)
        while time.monotonic() < deadline and proc.poll() is None:
            if os.path.exists(ready_path):
                break
            time.sleep(0.01)
    except Exception:
        try:
            os.remove(ready_path)
        except OSError:
            pass
        logger.debug("Could not start keyboard quarantine", exc_info=True)


def _read_clipboard_bytes() -> bytes:
    try:
        completed = subprocess.run(
            ["pbpaste"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
        return completed.stdout or b""
    except Exception:
        return b""


def _write_clipboard_bytes(data: bytes) -> None:
    subprocess.run(
        ["pbcopy"],
        input=data,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=2,
    )


def _write_clipboard_text(text: str) -> None:
    _write_clipboard_bytes(str(text or "").encode("utf-8"))


def _frontmost_process_identity() -> tuple[int, str]:
    script = (
        'tell application "System Events"\n'
        '  set frontProc to first application process whose frontmost is true\n'
        '  return (unix id of frontProc as text) & tab & (name of frontProc as text)\n'
        'end tell'
    )
    completed = subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=2,
        check=True,
    )
    raw_pid, _, app_name = completed.stdout.strip().partition("\t")
    pid = int(raw_pid)
    normalized_actual = app_name.casefold().replace(" browser", "").strip()
    configured_protected = str(
        os.environ.get(PROTECTED_INPUT_APPS_ENV, ",".join(DEFAULT_PROTECTED_INPUT_APPS))
        or ""
    )
    protected_apps = {
        item.casefold().replace(" browser", "").strip()
        for item in configured_protected.split(",")
        if item.strip()
    }
    allow_protected = str(
        os.environ.get(ALLOW_PROTECTED_INPUT_APPS_ENV, "") or ""
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if not allow_protected and any(
        protected == normalized_actual
        or protected in normalized_actual
        or normalized_actual in protected
        for protected in protected_apps
    ):
        raise ActionAborted(
            f"Refusing automated keyboard input in protected app {app_name}. "
            f"Set {ALLOW_PROTECTED_INPUT_APPS_ENV}=1 only for an explicitly trusted workflow."
        )
    expected = str(getattr(_thread_state, "expected_target_app", "") or "").strip()
    if expected:
        normalized_expected = expected.casefold().replace(" browser", "").strip()
        aliases = {normalized_expected, normalized_expected.replace("google ", "")}
        if normalized_actual not in aliases and not any(
            alias and (alias in normalized_actual or normalized_actual in alias)
            for alias in aliases
        ):
            raise ActionAborted(
                f"Focused app changed before keyboard input: expected {expected}, got {app_name}."
            )
    return pid, app_name


def _menu_command(command: str, *, expected_identity: Optional[tuple[int, str]] = None) -> None:
    """Invoke a front-app Edit menu command without synthesizing a hotkey."""
    if sys.platform != "darwin":
        raise RuntimeError("Menu commands are only supported on macOS.")
    command = str(command or "").strip().casefold()
    candidates = {
        "paste": (("Edit", "Paste"), ("编辑", "粘贴")),
        "copy": (("Edit", "Copy"), ("编辑", "拷贝"), ("编辑", "复制")),
    }.get(command)
    if not candidates:
        raise ValueError(f"Unsupported menu command: {command}")
    expected_pid, _ = expected_identity or _frontmost_process_identity()
    attempts = []
    for menu_name, item_name in candidates:
        attempts.append(
            f'''try
  click menu item "{_apple_script_string(item_name)}" of menu "{_apple_script_string(menu_name)}" of menu bar 1
  return "ok"
end try'''
        )
    script = (
        'tell application "System Events"\n'
        '  set frontProc to first application process whose frontmost is true\n'
        f'  if (unix id of frontProc) is not {expected_pid} then error "Focused app changed before {command}"\n'
        '  tell frontProc\n'
        f"{chr(10).join(attempts)}\n"
        f'    error "{command} menu command is unavailable"\n'
        "  end tell\n"
        "end tell\n"
    )
    _ensure_not_aborted()
    _mark_input_activity()
    _run_action_subprocess(
        ["osascript", "-e", script],
        timeout=3,
    )
    _ensure_not_aborted()


def _apple_script_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _paste_text_via_clipboard(text: str) -> None:
    expected_identity = _frontmost_process_identity()
    previous = _read_clipboard_bytes()
    try:
        _ensure_not_aborted()
        _write_clipboard_text(text)
        _menu_command("paste", expected_identity=expected_identity)
        _sleep_interruptible(0.08)
    finally:
        try:
            _write_clipboard_bytes(previous)
        except Exception:
            logger.debug("Could not restore clipboard after safe text paste", exc_info=True)


def _menu_command_for_hotkey(combo: str) -> str:
    parts = [_normalise_key(k) for k in combo.split("+") if k.strip()]
    if len(parts) != 2 or parts[0] != "command":
        return ""
    if parts[1] == "v":
        return "paste"
    if parts[1] == "c":
        return "copy"
    return ""


def _press_hotkey_macos(combo: str) -> None:
    expected_pid, _ = _frontmost_process_identity()
    parts = [_normalise_key(k) for k in combo.split("+") if k.strip()]
    if not parts:
        raise ValueError("Hotkey must include at least one key.")
    modifiers = {
        "command": "command down",
        "ctrl": "control down",
        "alt": "option down",
        "shift": "shift down",
    }
    key_codes = {
        "enter": 36,
        "tab": 48,
        "space": 49,
        "backspace": 51,
        "delete": 51,
        "escape": 53,
        "left": 123,
        "right": 124,
        "down": 125,
        "up": 126,
    }
    modifier_values = [modifiers[item] for item in parts[:-1] if item in modifiers]
    key = parts[-1]
    using = f" using {{{', '.join(modifier_values)}}}" if modifier_values else ""
    if key in key_codes:
        action = f"key code {key_codes[key]}{using}"
    elif len(key) == 1:
        action = f'keystroke "{_apple_script_string(key)}"{using}'
    else:
        raise ValueError(f"Unsupported macOS hotkey key: {key}")
    script = (
        'tell application "System Events"\n'
        '  set frontProc to first application process whose frontmost is true\n'
        f'  if (unix id of frontProc) is not {expected_pid} then error "Focused app changed before hotkey"\n'
        f"  {action}\n"
        "end tell"
    )
    _ensure_not_aborted()
    _mark_input_activity()
    _run_action_subprocess(["osascript", "-e", script], timeout=3)
    _ensure_not_aborted()


def _input_watchdog_enabled() -> bool:
    raw = str(os.environ.get(INPUT_WATCHDOG_ENV, "1") or "").strip().casefold()
    return raw not in {"0", "false", "no", "off"} and sys.platform == "darwin"


def _start_input_watchdog() -> None:
    global _watchdog_process, _watchdog_sentinel
    if not _input_watchdog_enabled():
        return
    with _input_state_lock:
        if _watchdog_process is not None and _watchdog_process.poll() is None:
            return
        fd, sentinel = tempfile.mkstemp(prefix=f"gpa-input-{os.getpid()}-", suffix=".watch")
        os.close(fd)
        quarantine_seconds = _keyboard_quarantine_seconds()
        code = r'''
import os
import signal
import sys
import time

parent_pid = int(sys.argv[1])
sentinel = sys.argv[2]
quarantine_seconds = max(0.0, float(sys.argv[3]))

def parent_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def release_modifiers():
    try:
        import Quartz
    except Exception:
        return
    for keycode in (0x37, 0x36, 0x38, 0x3C, 0x3A, 0x3D, 0x3B, 0x3E, 0x39, 0x3F):
        try:
            event = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except Exception:
            pass

def quarantine_keyboard(duration):
    if duration <= 0:
        return
    try:
        import Quartz
    except Exception:
        time.sleep(duration)
        return

    def swallow_input(proxy, event_type, event, refcon):
        return None

    mask = 0
    for _event_name in (
        "kCGEventKeyDown",
        "kCGEventKeyUp",
        "kCGEventFlagsChanged",
        "kCGEventLeftMouseDown",
        "kCGEventLeftMouseUp",
        "kCGEventRightMouseDown",
        "kCGEventRightMouseUp",
        "kCGEventOtherMouseDown",
        "kCGEventOtherMouseUp",
        "kCGEventLeftMouseDragged",
        "kCGEventRightMouseDragged",
        "kCGEventOtherMouseDragged",
        "kCGEventScrollWheel",
    ):
        _event_id = getattr(Quartz, _event_name, None)
        if _event_id is not None:
            mask |= (1 << _event_id)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,
        mask,
        swallow_input,
        None,
    )
    if tap is None:
        time.sleep(duration)
        return
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(),
        source,
        Quartz.kCFRunLoopCommonModes,
    )
    Quartz.CGEventTapEnable(tap, True)
    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        Quartz.CFRunLoopRunInMode(
            Quartz.kCFRunLoopDefaultMode,
            min(0.05, remaining),
            False,
        )

def terminate_action_groups():
    try:
        with open(sentinel, "r", encoding="utf-8") as handle:
            pids = [int(line.strip()) for line in handle if line.strip()]
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
    if pids:
        time.sleep(0.05)
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass

while os.path.exists(sentinel) and parent_alive(parent_pid):
    time.sleep(0.05)

if os.path.exists(sentinel):
    terminate_action_groups()
    quarantine_keyboard(quarantine_seconds)
    release_modifiers()
    try:
        os.remove(sentinel)
    except OSError:
        pass
'''
        try:
            _watchdog_process = subprocess.Popen(
                [sys.executable, "-c", code, str(os.getpid()), sentinel, str(quarantine_seconds)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _watchdog_sentinel = sentinel
            _sync_watchdog_processes_locked()
        except Exception:
            try:
                os.remove(sentinel)
            except OSError:
                pass
            logger.debug("Could not start input watchdog", exc_info=True)


def _stop_input_watchdog() -> None:
    global _watchdog_process, _watchdog_sentinel
    with _input_state_lock:
        sentinel = _watchdog_sentinel
        proc = _watchdog_process
        _watchdog_sentinel = None
        _watchdog_process = None
    if sentinel:
        try:
            os.remove(sentinel)
        except OSError:
            pass
    if proc is not None and proc.poll() is None:
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            terminate_process(proc)


def _sleep_interruptible(duration: float) -> None:
    deadline = time.monotonic() + max(0.0, duration)
    while True:
        _ensure_not_aborted()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.02, remaining))


def click(x: float, y: float, button: str = "left", double: bool = False) -> None:
    """Click at screen coordinates (x, y)."""
    _ensure_not_aborted()
    ix, iy = int(round(x)), int(round(y))
    logger.debug(f"Click {'double' if double else ''} {button} at ({ix}, {iy})")
    _mark_input_activity()
    pyautogui.moveTo(ix, iy)
    clicks = 2 if double else 1
    for _ in range(clicks):
        _ensure_not_aborted()
        _mouse_down(button)
        try:
            _sleep_interruptible(0.03)
        finally:
            _mouse_up(button)
        _sleep_interruptible(0.05)
    _ensure_not_aborted()


def right_click(x: float, y: float) -> None:
    _ensure_not_aborted()
    click(x, y, button="right")
    _ensure_not_aborted()


def type_text(text: str, interval: float = 0.03) -> None:
    """Type text with optional inter-character delay."""
    logger.debug(f"Type: {text!r}")
    if sys.platform == "darwin":
        _paste_text_via_clipboard(text)
        _ensure_not_aborted()
        return
    for ch in text:
        _ensure_not_aborted()
        _mark_input_activity()
        pyautogui.typewrite(ch, interval=0)
        _sleep_interruptible(interval)
    _ensure_not_aborted()


def press_hotkey(combo: str) -> None:
    """Press a key combination like 'cmd+s', 'ctrl+c', 'tab', 'enter'."""
    _ensure_not_aborted()
    logger.debug(f"Hotkey: {combo}")
    menu_command = _menu_command_for_hotkey(combo)
    if menu_command and sys.platform == "darwin":
        _menu_command(menu_command)
        _ensure_not_aborted()
        return
    if sys.platform == "darwin":
        _press_hotkey_macos(combo)
        return
    parts = [_normalise_key(k) for k in combo.split("+") if k.strip()]
    pressed: list[str] = []
    try:
        for key in parts:
            _ensure_not_aborted()
            _key_down(key)
            pressed.append(key)
            _sleep_interruptible(0.02)
    finally:
        for key in reversed(pressed):
            _key_up(key)
            time.sleep(0.01)
        # Defensive release for modifier keys. If a replay is interrupted around
        # a hotkey, a stuck modifier is much more dangerous than an extra key-up.
        _release_inputs_safely()
    _ensure_not_aborted()


def scroll(x: float, y: float, dx: int = 0, dy: int = 0) -> None:
    """Scroll at (x, y) by (dx, dy) clicks."""
    _ensure_not_aborted()
    logger.debug(f"Scroll at ({x:.0f}, {y:.0f}) dx={dx} dy={dy}")
    _mark_input_activity()
    pyautogui.moveTo(int(round(x)), int(round(y)))
    if dy != 0:
        _mark_input_activity()
        pyautogui.scroll(dy)
    if dx != 0:
        _mark_input_activity()
        pyautogui.hscroll(dx)
    _ensure_not_aborted()


def drag(start_x: float, start_y: float, end_x: float, end_y: float, duration: float = 0.3) -> None:
    """Drag from one screen coordinate to another."""
    _ensure_not_aborted()
    sx, sy = int(round(start_x)), int(round(start_y))
    ex, ey = int(round(end_x)), int(round(end_y))
    logger.debug(f"Drag from ({sx}, {sy}) to ({ex}, {ey}) duration={duration:.2f}")
    _mark_input_activity()
    pyautogui.moveTo(sx, sy)
    _ensure_not_aborted()
    total = max(0.05, float(duration))
    steps = max(2, int(total / 0.03))
    _mouse_down("left")
    try:
        for idx in range(1, steps + 1):
            _ensure_not_aborted()
            ratio = idx / steps
            x = sx + (ex - sx) * ratio
            y = sy + (ey - sy) * ratio
            _mark_input_activity()
            pyautogui.moveTo(int(round(x)), int(round(y)))
            _sleep_interruptible(total / steps)
    finally:
        _mouse_up("left")
    _ensure_not_aborted()


def move_to(x: float, y: float, duration: float = 0.1) -> None:
    _ensure_not_aborted()
    _mark_input_activity()
    pyautogui.moveTo(int(round(x)), int(round(y)), duration=duration)
    _ensure_not_aborted()


def _shutdown_actions() -> None:
    """Finish native-input cleanup and reap its helper before Python exits."""
    panic_stop()
    with _input_state_lock:
        proc = _quarantine_process
    if proc is None or proc.poll() is not None:
        if proc is not None:
            try:
                proc.wait(timeout=0)
            except Exception:
                pass
        return
    try:
        # The helper intentionally stays alive for the quarantine window.  A
        # bounded wait prevents a live child from leaking out of the replay
        # worker and avoids Python's "subprocess is still running" warning.
        proc.wait(timeout=_keyboard_quarantine_seconds() + 0.5)
    except subprocess.TimeoutExpired:
        terminate_process(proc)
    except Exception:
        logger.debug("Could not reap keyboard quarantine helper", exc_info=True)


atexit.register(_shutdown_actions)
