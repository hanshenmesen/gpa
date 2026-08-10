"""Screen + input recorder.

Records:
  - Screenshots at each action (via mss)
  - Mouse clicks (position, button)
  - Keyboard input (text typed, hotkeys)
  - Scroll events
  - Timing and active app metadata

Uses pynput for event capture and mss for fast screenshot capture.
"""
from __future__ import annotations

import logging
import math
import platform
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image

logger = logging.getLogger(__name__)


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


# ──────────────────────────────────────────────────────────────────────────── #
# Keyboard state helper                                                        #
# ──────────────────────────────────────────────────────────────────────────── #

def _key_name(key) -> str:
    from pynput.keyboard import Key
    try:
        return key.char
    except AttributeError:
        special = {
            Key.cmd: "cmd", Key.ctrl: "ctrl", Key.alt: "alt",
            Key.shift: "shift", Key.tab: "tab", Key.enter: "enter",
            Key.space: "space", Key.backspace: "backspace",
            Key.delete: "delete", Key.esc: "esc",
            Key.up: "up", Key.down: "down", Key.left: "left", Key.right: "right",
            Key.f1: "f1", Key.f2: "f2", Key.f3: "f3", Key.f4: "f4",
            Key.f5: "f5", Key.f6: "f6", Key.f7: "f7", Key.f8: "f8",
        }
        return special.get(key, str(key).replace("Key.", ""))


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

    def __init__(self):
        self._recording = Recording()
        self._running = False
        self._last_event_time = time.time()
        self._typed_buffer: list[str] = []
        self._held_modifiers: set[str] = set()
        self._mouse_listener = None
        self._keyboard_listener = None
        self._mouse_down: Optional[dict[str, Any]] = None
        self._lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────── #

    def start(self) -> None:
        from pynput import mouse, keyboard

        self._running = True
        self._recording = Recording()
        self._last_event_time = time.time()
        self._mouse_down = None
        logger.info("Recording started. Perform your workflow, then call stop().")

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

    def stop(self) -> Recording:
        self._running = False
        self._flush_typed_buffer()
        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        logger.info(f"Recording stopped. {len(self._recording.events)} events captured.")
        return self._recording

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
        )
        with self._lock:
            self._recording.events.append(ev)

    def _on_key_press(self, key):
        if not self._running:
            return
        name = _key_name(key)
        modifier_keys = {"cmd", "ctrl", "alt", "shift"}
        if name in modifier_keys:
            self._held_modifiers.add(name)
            return
        if self._held_modifiers:
            # Hotkey combination
            self._flush_typed_buffer()
            combo = "+".join(sorted(self._held_modifiers)) + "+" + name
            clipboard_before = _read_clipboard_text() if _is_copy_combo(combo) else ""
            pause = self._pause_since_last()
            ev = RecordedEvent(
                event_type="hotkey",
                value=combo,
                timestamp=time.time(),
                screenshot=capture_screenshot(),
                active_app=get_active_app(),
                pause_before=pause,
                clipboard_before=clipboard_before,
            )
            if _is_copy_combo(combo):
                ev.clipboard_after = _wait_for_clipboard_change(clipboard_before)
                ev.metadata["clipboard_before"] = clipboard_before
                ev.metadata["clipboard_after"] = ev.clipboard_after
                ev.metadata["clipboard_changed"] = _clipboard_changed(clipboard_before, ev.clipboard_after)
                ev.metadata["clipboard_length"] = len(ev.clipboard_after or "")
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
                )
                with self._lock:
                    self._recording.events.append(ev)

    def _on_key_release(self, key):
        if not self._running:
            return
        name = _key_name(key)
        self._held_modifiers.discard(name)
