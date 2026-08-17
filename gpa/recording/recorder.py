"""Screen + input recorder.

Records:
  - Screenshots at each action (via mss)
  - Mouse clicks (position, button)
  - Keyboard input (text typed, hotkeys)
  - Scroll events
  - Timing and active app metadata

Uses a raw Quartz event tap on macOS and pynput elsewhere.  The Quartz backend
deliberately avoids ``TISCopyCurrentKeyboardInputSource``: that macOS API can
abort the entire Python process when called from a background listener thread.
Screenshots are captured with mss.
"""
from __future__ import annotations

import logging
import math
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from PIL import Image

logger = logging.getLogger(__name__)

INPUT_CAPTURE_BACKEND_ENV = "GPA_RECORDING_INPUT_BACKEND"


# macOS virtual key codes.  Keeping this intentionally small is safer than
# asking TextInputSources to translate every event on a listener thread.  The
# recorder still captures pasted text exactly via the clipboard path.
_MAC_KEY_NAMES = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x",
    8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e", 15: "r",
    16: "y", 17: "t", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6",
    23: "5", 24: "=", 25: "9", 26: "7", 27: "-", 28: "8", 29: "0",
    30: "]", 31: "o", 32: "u", 33: "[", 34: "i", 35: "p", 36: "enter",
    37: "l", 38: "j", 39: "'", 40: "k", 41: ";", 42: "\\", 43: ",",
    44: "/", 45: "n", 46: "m", 47: ".", 48: "tab", 49: "space",
    50: "`", 51: "backspace", 53: "esc", 55: "cmd", 56: "shift",
    58: "alt", 59: "ctrl", 60: "shift", 61: "alt", 62: "ctrl",
    96: "f5", 97: "f6", 98: "f7", 99: "f3", 100: "f8", 101: "f9",
    103: "f11", 109: "f10", 111: "f12", 118: "f4", 120: "f2",
    122: "f1", 123: "left", 124: "right", 125: "down", 126: "up",
}
_MAC_SHIFTED_KEYS = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^",
    "7": "&", "8": "*", "9": "(", "0": ")", "-": "_", "=": "+",
    "[": "{", "]": "}", "\\": "|", ";": ":", "'": '"', ",": "<",
    ".": ">", "/": "?", "`": "~",
}


def _mac_key_name(keycode: int, *, shifted: bool = False) -> str:
    name = _MAC_KEY_NAMES.get(int(keycode), "")
    if not shifted:
        return name
    if len(name) == 1 and name.isalpha():
        return name.upper()
    return _MAC_SHIFTED_KEYS.get(name, name)


