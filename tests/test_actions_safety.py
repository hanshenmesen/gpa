import os
import threading
import unittest

import gpa.execution.actions as actions


class FakePyAutoGUI:
    FAILSAFE = True
    PAUSE = 0.0

    def __init__(self):
        self.calls = []
        self.fail_on_key_down = set()

    def keyDown(self, key):
        self.calls.append(("keyDown", key))
        if key in self.fail_on_key_down:
            raise RuntimeError(f"boom {key}")

    def keyUp(self, key):
        self.calls.append(("keyUp", key))

    def mouseDown(self, button="left"):
        self.calls.append(("mouseDown", button))

    def mouseUp(self, button="left"):
        self.calls.append(("mouseUp", button))

    def moveTo(self, *args, **kwargs):
        self.calls.append(("moveTo", args, kwargs))

    def scroll(self, value):
        self.calls.append(("scroll", value))

    def hscroll(self, value):
        self.calls.append(("hscroll", value))

    def typewrite(self, text, interval=0):
        self.calls.append(("typewrite", text, interval))


class ActionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.get(actions.DESKTOP_AUTOMATION_ENV)
        self.old_quarantine_env = os.environ.get(actions.KEYBOARD_QUARANTINE_SECONDS_ENV)
        os.environ[actions.DESKTOP_AUTOMATION_ENV] = "1"
        os.environ[actions.KEYBOARD_QUARANTINE_SECONDS_ENV] = "0"
        self.fake = FakePyAutoGUI()
        self.old_pyautogui = actions.pyautogui
        actions.pyautogui = self.fake
        actions.set_abort_checker(None)
        actions.arm_actions()

    def tearDown(self):
        actions.panic_stop()
        actions.set_abort_checker(None)
        actions.pyautogui = self.old_pyautogui
        if self.old_env is None:
            os.environ.pop(actions.DESKTOP_AUTOMATION_ENV, None)
        else:
            os.environ[actions.DESKTOP_AUTOMATION_ENV] = self.old_env
        if self.old_quarantine_env is None:
            os.environ.pop(actions.KEYBOARD_QUARANTINE_SECONDS_ENV, None)
        else:
            os.environ[actions.KEYBOARD_QUARANTINE_SECONDS_ENV] = self.old_quarantine_env

    def test_hotkey_releases_pressed_keys_when_later_key_fails(self):
        self.fake.fail_on_key_down.add("c")

        with self.assertRaises(RuntimeError):
            actions.press_hotkey("cmd+shift+c")

        self.assertIn(("keyDown", "command"), self.fake.calls)
        self.assertIn(("keyUp", "command"), self.fake.calls)

    def test_type_text_on_macos_uses_clipboard_paste_not_key_events(self):
        calls = []
        old_platform = actions.sys.platform
        old_paste = actions._paste_text_via_clipboard
        actions.sys.platform = "darwin"
        actions._paste_text_via_clipboard = lambda text: calls.append(text)
        try:
            actions.type_text("hello")
        finally:
            actions._paste_text_via_clipboard = old_paste
            actions.sys.platform = old_platform

        self.assertEqual(calls, ["hello"])
        self.assertNotIn(("typewrite", "h", 0), self.fake.calls)

    def test_cmd_v_on_macos_uses_menu_paste_not_key_events(self):
        calls = []
        old_platform = actions.sys.platform
        old_menu = actions._menu_command
        actions.sys.platform = "darwin"
        actions._menu_command = lambda command: calls.append(command)
        try:
            actions.press_hotkey("cmd+v")
        finally:
            actions._menu_command = old_menu
            actions.sys.platform = old_platform

        self.assertEqual(calls, ["paste"])
        self.assertNotIn(("keyDown", "command"), self.fake.calls)

    def test_panic_stop_releases_tracked_key_and_mouse_button(self):
        actions._key_down("command")
        actions._mouse_down("left")

        actions.panic_stop()

        self.assertIn(("keyUp", "command"), self.fake.calls)
        self.assertIn(("mouseUp", "left"), self.fake.calls)

    def test_key_is_tracked_before_os_dispatch_returns(self):
        entered = threading.Event()
        allow_return = threading.Event()
        errors = []

        def blocking_key_down(key):
            entered.set()
            allow_return.wait(timeout=1.0)
            self.fake.calls.append(("keyDown", key))

        self.fake.keyDown = blocking_key_down

        def dispatch():
            try:
                actions._key_down("command")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=dispatch)
        worker.start()
        self.assertTrue(entered.wait(timeout=1.0))
        try:
            self.assertIn("command", actions._held_keys)
            actions.panic_stop()
        finally:
            allow_return.set()
            worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(any(isinstance(exc, actions.ActionAborted) for exc in errors))
        self.assertIn(("keyUp", "command"), self.fake.calls)
        self.assertEqual(self.fake.calls[-1], ("keyUp", "command"))
        self.assertNotIn("command", actions._held_keys)

    def test_mouse_button_is_tracked_before_os_dispatch_returns(self):
        entered = threading.Event()
        allow_return = threading.Event()
        errors = []

        def blocking_mouse_down(button="left"):
            entered.set()
            allow_return.wait(timeout=1.0)
            self.fake.calls.append(("mouseDown", button))

        self.fake.mouseDown = blocking_mouse_down

        def dispatch():
            try:
                actions._mouse_down("left")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=dispatch)
        worker.start()
        self.assertTrue(entered.wait(timeout=1.0))
        try:
            self.assertIn("left", actions._held_mouse_buttons)
            actions.panic_stop()
        finally:
            allow_return.set()
            worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(any(isinstance(exc, actions.ActionAborted) for exc in errors))
        self.assertIn(("mouseUp", "left"), self.fake.calls)
        self.assertEqual(self.fake.calls[-1], ("mouseUp", "left"))
        self.assertNotIn("left", actions._held_mouse_buttons)

    def test_action_process_spawn_and_registration_are_atomic_with_stop(self):
        entered = threading.Event()
        allow_return = threading.Event()
        terminated = []

        class FakeProcess:
            pid = 7654

            def poll(self):
                return None

        process = FakeProcess()
        old_popen = actions.subprocess.Popen
        old_terminate = actions.terminate_process

        def blocking_popen(command, **kwargs):
            entered.set()
            allow_return.wait(timeout=1.0)
            return process

        actions.subprocess.Popen = blocking_popen
        actions.terminate_process = lambda proc: terminated.append(proc)
        spawned = []
        spawn_errors = []

        def spawn():
            try:
                spawned.append(actions.start_action_process(["osascript", "-e", "return 1"]))
            except Exception as exc:
                spawn_errors.append(exc)

        spawn_thread = threading.Thread(target=spawn)
        stop_thread = threading.Thread(target=actions.abort_actions)
        try:
            spawn_thread.start()
            self.assertTrue(entered.wait(timeout=1.0))
            stop_thread.start()
            self.assertTrue(stop_thread.is_alive())
            allow_return.set()
            spawn_thread.join(timeout=1.0)
            stop_thread.join(timeout=1.0)
        finally:
            allow_return.set()
            actions.subprocess.Popen = old_popen
            actions.terminate_process = old_terminate
            actions._action_processes.discard(process)

        self.assertFalse(spawn_errors)
        self.assertEqual(spawned, [process])
        self.assertEqual(terminated, [process])

    def test_action_subprocess_is_terminated_when_replay_stops(self):
        class FakeProcess:
            pid = 4321
            returncode = None

            def poll(self):
                return None

        fake_process = FakeProcess()
        popen_calls = []
        terminated = []
        checker_calls = {"count": 0}
        old_popen = actions.subprocess.Popen
        old_terminate = actions.terminate_process

        def stop_after_launch():
            checker_calls["count"] += 1
            return checker_calls["count"] >= 2

        actions.subprocess.Popen = lambda command, **kwargs: popen_calls.append((command, kwargs)) or fake_process
        actions.terminate_process = lambda proc: terminated.append(proc)
        actions.set_abort_checker(stop_after_launch)
        try:
            with self.assertRaises(actions.ActionAborted):
                actions._run_action_subprocess(["osascript", "-e", "return 1"], timeout=3.0)
        finally:
            actions.set_abort_checker(None)
            actions.subprocess.Popen = old_popen
            actions.terminate_process = old_terminate

        self.assertEqual(terminated, [fake_process])
        self.assertEqual(popen_calls[0][0][0], "osascript")
        self.assertTrue(popen_calls[0][1]["start_new_session"])

    def test_watchdog_sentinel_tracks_action_process_groups(self):
        class FakeProcess:
            pid = 2468

            def poll(self):
                return None

        fd, sentinel = actions.tempfile.mkstemp(prefix="gpa-action-test-", suffix=".watch")
        os.close(fd)
        old_sentinel = actions._watchdog_sentinel
        process = FakeProcess()
        actions._watchdog_sentinel = sentinel
        try:
            actions.track_action_process(process)
            with open(sentinel, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "2468")
            actions.untrack_action_process(process)
            with open(sentinel, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "")
        finally:
            actions._action_processes.discard(process)
            actions._watchdog_sentinel = old_sentinel
            try:
                os.remove(sentinel)
            except OSError:
                pass

    def test_panic_stop_does_not_emit_untracked_enter_keyup(self):
        actions.panic_stop()

        self.assertNotIn(("keyUp", "enter"), self.fake.calls)

    def test_hotkey_does_not_emit_untracked_release_events(self):
        actions.press_hotkey("enter")

        self.assertIn(("keyDown", "enter"), self.fake.calls)
        self.assertIn(("keyUp", "enter"), self.fake.calls)
        self.assertNotIn(("keyUp", "command"), self.fake.calls)
        self.assertNotIn(("mouseUp", "left"), self.fake.calls)

    def test_panic_stop_starts_keyboard_quarantine(self):
        calls = []
        old_quarantine = actions._start_keyboard_quarantine
        actions._start_keyboard_quarantine = lambda: calls.append("quarantine")
        try:
            actions._key_down("command")
            actions.panic_stop()
        finally:
            actions._start_keyboard_quarantine = old_quarantine

        self.assertEqual(calls, ["quarantine"])

    def test_idle_panic_stop_does_not_emit_release_events(self):
        actions.abort_actions()
        self.fake.calls.clear()

        actions.panic_stop()

        self.assertEqual(self.fake.calls, [])

    def test_stale_action_token_blocks_old_worker_input(self):
        old_token = actions.arm_actions()
        new_token = actions.arm_actions()

        actions.bind_action_token(old_token)
        with self.assertRaises(actions.ActionAborted):
            actions.click(10, 20)

        self.assertEqual(self.fake.calls, [])

        actions.bind_action_token(new_token)
        actions.click(10, 20)

        self.assertIn(("mouseDown", "left"), self.fake.calls)

    def test_panic_invalidates_active_action_token(self):
        token = actions.arm_actions()
        actions.bind_action_token(token)

        actions.panic_stop()

        with self.assertRaises(actions.ActionAborted):
            actions.click(10, 20)

    def test_panic_stop_with_stale_token_still_releases_inputs_and_quarantines(self):
        # Reproduces the "burst of operations after manual interrupt" bug: the web
        # Stop handler calls abort_actions() (no token) the instant Stop is pressed,
        # advancing the action generation. The replay worker later reaches its
        # finally block and calls panic_stop(token) with a now-stale token. That
        # cleanup must still run — otherwise held modifiers stay stuck and no
        # keyboard quarantine is installed, so OS-queued input flushes as a burst.
        calls = []
        old_quarantine = actions._start_keyboard_quarantine
        actions._start_keyboard_quarantine = lambda: calls.append("quarantine")
        try:
            token = actions.arm_actions()
            actions.bind_action_token(token)
            actions._key_down("command")
            actions._mouse_down("left")

            # Simulate the Stop handler advancing the generation first.
            self.assertTrue(actions.abort_actions())

            # Worker finally block now runs with a stale token.
            self.fake.calls.clear()
            actions.panic_stop(token)
        finally:
            actions._start_keyboard_quarantine = old_quarantine

        self.assertEqual(calls, ["quarantine"])
        self.assertIn(("keyUp", "command"), self.fake.calls)
        self.assertIn(("mouseUp", "left"), self.fake.calls)
        self.assertFalse(actions._held_keys)
        self.assertFalse(actions._held_mouse_buttons)

    def test_abort_actions_invalidates_without_emitting_input(self):
        token = actions.arm_actions()
        actions.bind_action_token(token)

        self.assertTrue(actions.abort_actions(token))

        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(actions.ActionAborted):
            actions.click(10, 20)

    def test_finish_actions_quarantines_after_successful_input_without_release_events(self):
        calls = []
        old_quarantine = actions._start_keyboard_quarantine
        actions._start_keyboard_quarantine = lambda: calls.append("quarantine")
        try:
            token = actions.arm_actions()
            actions.bind_action_token(token)
            actions.press_hotkey("enter")
            self.fake.calls.clear()

            self.assertTrue(actions.finish_actions(token))
        finally:
            actions._start_keyboard_quarantine = old_quarantine

        self.assertEqual(calls, ["quarantine"])
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(actions.ActionAborted):
            actions.click(10, 20)

    def test_process_exit_panic_quarantines_after_prior_successful_input(self):
        calls = []
        old_quarantine = actions._start_keyboard_quarantine
        actions._start_keyboard_quarantine = lambda: calls.append("quarantine")
        try:
            token = actions.arm_actions()
            actions.bind_action_token(token)
            actions.press_hotkey("enter")
            actions.finish_actions(token)
            self.fake.calls.clear()

            actions.panic_stop()
        finally:
            actions._start_keyboard_quarantine = old_quarantine

        self.assertEqual(calls, ["quarantine", "quarantine"])
        self.assertEqual(self.fake.calls, [])

    def test_finish_actions_keeps_watchdog_armed_until_process_exit_cleanup(self):
        calls = []
        old_stop_watchdog = actions._stop_input_watchdog
        old_quarantine = actions._start_keyboard_quarantine
        actions._stop_input_watchdog = lambda: calls.append("stop_watchdog")
        actions._start_keyboard_quarantine = lambda: calls.append("quarantine")
        try:
            token = actions.arm_actions()
            actions.bind_action_token(token)
            actions.press_hotkey("enter")

            self.assertTrue(actions.finish_actions(token))
            self.assertEqual(calls, ["quarantine"])

            actions.panic_stop()
        finally:
            actions._stop_input_watchdog = old_stop_watchdog
            actions._start_keyboard_quarantine = old_quarantine

        self.assertEqual(calls, ["quarantine", "stop_watchdog", "quarantine"])

    def test_interruptible_sleep_does_not_pass_negative_duration_to_sleep(self):
        class JumpingTime:
            def __init__(self):
                self.values = [0.0, 0.2]
                self.sleeps = []

            def monotonic(self):
                if self.values:
                    return self.values.pop(0)
                return 0.2

            def sleep(self, duration):
                self.sleeps.append(duration)
                if duration < 0:
                    raise AssertionError("negative sleep")

        fake_time = JumpingTime()
        old_time = actions.time
        actions.time = fake_time
        try:
            actions._sleep_interruptible(0.1)
        finally:
            actions.time = old_time

        self.assertEqual(fake_time.sleeps, [])


if __name__ == "__main__":
    unittest.main()
