import sys
import types
import unittest

import gpa.recording.recorder as recorder_module
from gpa.recording.recorder import Recorder, _mac_key_name


class FakeScreenshot:
    width = 1000
    height = 800


class FakeKey:
    def __init__(self, char):
        self.char = char


class RecorderEventTests(unittest.TestCase):
    def test_key_normalization_never_imports_pynput(self):
        old_pynput = sys.modules.pop("pynput", None)
        old_keyboard = sys.modules.pop("pynput.keyboard", None)
        try:
            self.assertEqual(recorder_module._key_name(FakeKey("x")), "x")
            self.assertEqual(recorder_module._key_name("Key.enter"), "enter")
            self.assertNotIn("pynput", sys.modules)
            self.assertNotIn("pynput.keyboard", sys.modules)
        finally:
            if old_pynput is not None:
                sys.modules["pynput"] = old_pynput
            if old_keyboard is not None:
                sys.modules["pynput.keyboard"] = old_keyboard

    def test_stop_synchronously_stops_and_joins_both_input_listeners(self):
        events = []

        class FakeListener:
            def __init__(self, kind, **kwargs):
                self.kind = kind

            def start(self):
                events.append((self.kind, "start"))

            def stop(self):
                events.append((self.kind, "stop"))

            def join(self, timeout=None):
                events.append((self.kind, "join", timeout))

        old_pynput = sys.modules.get("pynput")
        fake_keyboard = types.SimpleNamespace(Listener=lambda **kwargs: FakeListener("keyboard", **kwargs))
        fake_mouse = types.SimpleNamespace(Listener=lambda **kwargs: FakeListener("mouse", **kwargs))
        sys.modules["pynput"] = types.SimpleNamespace(keyboard=fake_keyboard, mouse=fake_mouse)
        try:
            with patched_recorder(platform=types.SimpleNamespace(system=lambda: "Linux")):
                rec = Recorder(input_backend="pynput")
                rec.start()
                rec.stop()
        finally:
            if old_pynput is None:
                sys.modules.pop("pynput", None)
            else:
                sys.modules["pynput"] = old_pynput

        self.assertEqual(
            events,
            [
                ("mouse", "start"),
                ("keyboard", "start"),
                ("mouse", "stop"),
                ("keyboard", "stop"),
                ("mouse", "join", 1.0),
                ("keyboard", "join", 1.0),
            ],
        )
        self.assertIsNone(rec._mouse_listener)
        self.assertIsNone(rec._keyboard_listener)

    def test_partial_listener_start_failure_is_fully_cleaned_up(self):
        events = []

        class FakeListener:
            def __init__(self, kind, fail=False, **kwargs):
                self.kind = kind
                self.fail = fail

            def start(self):
                events.append((self.kind, "start"))
                if self.fail:
                    raise RuntimeError("listener startup failed")

            def stop(self):
                events.append((self.kind, "stop"))

            def join(self, timeout=None):
                events.append((self.kind, "join"))

        old_pynput = sys.modules.get("pynput")
        fake_keyboard = types.SimpleNamespace(
            Listener=lambda **kwargs: FakeListener("keyboard", fail=True, **kwargs)
        )
        fake_mouse = types.SimpleNamespace(Listener=lambda **kwargs: FakeListener("mouse", **kwargs))
        sys.modules["pynput"] = types.SimpleNamespace(keyboard=fake_keyboard, mouse=fake_mouse)
        try:
            with patched_recorder(platform=types.SimpleNamespace(system=lambda: "Linux")):
                rec = Recorder(input_backend="pynput")
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    rec.start()
        finally:
            if old_pynput is None:
                sys.modules.pop("pynput", None)
            else:
                sys.modules["pynput"] = old_pynput

        self.assertFalse(rec._running)
        self.assertIn(("mouse", "stop"), events)
        self.assertIn(("keyboard", "stop"), events)
        self.assertIn(("mouse", "join"), events)
        self.assertIn(("keyboard", "join"), events)
        self.assertIsNone(rec._mouse_listener)
        self.assertIsNone(rec._keyboard_listener)

    def test_macos_default_uses_quartz_listener_without_importing_pynput(self):
        events = []

        class FakeQuartzListener:
            def __init__(self, recorder):
                self.recorder = recorder

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

            def join(self, timeout=None):
                events.append(("join", timeout))

        with patched_recorder(
            _QuartzInputListener=FakeQuartzListener,
            platform=types.SimpleNamespace(system=lambda: "Darwin"),
        ):
            rec = Recorder()
            rec.start()
            rec.stop()

        self.assertEqual(rec._input_capture_backend, "quartz")
        self.assertEqual(events, ["start", "stop", ("join", 1.0)])
        self.assertIsNone(rec._input_listener)

    def test_macos_explicit_pynput_request_is_forced_to_quartz(self):
        events = []

        class FakeQuartzListener:
            def __init__(self, recorder):
                self.recorder = recorder

            def start(self):
                events.append("start")

            def stop(self):
                events.append("stop")

            def join(self, timeout=None):
                events.append(("join", timeout))

        with patched_recorder(
            _QuartzInputListener=FakeQuartzListener,
            platform=types.SimpleNamespace(system=lambda: "Darwin"),
        ):
            rec = Recorder(input_backend="pynput")
            rec.start()
            rec.stop()

        self.assertEqual(rec._input_capture_backend, "quartz")
        self.assertEqual(events, ["start", "stop", ("join", 1.0)])

    def test_macos_keycode_mapping_never_uses_text_input_sources(self):
        self.assertEqual(_mac_key_name(0), "a")
        self.assertEqual(_mac_key_name(0, shifted=True), "A")
        self.assertEqual(_mac_key_name(18, shifted=True), "!")
        self.assertEqual(_mac_key_name(55), "cmd")
        self.assertEqual(_mac_key_name(999), "")

    def test_external_accessibility_event_is_captured_and_auditable(self):
        rec = Recorder()
        rec._running = True

        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
        ):
            event = rec.append_external_event(
                "hotkey",
                value="cmd+f",
                metadata={"target_hint": "browser find"},
            )

        self.assertEqual(len(rec._recording.events), 1)
        self.assertIs(event, rec._recording.events[0])
        self.assertEqual(event.event_type, "hotkey")
        self.assertEqual(event.value, "cmd+f")
        self.assertEqual(event.active_app, "Google Chrome")
        self.assertEqual(event.metadata["input_source"], "accessibility_automation")
        self.assertEqual(event.metadata["target_hint"], "browser find")

    def test_external_event_requires_active_recording(self):
        with self.assertRaisesRegex(RuntimeError, "not active"):
            Recorder().append_external_event("click", x=10, y=20)

    def test_external_event_rejects_unknown_type(self):
        rec = Recorder()
        rec._running = True
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            rec.append_external_event("launch_process")

    def test_external_app_window_coordinates_are_converted_to_screen(self):
        rec = Recorder()
        rec._running = True
        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
            get_active_window_bounds=lambda: [200.0, 100.0, 1000.0, 760.0],
        ):
            event = rec.append_external_event(
                "click",
                x=76,
                y=483,
                coordinate_space="app_window",
            )

        self.assertEqual((event.x, event.y), (276.0, 583.0))
        self.assertEqual(event.metadata["reported_coordinate_space"], "app_window")
        self.assertEqual(event.metadata["window_offset"], [200.0, 100.0])

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
        self.assertEqual(event.metadata["clipboard_operation"], "copy")

    def test_paste_hotkey_records_existing_clipboard_payload(self):
        rec = Recorder()
        rec._running = True
        rec._held_modifiers.add("cmd")

        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
            _read_clipboard_text=lambda: "ACME Shanghai",
        ):
            rec._on_key_press(FakeKey("v"))

        self.assertEqual(len(rec._recording.events), 1)
        event = rec._recording.events[0]
        self.assertEqual(event.event_type, "hotkey")
        self.assertEqual(event.value, "cmd+v")
        self.assertEqual(event.clipboard_after, "ACME Shanghai")
        self.assertEqual(event.metadata["clipboard_operation"], "paste")

    def test_raw_recording_event_identifies_capture_backend(self):
        rec = Recorder(input_backend="quartz")
        rec._running = True
        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
        ):
            rec._on_key_name_press("g")
            rec._flush_typed_buffer()

        self.assertEqual(
            rec._recording.events[0].metadata["input_source"],
            "quartz_listener",
        )

    def test_shifted_character_is_text_not_a_global_hotkey(self):
        rec = Recorder(input_backend="quartz")
        rec._running = True
        rec._on_key_name_press("shift")
        rec._on_key_name_press("A")
        rec._on_key_name_release("shift")
        with patched_recorder(
            capture_screenshot=lambda: FakeScreenshot(),
            get_active_app=lambda: "Google Chrome",
        ):
            rec._flush_typed_buffer()

        self.assertEqual(rec._recording.events[0].event_type, "type")
        self.assertEqual(rec._recording.events[0].value, "A")


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