class _QuartzInputListener:
    """Listen-only macOS input capture without TextInputSources translation."""

    def __init__(self, recorder: "Recorder"):
        self._recorder = recorder
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._error: Optional[BaseException] = None
        self._tap = None
        self._run_loop = None
        self._callback = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Quartz input listener is already active.")
        self._stop_event.clear()
        self._ready_event.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="gpa-quartz-input-listener",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=2.0):
            self.stop()
            self.join(1.0)
            raise RuntimeError("Timed out starting the Quartz input listener.")
        if self._error is not None:
            error = self._error
            self.stop()
            self.join(1.0)
            raise RuntimeError(f"Could not start Quartz input capture: {error}") from error

    def stop(self) -> None:
        self._stop_event.set()
        quartz = getattr(self, "_quartz", None)
        if quartz is not None and self._run_loop is not None:
            try:
                quartz.CFRunLoopStop(self._run_loop)
            except Exception:
                logger.debug("Could not stop Quartz input run loop", exc_info=True)

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            import Quartz

            self._quartz = Quartz
            event_names = (
                "kCGEventKeyDown", "kCGEventKeyUp", "kCGEventFlagsChanged",
                "kCGEventLeftMouseDown", "kCGEventLeftMouseUp",
                "kCGEventRightMouseDown", "kCGEventRightMouseUp",
                "kCGEventOtherMouseDown", "kCGEventOtherMouseUp",
                "kCGEventScrollWheel",
            )
            event_types = [getattr(Quartz, name) for name in event_names]
            mask = 0
            for event_type in event_types:
                mask |= 1 << int(event_type)

            def callback(proxy, event_type, event, refcon):
                try:
                    self._dispatch_event(Quartz, int(event_type), event)
                except BaseException:
                    logger.exception("Quartz recorder callback failed")
                return event

            self._callback = callback
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                callback,
                None,
            )
            if self._tap is None:
                raise RuntimeError("Input Monitoring permission is unavailable")
            source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._run_loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(
                self._run_loop,
                source,
                Quartz.kCFRunLoopCommonModes,
            )
            Quartz.CGEventTapEnable(self._tap, True)
            self._ready_event.set()
            while not self._stop_event.is_set():
                Quartz.CFRunLoopRunInMode(
                    Quartz.kCFRunLoopDefaultMode,
                    0.05,
                    False,
                )
        except BaseException as exc:
            self._error = exc
            self._ready_event.set()
        finally:
            quartz = getattr(self, "_quartz", None)
            if quartz is not None and self._tap is not None:
                try:
                    quartz.CGEventTapEnable(self._tap, False)
                except Exception:
                    logger.debug("Could not disable Quartz input tap", exc_info=True)

    def _dispatch_event(self, Quartz, event_type: int, event) -> None:
        if not self._recorder._running:
            return
        if event_type in {Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp}:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            flags = int(Quartz.CGEventGetFlags(event))
            shifted = bool(flags & int(Quartz.kCGEventFlagMaskShift))
            name = _mac_key_name(int(keycode), shifted=shifted)
            if not name:
                logger.debug("Ignoring unmapped macOS keycode %s", keycode)
                return
            if event_type == Quartz.kCGEventKeyDown:
                self._recorder._on_key_name_press(name)
            else:
                self._recorder._on_key_name_release(name)
            return
        if event_type == Quartz.kCGEventFlagsChanged:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            name = _mac_key_name(int(keycode))
            flag_by_name = {
                "cmd": Quartz.kCGEventFlagMaskCommand,
                "ctrl": Quartz.kCGEventFlagMaskControl,
                "alt": Quartz.kCGEventFlagMaskAlternate,
                "shift": Quartz.kCGEventFlagMaskShift,
            }
            if name not in flag_by_name:
                return
            flags = int(Quartz.CGEventGetFlags(event))
            if flags & int(flag_by_name[name]):
                self._recorder._on_key_name_press(name)
            else:
                self._recorder._on_key_name_release(name)
            return
        if event_type == Quartz.kCGEventScrollWheel:
            point = Quartz.CGEventGetLocation(event)
            dy = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventDeltaAxis1
            )
            dx = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGScrollWheelEventDeltaAxis2
            )
            self._recorder._on_scroll(point.x, point.y, dx, dy)
            return
        down_types = {
            Quartz.kCGEventLeftMouseDown: "left",
            Quartz.kCGEventRightMouseDown: "right",
            Quartz.kCGEventOtherMouseDown: "middle",
        }
        up_types = {
            Quartz.kCGEventLeftMouseUp: "left",
            Quartz.kCGEventRightMouseUp: "right",
            Quartz.kCGEventOtherMouseUp: "middle",
        }
        if event_type in down_types or event_type in up_types:
            point = Quartz.CGEventGetLocation(event)
            pressed = event_type in down_types
            button = (down_types if pressed else up_types)[event_type]
            self._recorder._on_click(point.x, point.y, button, pressed)


# ──────────────────────────────────────────────────────────────────────────── #
# Action types                                                                  #
# ──────────────────────────────────────────────────────────────────────────── #

@dataclass
class RecordedEvent:
    event_type: str          # "click" | "drag" | "type" | "hotkey" | "scroll"
    x: float = 0.0
    y: float = 0.0
    button: str = ""         # "left" | "right" | "middle"
    value: str = ""          # typed text or hotkey string e.g. "cmd+s"
    scroll_dx: int = 0
    scroll_dy: int = 0
    start_x: float = 0.0
    start_y: float = 0.0
    end_x: float = 0.0
    end_y: float = 0.0
    duration_seconds: float = 0.0
    clipboard_before: str = ""
    clipboard_after: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    screenshot: Optional[Image.Image] = field(default=None, repr=False)
    active_app: str = ""
    pause_before: float = 0.0   # time since last event


@dataclass
class Recording:
    events: list[RecordedEvent] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)


# ──────────────────────────────────────────────────────────────────────────── #
# Screenshot capture                                                           #
# ──────────────────────────────────────────────────────────────────────────── #

def capture_screenshot() -> Image.Image:
    """Capture the full screen as a PIL Image."""
    import mss
    with mss.mss() as sct:
        monitor = sct.monitors[1]   # primary display
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img


