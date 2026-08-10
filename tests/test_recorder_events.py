import unittest

import gpa.recording.recorder as recorder_module
from gpa.recording.recorder import Recorder


class FakeScreenshot:
    width = 1000
    height = 800


class FakeKey:
    def __init__(self, char):
        self.char = char


class RecorderEventTests(unittest.TestCase):
    def test_mouse_drag_records_drag_event(self):
        rec = Recorder()
        rec._running = True

        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
        ):
            rec._on_click(10, 20, "Button.left", True)
            rec._mouse_down["timestamp"] -= 0.2
            rec._on_click(120, 80, "Button.left", False)

        self.assertEqual(len(rec._recording.events), 1)
        event = rec._recording.events[0]
        self.assertEqual(event.event_type, "drag")
        self.assertEqual(event.start_x, 10.0)
        self.assertEqual(event.start_y, 20.0)
        self.assertEqual(event.end_x, 120.0)
        self.assertEqual(event.end_y, 80.0)
        self.assertEqual(event.metadata["drag_start"], [10.0, 20.0])
        self.assertEqual(event.metadata["drag_end"], [120.0, 80.0])

    def test_copy_hotkey_records_clipboard_after(self):
        rec = Recorder()
        rec._running = True
        rec._held_modifiers.add("cmd")
        reads = iter(["old", "selected text"])

        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
            _read_clipboard_text=lambda: next(reads),
        ):
            rec._on_key_press(FakeKey("c"))

        self.assertEqual(len(rec._recording.events), 1)
        event = rec._recording.events[0]
        self.assertEqual(event.event_type, "hotkey")
        self.assertEqual(event.value, "cmd+c")
        self.assertEqual(event.clipboard_before, "old")
        self.assertEqual(event.clipboard_after, "selected text")
        self.assertEqual(event.metadata["clipboard_after"], "selected text")


class patched_recorder:
    def __init__(self, **replacements):
        self.replacements = replacements
        self.originals = {}

    def __enter__(self):
        for name, value in self.replacements.items():
            self.originals[name] = getattr(recorder_module, name)
            setattr(recorder_module, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(recorder_module, name, value)


if __name__ == "__main__":
    unittest.main()