def get_active_app() -> str:
    """Return the name of the currently focused application (macOS)."""
    if platform.system() != "Darwin":
        return ""
    try:
        import subprocess
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process '
             'whose frontmost is true'],
            capture_output=True, text=True, timeout=1,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def get_active_window_bounds() -> Optional[list[float]]:
    """Return the frontmost window as [x, y, width, height] on macOS."""
    if platform.system() != "Darwin":
        return None
    script = (
        'tell application "System Events"\n'
        "  set frontProcess to first process whose frontmost is true\n"
        "  set windowPosition to position of front window of frontProcess\n"
        "  set windowSize to size of front window of frontProcess\n"
        "  return (item 1 of windowPosition as string) & \",\" & "
        "(item 2 of windowPosition as string) & \",\" & "
        "(item 1 of windowSize as string) & \",\" & "
        "(item 2 of windowSize as string)\n"
        "end tell"
    )
    try:
        raw = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout
        values = [float(part.strip()) for part in raw.split(",") if part.strip()]
    except Exception:
        return None
    return values if len(values) == 4 else None


def _read_clipboard_text() -> str:
    try:
        raw = subprocess.run(
            ["pbpaste"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).stdout
        return (raw or b"").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _clipboard_changed(before: str, after: str) -> bool:
    before_clean = (before or "").strip()
    after_clean = (after or "").strip()
    return bool(after_clean) and after_clean != before_clean


def _wait_for_clipboard_change(before: str, timeout_seconds: float = 1.5) -> str:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    latest = ""
    while True:
        latest = _read_clipboard_text()
        if _clipboard_changed(before, latest):
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(0.05)


def _is_copy_combo(combo: str) -> bool:
    value = str(combo or "").strip().casefold().replace("command", "cmd")
    return value in {"cmd+c", "cmd+copy", "ctrl+c"}


def _is_paste_combo(combo: str) -> bool:
    value = str(combo or "").strip().casefold().replace("command", "cmd")
    return value in {"cmd+v", "cmd+paste", "ctrl+v"}


# ──────────────────────────────────────────────────────────────────────────── #
# Keyboard state helper                                                        #
# ──────────────────────────────────────────────────────────────────────────── #

def _key_name(key) -> str:
    # Keep key normalization independent from pynput. The listener object is
    # the only place that may import pynput, and that branch is unreachable on
    # macOS. This prevents a direct callback/helper call from accidentally
    # loading pynput's TextInputSources translation code into a Mac process.
    char = getattr(key, "char", None)
    if char is not None:
        return str(char)
    raw = str(key)
    special = {
        "Key.cmd": "cmd", "Key.cmd_l": "cmd", "Key.cmd_r": "cmd",
        "Key.ctrl": "ctrl", "Key.ctrl_l": "ctrl", "Key.ctrl_r": "ctrl",
        "Key.alt": "alt", "Key.alt_l": "alt", "Key.alt_r": "alt",
        "Key.shift": "shift", "Key.shift_l": "shift", "Key.shift_r": "shift",
        "Key.tab": "tab", "Key.enter": "enter", "Key.space": "space",
        "Key.backspace": "backspace", "Key.delete": "delete", "Key.esc": "esc",
        "Key.up": "up", "Key.down": "down", "Key.left": "left", "Key.right": "right",
        "Key.f1": "f1", "Key.f2": "f2", "Key.f3": "f3", "Key.f4": "f4",
        "Key.f5": "f5", "Key.f6": "f6", "Key.f7": "f7", "Key.f8": "f8",
    }
    return special.get(raw, raw.replace("Key.", ""))


# ──────────────────────────────────────────────────────────────────────────── #
# Recorder                                                                     #
# ──────────────────────────────────────────────────────────────────────────── #

class Recorder:
    """Records user interactions until stop() is called.

    Usage:
        rec = Recorder()
        rec.start()
        # ... user performs the workflow ...
        recording = rec.stop()
    """

    def __init__(self, *, input_backend: Optional[str] = None):
        self._recording = Recording()
        self._running = False
        self._last_event_time = time.time()
        self._typed_buffer: list[str] = []
        self._held_modifiers: set[str] = set()
        self._mouse_listener = None
        self._keyboard_listener = None
        self._input_listener = None
        requested_backend = str(
            input_backend or os.environ.get(INPUT_CAPTURE_BACKEND_ENV, "auto")
        ).strip().casefold()
        if requested_backend not in {"auto", "quartz", "pynput"}:
            raise ValueError(
                f"{INPUT_CAPTURE_BACKEND_ENV} must be auto, quartz, or pynput"
            )
        current_platform = platform.system()
        if current_platform == "Darwin":
            # pynput translates key events through TextInputSources on its
            # listener thread.  On macOS 15 this can abort the whole Python
            # process inside TISCopyCurrentKeyboardInputSource, bypassing every
            # Python exception handler.  Quartz records raw keycodes and does
            # not enter that translation path.  Make the safe choice a hard
            # invariant, including for callers that bypass start.sh or carry a
            # stale GPA_RECORDING_INPUT_BACKEND=pynput environment variable.
            if requested_backend == "pynput":
                logger.warning(
                    "Ignoring unsafe pynput recording backend on macOS; using Quartz raw input capture."
                )
            requested_backend = "quartz"
        elif requested_backend == "auto":
            requested_backend = "pynput"
        self._input_capture_backend = requested_backend
        self._mouse_down: Optional[dict[str, Any]] = None
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────── #

    def start(self) -> None:
        if self._running:
            raise RuntimeError("Recording is already active.")
        self._recording = Recording()
        self._last_event_time = time.time()
        self._mouse_down = None
        self._typed_buffer = []
        self._held_modifiers.clear()

        self._running = True
        try:
            if self._input_capture_backend == "quartz":
                self._input_listener = _QuartzInputListener(self)
                self._input_listener.start()
            else:
                from pynput import keyboard, mouse

                self._mouse_listener = mouse.Listener(
                    on_click=self._on_click,
                    on_scroll=self._on_scroll,
                )
                self._keyboard_listener = keyboard.Listener(
                    on_press=self._on_key_press,
                    on_release=self._on_key_release,
                )
                self._mouse_listener.start()
                self._keyboard_listener.start()
        except BaseException:
            self._running = False
            self._stop_and_join_listeners()
            raise
        logger.info(
            "Recording started with %s input capture. Perform your workflow, then call stop().",
            self._input_capture_backend,
        )

    def stop(self) -> Recording:
        self._running = False
        self._stop_and_join_listeners()
        self._flush_typed_buffer()
        self._held_modifiers.clear()
        self._mouse_down = None
        logger.info(f"Recording stopped. {len(self._recording.events)} events captured.")
        return self._recording

    def _stop_and_join_listeners(self, timeout: float = 1.0) -> None:
        """Synchronously retire input hooks so no listener survives stop/failure."""
        listeners = [self._input_listener, self._mouse_listener, self._keyboard_listener]
        self._input_listener = None
        self._mouse_listener = None
        self._keyboard_listener = None
        for listener in listeners:
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:
                logger.debug("Could not stop input listener", exc_info=True)
        current = threading.current_thread()
        for listener in listeners:
            if listener is None or listener is current:
                continue
            join = getattr(listener, "join", None)
            if not callable(join):
                continue
            try:
                join(timeout=max(0.0, float(timeout)))
            except (RuntimeError, TypeError):
                # A listener that failed before its thread started cannot be joined.
                continue
            except Exception:
                logger.debug("Could not join input listener", exc_info=True)

    def _capture_metadata(self) -> dict[str, Any]:
        return {"input_source": f"{self._input_capture_backend}_listener"}

    def append_external_event(
        self,
        event_type: str,
        *,
        x: float = 0.0,
        y: float = 0.0,
        value: str = "",
        button: str = "left",
        scroll_dx: int = 0,
        scroll_dy: int = 0,
        start_x: float = 0.0,
        start_y: float = 0.0,
        end_x: float = 0.0,
        end_y: float = 0.0,
        duration_seconds: float = 0.0,
        clipboard_before: str = "",
        clipboard_after: str = "",
        active_app: str = "",
        coordinate_space: str = "screen",
        metadata: Optional[dict[str, Any]] = None,
    ) -> RecordedEvent:
        """Append an action performed by an accessibility automation client.

        macOS input monitoring does not report every synthetic accessibility
        action to ``pynput``.  Local test and assistive clients can therefore
        report the action explicitly while the normal recorder still captures
        the live screen and foreground application.  The event is tagged so a
        saved Replay remains auditable instead of looking like human input.
        """
        if not self._running:
            raise RuntimeError("Recording is not active.")
        event_type = str(event_type or "").strip().casefold()
        if event_type not in {"click", "drag", "scroll", "type", "hotkey"}:
            raise ValueError(f"Unsupported external recording event: {event_type or '<empty>'}")

        self._flush_typed_buffer()
        event_metadata = dict(metadata or {})
        event_metadata["input_source"] = "accessibility_automation"
        coordinate_space = str(coordinate_space or "screen").strip().casefold()
        if coordinate_space not in {"screen", "app_window"}:
            raise ValueError("coordinate_space must be 'screen' or 'app_window'")
        if coordinate_space == "app_window" and event_type in {"click", "drag", "scroll"}:
            window_bounds = get_active_window_bounds()
            if window_bounds is None:
                raise RuntimeError("Could not resolve the active window for app-relative coordinates.")
            offset_x, offset_y = window_bounds[:2]
            x = float(x) + offset_x
            y = float(y) + offset_y
            start_x = float(start_x) + offset_x
            start_y = float(start_y) + offset_y
            end_x = float(end_x) + offset_x
            end_y = float(end_y) + offset_y
            event_metadata["reported_coordinate_space"] = "app_window"
            event_metadata["window_offset"] = [offset_x, offset_y]
            event_metadata["window_bounds"] = window_bounds
        else:
            event_metadata["reported_coordinate_space"] = "screen"
        if clipboard_before:
            event_metadata.setdefault("clipboard_before", clipboard_before)
        if clipboard_after:
            event_metadata.setdefault("clipboard_after", clipboard_after)
            event_metadata.setdefault("clipboard_length", len(clipboard_after))

        event = RecordedEvent(
            event_type=event_type,
            x=float(x),
            y=float(y),
            button=str(button or "left"),
            value=str(value or ""),
            scroll_dx=int(scroll_dx),
            scroll_dy=int(scroll_dy),
            start_x=float(start_x),
            start_y=float(start_y),
            end_x=float(end_x),
            end_y=float(end_y),
            duration_seconds=max(0.0, float(duration_seconds)),
            clipboard_before=str(clipboard_before or ""),
            clipboard_after=str(clipboard_after or ""),
            metadata=event_metadata,
            timestamp=time.time(),
            screenshot=capture_screenshot(),
            active_app=str(active_app or get_active_app()),
            pause_before=self._pause_since_last(),
        )
        with self._lock:
            self._recording.events.append(event)
        self._last_event_time = time.time()
        return event

    # ──────────────────────────────────────────────────────────────────── #
    # Internal event handlers                                              #
    # ──────────────────────────────────────────────────────────────────── #

    def _pause_since_last(self) -> float:
        now = time.time()
        pause = now - self._last_event_time
        self._last_event_time = now
        return pause

    def _flush_typed_buffer(self) -> None:
        if not self._typed_buffer:
            return
        text = "".join(self._typed_buffer)
        self._typed_buffer = []
        if text:
            with self._lock:
                ev = RecordedEvent(
                    event_type="type",
                    value=text,
                    timestamp=time.time(),
                    screenshot=capture_screenshot(),
                    active_app=get_active_app(),
                    pause_before=self._pause_since_last(),
                    metadata=self._capture_metadata(),
                )
                self._recording.events.append(ev)

    def _on_click(self, x, y, button, pressed):
        if not self._running:
            return
        button_name = str(button).replace("Button.", "")
        if pressed:
            self._flush_typed_buffer()
            self._mouse_down = {
                "x": float(x),
                "y": float(y),
                "button": button_name,
                "timestamp": time.time(),
                "screenshot": capture_screenshot(),
                "active_app": get_active_app(),
                "pause_before": self._pause_since_last(),
            }
            return

        down = self._mouse_down
        self._mouse_down = None
        if not down:
            return

        end_x, end_y = float(x), float(y)
        start_x, start_y = float(down["x"]), float(down["y"])
        duration = max(0.0, time.time() - float(down["timestamp"]))
        distance = math.hypot(end_x - start_x, end_y - start_y)
        is_drag = distance >= 8.0 and duration >= 0.08

        if is_drag:
            ev = RecordedEvent(
                event_type="drag",
                x=end_x,
                y=end_y,
                button=button_name,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                duration_seconds=duration,
                timestamp=time.time(),
                screenshot=capture_screenshot(),
                active_app=get_active_app(),
                pause_before=float(down["pause_before"]),
                metadata={
                    "drag_start": [start_x, start_y],
                    "drag_end": [end_x, end_y],
                    "duration_seconds": duration,
                    "button": button_name,
                    **self._capture_metadata(),
                },
            )
        else:
            ev = RecordedEvent(
                event_type="click",
                x=start_x,
                y=start_y,
                button=str(down["button"]),
                timestamp=float(down["timestamp"]),
                screenshot=down["screenshot"],
                active_app=str(down["active_app"]),
                pause_before=float(down["pause_before"]),
                metadata=self._capture_metadata(),
            )
        with self._lock:
            self._recording.events.append(ev)
        self._last_event_time = time.time()
        logger.debug(f"{ev.event_type.title()} ({start_x}, {start_y}) -> ({end_x}, {end_y}) {ev.button}")

    def _on_scroll(self, x, y, dx, dy):
        if not self._running:
            return
        self._flush_typed_buffer()
        pause = self._pause_since_last()
        ev = RecordedEvent(
            event_type="scroll",
            x=float(x), y=float(y),
            scroll_dx=int(dx), scroll_dy=int(dy),
            timestamp=time.time(),
            screenshot=capture_screenshot(),
            active_app=get_active_app(),
            pause_before=pause,
            metadata=self._capture_metadata(),
        )
        with self._lock:
            self._recording.events.append(ev)

    def _on_key_press(self, key):
        if not self._running:
            return
        self._on_key_name_press(_key_name(key))

    def _on_key_name_press(self, name: str) -> None:
        if not self._running:
            return
        modifier_keys = {"cmd", "ctrl", "alt", "shift"}
        if name in modifier_keys:
            self._held_modifiers.add(name)
            return
        hotkey_modifiers = self._held_modifiers - {"shift"}
        if hotkey_modifiers:
            # Hotkey combination
            self._flush_typed_buffer()
            combo = "+".join(sorted(self._held_modifiers)) + "+" + name
            is_copy = _is_copy_combo(combo)
            is_paste = _is_paste_combo(combo)
            clipboard_before = _read_clipboard_text() if is_copy else ""
            pasted_text = _read_clipboard_text() if is_paste else ""
            pause = self._pause_since_last()
            ev = RecordedEvent(
                event_type="hotkey",
                value=combo,
                timestamp=time.time(),
                screenshot=capture_screenshot(),
                active_app=get_active_app(),
                pause_before=pause,
                clipboard_before=clipboard_before,
                metadata=self._capture_metadata(),
            )
            if is_copy:
                ev.clipboard_after = _wait_for_clipboard_change(clipboard_before)
                ev.metadata["clipboard_before"] = clipboard_before
                ev.metadata["clipboard_after"] = ev.clipboard_after
                ev.metadata["clipboard_changed"] = _clipboard_changed(clipboard_before, ev.clipboard_after)
                ev.metadata["clipboard_length"] = len(ev.clipboard_after or "")
                ev.metadata["clipboard_operation"] = "copy"
            elif is_paste:
                # The clipboard already contains the text at key-down time.  Capturing
                # it here makes paste-driven input reproducible and gives the builder
                # concrete evidence for a TYPE step instead of asking the model to
                # infer content from the task description.
                ev.clipboard_after = pasted_text
                ev.metadata["clipboard_after"] = pasted_text
                ev.metadata["clipboard_length"] = len(pasted_text)
                ev.metadata["clipboard_operation"] = "paste"
            with self._lock:
                self._recording.events.append(ev)
            logger.debug(f"Hotkey: {combo}")
        else:
            # Regular key → accumulate into typed buffer
            if name and len(name) == 1:
                self._typed_buffer.append(name)
            elif name in ("backspace", "delete"):
                if self._typed_buffer:
                    self._typed_buffer.pop()
            elif name in ("tab", "enter"):
                self._flush_typed_buffer()
                pause = self._pause_since_last()
                ev = RecordedEvent(
                    event_type="hotkey",
                    value=name,
                    timestamp=time.time(),
                    screenshot=capture_screenshot(),
                    active_app=get_active_app(),
                    pause_before=pause,
                    metadata=self._capture_metadata(),
                )
                with self._lock:
                    self._recording.events.append(ev)

    def _on_key_release(self, key):
        if not self._running:
            return
        self._on_key_name_release(_key_name(key))

    def _on_key_name_release(self, name: str) -> None:
        if not self._running:
            return
        self._held_modifiers.discard(name)
