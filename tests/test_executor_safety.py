import os
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import gpa.execution.executor as executor_module
from gpa.core.smc import LocalizationResult
from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode
from gpa.execution.actions import (
    DESKTOP_AUTOMATION_ENV,
    KEYBOARD_QUARANTINE_SECONDS_ENV,
    ActionAborted,
    actions_stopped,
    arm_actions,
    click,
    panic_stop,
    set_abort_checker,
)
from gpa.execution.executor import Executor
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowVariable


class FakeScreenshot:
    width = 1000
    height = 800

    def save(self, buffer, format=None):
        buffer.write(b"fake-image-bytes")


class ExecutorSafetyTests(unittest.TestCase):
    def test_http_evidence_rejects_private_network_targets(self):
        with patch.object(
            executor_module.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            error = executor_module._public_http_url_error("https://example.test/page")

        self.assertIn("non-public", error)

    def test_http_evidence_accepts_public_targets(self):
        with patch.object(
            executor_module.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        ):
            error = executor_module._public_http_url_error("https://example.test/page")

        self.assertEqual(error, "")

    def test_http_evidence_parser_omits_script_content(self):
        parser = executor_module._VisibleTextParser()
        parser.feed("<main>Visible <script>ignore me</script><strong>answer</strong></main>")

        self.assertEqual(parser.parts, ["Visible", "answer"])

    def test_nearby_primary_button_center_finds_shifted_retry_button(self):
        image = Image.new("RGB", (1000, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((810, 700, 940, 748), radius=8, fill=(40, 95, 201))
        previous = LocalizationResult(
            x=875,
            y=680,
            confidence=1.0,
            likelihood_conf=1.0,
            spatial_conf=1.0,
            method="previous",
        )

        center = executor_module._nearby_primary_button_center(image, previous)

        self.assertIsNotNone(center)
        self.assertAlmostEqual(center[0], 875.0, delta=2.0)
        self.assertAlmostEqual(center[1], 724.0, delta=2.0)

    def test_text_anchor_uses_target_text_not_neighbor_label(self):
        target = UINode(
            id=1,
            pos=[10, 10, 80, 30],
            elem_type="text",
            content="Send message",
            text_emb=np.array([1.0, 0.0]),
        )
        neighbor = UINode(
            id=2,
            pos=[10, 60, 80, 30],
            elem_type="text",
            content="Account",
            text_emb=np.array([0.0, 1.0]),
        )
        subgraph = StepSubgraph(
            target_element_id=1,
            click_coordinates=[50, 25],
            ui_graph=UIGraph(nodes=[target, neighbor], edges=[(1, 2)], image_size=[400, 300]),
            window_bounds=[0, 0, 400, 300],
        )
        renamed = UINode(
            id=10,
            pos=[250, 180, 100, 40],
            elem_type="text",
            content="Submit reply",
            text_emb=np.array([1.0, 0.0]),
        )
        account = UINode(
            id=11,
            pos=[30, 30, 100, 40],
            elem_type="text",
            content="Account",
            text_emb=np.array([0.0, 1.0]),
        )
        step = WorkflowStep(1, "Send the message", action_type="click")

        result = executor_module._text_anchor_localization(
            step,
            subgraph,
            UIGraph(nodes=[account, renamed], image_size=[400, 300]),
        )

        self.assertIsNotNone(result)
        self.assertEqual((result.x, result.y), tuple(renamed.center))

    def test_text_anchor_rejects_ambiguous_duplicate_controls(self):
        target = UINode(id=1, pos=[0, 0, 50, 20], elem_type="text", content="Save")
        subgraph = StepSubgraph(
            target_element_id=1,
            click_coordinates=[25, 10],
            ui_graph=UIGraph(nodes=[target], image_size=[400, 300]),
            window_bounds=[0, 0, 400, 300],
        )
        runtime = UIGraph(nodes=[
            UINode(id=10, pos=[10, 10, 50, 20], elem_type="text", content="Save"),
            UINode(id=11, pos=[300, 200, 50, 20], elem_type="text", content="Save"),
        ], image_size=[400, 300])

        result = executor_module._text_anchor_localization(
            WorkflowStep(1, "Save", action_type="click"),
            subgraph,
            runtime,
        )

        self.assertIsNone(result)

    def test_executor_rejects_unsafe_retry_and_readiness_configuration(self):
        workflow = Workflow("config", "config", "config", "config", steps=[])
        for kwargs in (
            {"readiness_threshold": -0.1},
            {"readiness_threshold": float("nan")},
            {"max_retries": -1},
            {"max_retries": 51},
            {"max_retries": True},
            {"retry_sleep": -1},
            {"retry_sleep": 61},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    Executor(workflow, {}, **kwargs)

    def setUp(self):
        self.old_desktop_env = os.environ.get(DESKTOP_AUTOMATION_ENV)
        self.old_quarantine_env = os.environ.get(
            KEYBOARD_QUARANTINE_SECONDS_ENV
        )
        self.old_app_launch_env = os.environ.get(executor_module.APP_LAUNCH_FALLBACK_ENV)
        self.old_browser_repair_env = os.environ.get(executor_module.BROWSER_NAVIGATION_REPAIR_ENV)
        os.environ[DESKTOP_AUTOMATION_ENV] = "1"
        os.environ[KEYBOARD_QUARANTINE_SECONDS_ENV] = "0"
        os.environ.pop(executor_module.APP_LAUNCH_FALLBACK_ENV, None)
        os.environ.pop(executor_module.BROWSER_NAVIGATION_REPAIR_ENV, None)
        arm_actions()
        set_abort_checker(None)

    def tearDown(self):
        set_abort_checker(None)
        panic_stop()
        if self.old_desktop_env is None:
            os.environ.pop(DESKTOP_AUTOMATION_ENV, None)
        else:
            os.environ[DESKTOP_AUTOMATION_ENV] = self.old_desktop_env
        if self.old_quarantine_env is None:
            os.environ.pop(KEYBOARD_QUARANTINE_SECONDS_ENV, None)
        else:
            os.environ[KEYBOARD_QUARANTINE_SECONDS_ENV] = self.old_quarantine_env
        if self.old_app_launch_env is None:
            os.environ.pop(executor_module.APP_LAUNCH_FALLBACK_ENV, None)
        else:
            os.environ[executor_module.APP_LAUNCH_FALLBACK_ENV] = self.old_app_launch_env
        if self.old_browser_repair_env is None:
            os.environ.pop(executor_module.BROWSER_NAVIGATION_REPAIR_ENV, None)
        else:
            os.environ[executor_module.BROWSER_NAVIGATION_REPAIR_ENV] = self.old_browser_repair_env

    def test_action_abort_guard_blocks_desktop_event(self):
        set_abort_checker(lambda: True)
        try:
            with self.assertRaises(ActionAborted):
                click(1, 1)
        finally:
            set_abort_checker(None)

    def test_actions_default_to_stopped_until_armed(self):
        panic_stop()
        self.assertTrue(actions_stopped())
        with self.assertRaises(ActionAborted):
            click(1, 1)

    def test_clipboard_read_tolerates_invalid_utf8_bytes(self):
        class Completed:
            stdout = b"hello\xa1world"

        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return Completed()

        with patched_executor(subprocess=type("FakeSubprocess", (), {
            "PIPE": executor_module.subprocess.PIPE,
            "DEVNULL": executor_module.subprocess.DEVNULL,
            "run": staticmethod(fake_run),
        })()):
            text = executor_module._read_clipboard_text()

        self.assertEqual(text, "hello�world")
        self.assertEqual(calls[0][0], ["pbpaste"])
        self.assertFalse(calls[0][1].get("text", False))

    def test_stop_after_agent_decision_prevents_hotkey(self):
        workflow = Workflow(
            "stop_after_llm",
            "Stop After LLM",
            "Stop After LLM",
            "Smoke",
            steps=[WorkflowStep(1, "Save", action_type="hotkey", value="cmd+s")],
        )
        calls = []
        stop_calls = {"count": 0}

        def should_stop():
            stop_calls["count"] += 1
            return stop_calls["count"] >= 2

        with patched_executor(
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "hotkey",
                "value": "cmd+s",
                "reason": "continue",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "",
            press_hotkey=lambda combo: calls.append(combo),
        ):
            result = Executor(workflow, {}, should_stop=should_stop, max_retries=0).run()

        self.assertFalse(result.success)
        self.assertEqual(calls, [])
        self.assertEqual(result.step_results[0].state.name, "FAILED")

    def test_execute_step_action_checks_abort_before_dispatch(self):
        calls = []
        step = WorkflowStep(1, "Save", action_type="hotkey", value="cmd+s")
        loc = LocalizationResult(
            x=0,
            y=0,
            confidence=1.0,
            likelihood_conf=1.0,
            spatial_conf=1.0,
            method="direct",
        )
        set_abort_checker(lambda: True)
        try:
            with patched_executor(press_hotkey=lambda combo: calls.append(combo)):
                with self.assertRaises(ActionAborted):
                    executor_module._execute_step_action(step, loc, {})
        finally:
            set_abort_checker(None)

        self.assertEqual(calls, [])

    def test_executor_sleep_does_not_pass_negative_duration_to_sleep(self):
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

        workflow = Workflow("sleep_guard", "Sleep Guard", "Sleep Guard", "Smoke")
        executor = Executor(workflow, {})
        fake_time = JumpingTime()
        old_time = executor_module.time
        executor_module.time = fake_time
        try:
            self.assertTrue(executor._sleep_interruptible(0.1))
        finally:
            executor_module.time = old_time

        self.assertEqual(fake_time.sleeps, [])

    def test_non_visual_keyboard_step_settles_before_next_action(self):
        workflow = Workflow(
            "keyboard_settle",
            "Keyboard Settle",
            "Keyboard Settle",
            "Smoke",
            steps=[
                WorkflowStep(1, "Submit", action_type="hotkey", value="enter"),
                WorkflowStep(2, "Paste", action_type="hotkey", value="cmd+v"),
            ],
        )
        hotkeys = []
        sleep_calls = []

        executor = Executor(workflow, {}, max_retries=0, agent_first=False)

        def stop_during_settle(duration):
            sleep_calls.append(duration)
            return False

        executor._sleep_interruptible = stop_during_settle
        with patched_executor(
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not be called in record-first mode"),
            capture_screenshot=lambda: self.fail("screenshot should not be captured in record-first mode"),
            parse_screenshot=lambda shot: self.fail("screenshot should not be parsed in record-first mode"),
            get_active_app=lambda: "",
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = executor.run()

        self.assertFalse(result.success)
        self.assertEqual(hotkeys, ["enter"])
        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(result.step_results[0].error, "Replay stopped.")

    def test_coordinate_only_step_does_not_retry_visual_matching(self):
        step = WorkflowStep(1, "Click coordinate", id="click-step", action_type="click")
        workflow = Workflow(
            "coord_only",
            "Coord Only",
            "Coord Only",
            "Smoke",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[100.0, 200.0],
            ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        clicks = []

        with patched_executor(
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "click",
                "action": "Click coordinate",
                "target_hint": "",
                "reason": "continue",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(workflow, {"click-step": subgraph}, max_retries=5).run()

        step_result = result.step_results[0]
        self.assertTrue(result.success)
        self.assertEqual(step_result.retries, 0)
        self.assertEqual(step_result.localization.method, "coord_fallback_no_visual_context_scaled")
        self.assertEqual(clicks, [(100.0, 200.0)])
        self.assertTrue(actions_stopped())

    def test_record_first_browser_scroll_uses_recorded_delta_without_vision_or_llm(self):
        step = WorkflowStep(
            1,
            "Scroll the browser article",
            id="browser-scroll",
            action_type="scroll",
            active_app_name="Google Chrome",
            metadata={"scroll_dx": 0, "scroll_dy": -4},
        )
        workflow = Workflow(
            "recorded_browser_scroll",
            "Recorded Browser Scroll",
            "Recorded Browser Scroll",
            "Scroll the active browser page.",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=1,
            click_coordinates=[600.0, 400.0],
            ui_graph=UIGraph(
                nodes=[UINode(id=1, pos=[560.0, 360.0, 80.0, 80.0], elem_type="text", content="Article")],
                image_size=[1200, 800],
            ),
            window_bounds=[0.0, 0.0, 1200.0, 800.0],
        )
        scrolls = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _get_front_window_bounds=lambda app_name: [0.0, 0.0, 1200.0, 800.0],
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not run for recorded browser scroll"),
            capture_screenshot=lambda: self.fail("screenshot should not run for recorded browser scroll"),
            parse_screenshot=lambda shot: self.fail("parser should not run for recorded browser scroll"),
            get_active_app=lambda: "Google Chrome",
            scroll=lambda *args, **kwargs: scrolls.append((args, kwargs)),
        ):
            result = Executor(
                workflow,
                {"browser-scroll": subgraph},
                max_retries=5,
                agent_first=False,
            ).run()

        self.assertTrue(result.success)
        self.assertEqual(scrolls, [((600.0, 400.0, 0, -4), {})])
        self.assertEqual(result.step_results[0].retries, 0)
        self.assertFalse(result.step_results[0].observation_metrics)

    def test_recorded_scroll_fast_path_is_limited_to_browsers(self):
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[10.0, 20.0],
            ui_graph=UIGraph(nodes=[], image_size=[100, 100]),
            window_bounds=[0.0, 0.0, 100.0, 100.0],
        )

        browser = WorkflowStep(1, "Scroll", action_type="scroll", active_app_name="Google Chrome")
        editor = WorkflowStep(1, "Scroll", action_type="scroll", active_app_name="Visual Studio Code")

        self.assertTrue(executor_module._uses_recorded_scroll_fast_path(browser, subgraph))
        self.assertFalse(executor_module._uses_recorded_scroll_fast_path(editor, subgraph))

    def test_messaging_app_name_variants_remain_guarded(self):
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[10.0, 20.0],
            ui_graph=UIGraph(nodes=[], image_size=[100, 100]),
            window_bounds=[0.0, 0.0, 100.0, 100.0],
        )

        for app_name in ("WeChat (2)", "Slack Beta"):
            with self.subTest(app_name=app_name):
                step = WorkflowStep(1, "Scroll", action_type="scroll", active_app_name=app_name)
                self.assertTrue(executor_module._is_messaging_app(app_name))
                self.assertFalse(executor_module._uses_recorded_scroll_fast_path(step, subgraph))

    def test_drag_step_replays_recorded_selection_range(self):
        step = WorkflowStep(
            1,
            "Select text",
            id="drag-step",
            action_type="drag",
            metadata={
                "drag_start": [100.0, 200.0],
                "drag_end": [300.0, 240.0],
                "drag_duration_seconds": 0.4,
            },
        )
        workflow = Workflow(
            "drag_selection",
            "Drag Selection",
            "Drag Selection",
            "Smoke",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[100.0, 200.0],
            ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        drags = []

        with patched_executor(
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "drag",
                "action": "Select text",
                "reason": "continue",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "",
            drag=lambda sx, sy, ex, ey, duration=0.0: drags.append((sx, sy, ex, ey, duration)),
        ):
            result = Executor(workflow, {"drag-step": subgraph}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(drags, [(100.0, 200.0, 300.0, 240.0, 0.4)])

    def test_app_bound_coordinate_step_fails_when_target_app_not_active(self):
        step = WorkflowStep(
            1,
            "Click target app input",
            id="click-step",
            action_type="click",
            active_app_name="REDcity",
        )
        workflow = Workflow(
            "target_app_required",
            "Target App Required",
            "Target App Required",
            "Smoke",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[100.0, 200.0],
            ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        clicks = []

        with patched_executor(
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "click",
                "action": "Click input",
                "target_hint": "",
                "reason": "continue",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            _ensure_active_app=lambda *args, **kwargs: False,
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(workflow, {"click-step": subgraph}, max_retries=5).run()

        self.assertFalse(result.success)
        self.assertIn("Refusing recorded-coordinate fallback", result.error)
        self.assertEqual(clicks, [])

    def test_target_app_activation_happens_before_agent_decision(self):
        step = WorkflowStep(
            1,
            "Type into browser",
            action_type="type",
            value="acm",
            active_app_name="Google Chrome",
        )
        workflow = Workflow(
            "activate_before_agent",
            "Activate Before Agent",
            "Activate Before Agent",
            "Smoke",
            steps=[step],
        )
        activations = []
        typed = []

        def fake_ensure_active_app(step, *args, **kwargs):
            activations.append(step.active_app_name)
            return True

        def fake_llm(*args, **kwargs):
            self.assertEqual(activations[0], "Google Chrome")
            return {
                "should_execute": True,
                "action_type": "type",
                "value": "acm",
                "reason": "target app was activated before deciding",
            }

        with patched_executor(
            _ensure_active_app=fake_ensure_active_app,
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            type_text=lambda text: typed.append(text),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertGreaterEqual(len(activations), 1)
        self.assertEqual(typed, ["acm"])

    def test_agent_decision_receives_current_screenshot_image(self):
        step = WorkflowStep(
            1,
            "Type into browser",
            action_type="type",
            value="acm",
            active_app_name="Google Chrome",
        )
        workflow = Workflow(
            "vision_step",
            "Vision Step",
            "Vision Step",
            "Smoke",
            steps=[step],
        )
        screenshot = FakeScreenshot()
        seen = {}

        def fake_llm(*args, **kwargs):
            seen["image"] = kwargs.get("image")
            seen["prompt"] = args[1]
            return {
                "should_execute": True,
                "action_type": "type",
                "value": "acm",
                "reason": "continue after inspecting screenshot",
            }

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _focused_control_snapshot=lambda app: {
                "role": "AXTextField",
                "value": "existing content",
            },
            call_json_llm=fake_llm,
            capture_screenshot=lambda: screenshot,
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            type_text=lambda text: None,
        ):
            result = Executor(
                workflow,
                {},
                variables={"self_recipient_name": "韩晨（实习）"},
                max_retries=0,
            ).run()

        self.assertTrue(result.success)
        self.assertIs(seen["image"], screenshot)
        self.assertIn('"screenshot_attached": true', seen["prompt"])
        self.assertIn('"self_recipient_name": "韩晨（实习）"', seen["prompt"])
        self.assertTrue(result.step_results[0].agent_decision["vision_input"])
        self.assertEqual(result.step_results[0].agent_decision["vision_image_size"], [1000, 800])

    def test_browser_navigation_recording_is_repaired_to_open_url_by_default(self):
        workflow = Workflow(
            "browser_repair",
            "Browser Repair",
            "Open ACM TechNews and copy the first news item.",
            "Smoke",
            task_description="打开acmtechnews，复制第一条新闻",
            steps=[
                WorkflowStep(
                    1,
                    "Click on the browser address bar",
                    id="address",
                    action_type="click",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    'Type text: "acm"',
                    action_type="type",
                    value="acm",
                    active_app_name="Google Chrome",
                ),
            ],
        )
        opened = []
        clicks = []
        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _get_front_window_bounds=lambda app_name: [0, 0, 1000, 800],
            _set_front_window_bounds=lambda app_name, bounds: True,
            _open_url_in_browser=lambda url, app_name="": opened.append((url, app_name)),
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not be called for browser navigation repair"),
            capture_screenshot=lambda: self.fail("screenshot should not be captured for browser navigation repair"),
            parse_screenshot=lambda shot: self.fail("screenshot should not be parsed for browser navigation repair"),
            get_active_app=lambda: "Google Chrome",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(
                workflow,
                {
                    "address": StepSubgraph(
                        target_element_id=0,
                        click_coordinates=[10.0, 20.0],
                        ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
                        window_bounds=[0.0, 0.0, 1000.0, 800.0],
                    ),
                },
                max_retries=0,
            ).run()

        self.assertTrue(result.success)
        self.assertEqual(opened, [("https://technews.acm.org/", "Google Chrome")])
        self.assertEqual(clicks, [])
        self.assertEqual(result.step_results[0].agent_decision["action_type"], "open_url")
        self.assertEqual(result.step_results[1].agent_decision["action_type"], "skip")

    def test_browser_find_is_not_repaired_into_filename_url(self):
        workflow = Workflow(
            "browser_find",
            "Browser Find",
            "Find a symbol in executor.py.",
            "Smoke",
            task_description="在 executor.py 中查找 class Executor",
            steps=[
                WorkflowStep(
                    1,
                    "Press cmd+f — browser find",
                    action_type="hotkey",
                    value="cmd+f",
                    active_app_name="Google Chrome",
                    metadata={
                        "input_source": "accessibility_automation",
                        "target_hint": "browser find",
                    },
                ),
                WorkflowStep(
                    2,
                    "Type 'class Executor' in Find text field",
                    action_type="type",
                    value="class Executor",
                    active_app_name="Google Chrome",
                    metadata={
                        "input_source": "accessibility_automation",
                        "target_hint": "Find text field",
                    },
                ),
            ],
        )
        hotkeys = []
        typed = []
        opened = []
        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _focused_control_snapshot=lambda app_name: {},
            get_active_app=lambda: "Google Chrome",
            press_hotkey=lambda value: hotkeys.append(value),
            type_text=lambda value: typed.append(value),
            _open_url_in_browser=lambda url, app_name="": opened.append(url),
            call_json_llm=lambda *args, **kwargs: self.fail("browser Find should use recorded fast path"),
            capture_screenshot=lambda: self.fail("browser Find should not require a screenshot"),
        ):
            result = Executor(workflow, {}, max_retries=0, agent_first=True).run()

        self.assertTrue(result.success)
        self.assertEqual(opened, [])
        self.assertEqual(hotkeys, ["cmd+f"])
        self.assertEqual(typed, ["class Executor"])

    def test_agent_first_replays_external_recorded_coordinate(self):
        workflow = Workflow(
            "external_coordinate",
            "External Coordinate",
            "Click the gpa directory.",
            "Smoke",
            steps=[WorkflowStep(
                1,
                "Click gpa directory",
                id="gpa",
                action_type="click",
                active_app_name="Google Chrome",
                metadata={"input_source": "accessibility_automation", "target_hint": "gpa directory"},
            )],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[76.0, 483.0],
            ui_graph=UIGraph(nodes=[], image_size=[1470, 956]),
            window_bounds=[0.0, 0.0, 1470.0, 956.0],
        )
        clicks = []
        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _get_front_window_bounds=lambda app_name: [0, 0, 1470, 956],
            _set_front_window_bounds=lambda app_name, bounds: True,
            get_active_app=lambda: "Google Chrome",
            click=lambda x, y: clicks.append((x, y)),
            call_json_llm=lambda *args, **kwargs: self.fail("external coordinate should use recorded fast path"),
            capture_screenshot=lambda: self.fail("external coordinate should not require a screenshot"),
        ):
            result = Executor(workflow, {"gpa": subgraph}, max_retries=0, agent_first=True).run()

        self.assertTrue(result.success)
        self.assertEqual(clicks, [(76.0, 483.0)])

    def test_agent_first_opens_external_click_semantic_target(self):
        target = "https://github.com/hanshenmesen/gpa/tree/main/gpa"
        workflow = Workflow(
            "external_semantic_navigation",
            "External Semantic Navigation",
            "Open the gpa directory.",
            "Smoke",
            steps=[WorkflowStep(
                1,
                "Click gpa directory",
                id="gpa",
                action_type="click",
                active_app_name="Google Chrome",
                metadata={
                    "input_source": "accessibility_automation",
                    "target_hint": "gpa directory",
                    "target_url": target,
                },
            )],
        )
        opened = []
        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            get_active_app=lambda: "Google Chrome",
            _open_url_in_browser=lambda url, app_name="": opened.append((url, app_name)),
            call_json_llm=lambda *args, **kwargs: self.fail("semantic navigation should be deterministic"),
            capture_screenshot=lambda: self.fail("semantic navigation should not need a screenshot"),
        ):
            result = Executor(workflow, {}, max_retries=0, agent_first=True).run()

        self.assertTrue(result.success)
        self.assertEqual(opened, [(target, "Google Chrome")])

    def test_browser_navigation_repair_can_be_disabled_by_env(self):
        os.environ[executor_module.BROWSER_NAVIGATION_REPAIR_ENV] = "0"
        workflow = Workflow(
            "browser_repair_disabled",
            "Browser Repair Disabled",
            "Open ACM TechNews.",
            "Smoke",
            task_description="打开acmtechnews",
            steps=[
                WorkflowStep(
                    1,
                    "Click on the browser address bar",
                    id="address",
                    action_type="click",
                    active_app_name="Google Chrome",
                ),
            ],
        )
        opened = []
        clicks = []
        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _get_front_window_bounds=lambda app_name: [0, 0, 1000, 800],
            _set_front_window_bounds=lambda app_name, bounds: True,
            _open_url_in_browser=lambda url, app_name="": opened.append((url, app_name)),
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "click",
                "action": "Click address bar",
                "reason": "recorded click",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(
                workflow,
                {
                    "address": StepSubgraph(
                        target_element_id=0,
                        click_coordinates=[10.0, 20.0],
                        ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
                        window_bounds=[0.0, 0.0, 1000.0, 800.0],
                    ),
                },
                max_retries=0,
            ).run()

        self.assertTrue(result.success)
        self.assertEqual(opened, [])
        self.assertEqual(clicks, [(10.0, 20.0)])

    def test_browser_navigation_repair_handles_multiple_browser_targets(self):
        workflow = Workflow(
            "browser_multi_target",
            "Browser Multi Target",
            "Open ACM TechNews, then open ChatGPT.",
            "Smoke",
            task_description="打开acmtechnews，然后打开chatgpt翻译",
            variables=[
                WorkflowVariable("chatgpt_url", "https://chatgpt.com", "ChatGPT URL"),
            ],
            steps=[
                WorkflowStep(
                    1,
                    "Click the browser address bar",
                    action_type="click",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    "Type ACM TechNews search query",
                    action_type="type",
                    value="acmtechnews",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    3,
                    "Submit the search or address bar entry",
                    action_type="hotkey",
                    value="enter",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    4,
                    "Click the browser address bar to open ChatGPT",
                    action_type="click",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    5,
                    "Type the ChatGPT website URL",
                    action_type="type",
                    value="{{chatgpt_url}}",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    6,
                    "Navigate to ChatGPT",
                    action_type="hotkey",
                    value="enter",
                    active_app_name="Google Chrome",
                ),
            ],
        )
        opened = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _get_front_window_bounds=lambda app_name: [0, 0, 1000, 800],
            _set_front_window_bounds=lambda app_name, bounds: True,
            _open_url_in_browser=lambda url, app_name="": opened.append((url, app_name)),
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not be called for browser navigation repair"),
            capture_screenshot=lambda: self.fail("screenshot should not be captured for browser navigation repair"),
            parse_screenshot=lambda shot: self.fail("screenshot should not be parsed for browser navigation repair"),
            get_active_app=lambda: "Google Chrome",
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(
            opened,
            [
                ("https://technews.acm.org/", "Google Chrome"),
                ("https://chatgpt.com", "Google Chrome"),
            ],
        )
        self.assertEqual(result.step_results[0].agent_decision["action_type"], "open_url")
        self.assertEqual(result.step_results[1].agent_decision["action_type"], "skip")
        self.assertEqual(result.step_results[3].agent_decision["action_type"], "open_url")
        self.assertEqual(result.step_results[4].agent_decision["action_type"], "skip")

    def test_browser_copy_recovers_and_extracts_first_news_when_clipboard_unchanged(self):
        selected_text = (
            "U.N. AI Safety Panel Says 'Catastrophic Harm' Can't Be Ruled Out\n"
            "The U.N.'s first independent scientific assessment of AI concluded that no one "
            "can currently guarantee advanced AI systems will not cause catastrophic harm."
        )
        workflow = Workflow(
            "copy_repair",
            "Copy Repair",
            "Open ACM TechNews and copy the first news item.",
            "Smoke",
            task_description="打开acmtechnews，复制第一条新闻",
            steps=[
                WorkflowStep(
                    1,
                    "Open ACM TechNews",
                    action_type="open_url",
                    value="https://technews.acm.org/",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    "Copy the selected news content",
                    action_type="hotkey",
                    value="cmd+c",
                    active_app_name="Google Chrome",
                    metadata={"recorded_clipboard_text": selected_text},
                ),
            ],
        )
        page_text = """
Show Headlines
Banner
Welcome to the July 6, 2026 edition of ACM TechNews, providing timely information for computer professionals three times a week.
The UN panel said its approach to AI was scientific, not political
U.N. AI Safety Panel Says 'Catastrophic Harm' Can't Be Ruled Out
The U.N.'s first independent scientific assessment of AI concluded that no one can currently guarantee advanced AI systems will not cause catastrophic harm.
[ » Read full article ]
Decrypt; Jose Antonio Lanz (July 1, 2026)
ACM Posts Strongest JCR, Impact Factor Showing to Date
"""
        clipboard_reads = ["old clipboard", "old clipboard", page_text]
        hotkeys = []
        writes = []

        def fake_read_clipboard():
            if clipboard_reads:
                return clipboard_reads.pop(0)
            return page_text

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _open_url_in_browser=lambda *args, **kwargs: None,
            _get_front_window_bounds=lambda app_name: [0, 40, 1512, 982],
            _read_clipboard_text=fake_read_clipboard,
            _write_clipboard_text=lambda text: writes.append(text),
            _browser_page_text=lambda app_name: page_text,
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "hotkey",
                "value": "cmd+c",
                "reason": "copy",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            click=lambda *args, **kwargs: None,
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(hotkeys, [])
        self.assertEqual(writes, [executor_module._extract_first_news_item(page_text, workflow)])

    def test_browser_copy_without_recorded_selection_recovers_current_page_text_for_page_copy(self):
        workflow = Workflow(
            "copy_repair_dom",
            "Copy Repair DOM",
            "Open ACM TechNews and copy the first news item.",
            "Smoke",
            task_description="打开acmtechnews，复制第一条新闻",
            steps=[
                WorkflowStep(
                    1,
                    "Open ACM TechNews",
                    action_type="open_url",
                    value="https://technews.acm.org/",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    "Copy the selected news content",
                    action_type="hotkey",
                    value="cmd+c",
                    active_app_name="Google Chrome",
                ),
            ],
        )
        page_text = """
Welcome to the July 6, 2026 edition of ACM TechNews
U.N. AI Safety Panel Says 'Catastrophic Harm' Can't Be Ruled Out
The U.N.'s first independent scientific assessment of AI concluded that no one can guarantee advanced AI systems will not cause catastrophic harm.
[ » Read full article ]
Decrypt; Jose Antonio Lanz (July 1, 2026)
Second item
"""
        clipboard_reads = ["old", "old", "old"]
        hotkeys = []
        writes = []

        def fake_read_clipboard():
            if clipboard_reads:
                return clipboard_reads.pop(0)
            return "old"

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _open_url_in_browser=lambda *args, **kwargs: None,
            _get_front_window_bounds=lambda app_name: [0, 40, 1512, 982],
            _read_clipboard_text=fake_read_clipboard,
            _write_clipboard_text=lambda text: writes.append(text),
            _wait_for_clipboard_copy=lambda before: "old",
            _browser_page_text=lambda app_name: page_text,
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "hotkey",
                "value": "cmd+c",
                "reason": "copy",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            click=lambda *args, **kwargs: None,
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(hotkeys, [])
        self.assertEqual(writes, [executor_module._extract_first_news_item(page_text, workflow)])

    def test_browser_page_copy_skips_recorded_drag_and_writes_dom_text(self):
        workflow = Workflow(
            "copy_page_without_drag",
            "Copy Page Without Drag",
            "Open ACM TechNews and copy the article content.",
            "Smoke",
            task_description="打开 ACM TechNews，复制文章正文",
            steps=[
                WorkflowStep(
                    1,
                    "Select the article text to copy",
                    id="drag-copy",
                    action_type="drag",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    "Copy the selected article text",
                    action_type="hotkey",
                    value="cmd+c",
                    active_app_name="Google Chrome",
                ),
            ],
        )
        subgraphs = {
            "drag-copy": StepSubgraph(
                target_element_id=0,
                click_coordinates=[200.0, 300.0],
                ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
                window_bounds=[0.0, 0.0, 1000.0, 800.0],
            )
        }
        page_text = "Article title\nArticle body"
        drags = []
        hotkeys = []
        writes = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _browser_page_text=lambda app_name: page_text,
            _write_clipboard_text=lambda text: writes.append(text),
            _read_clipboard_text=lambda: "old",
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not be called for direct browser page copy"),
            capture_screenshot=lambda: self.fail("screenshot should not be captured for direct browser page copy"),
            parse_screenshot=lambda shot: self.fail("screenshot should not be parsed for direct browser page copy"),
            get_active_app=lambda: "Google Chrome",
            drag=lambda *args, **kwargs: drags.append(args),
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, subgraphs, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(drags, [])
        self.assertEqual(hotkeys, [])
        self.assertEqual(writes, [page_text])

    def test_browser_selection_copy_does_not_trigger_whole_page_recovery(self):
        workflow = Workflow(
            "selection_copy",
            "Selection Copy",
            "Copy source text from the page.",
            "Source contract audit",
            task_description="复制页面中的契约文本",
        )
        step = WorkflowStep(
            1,
            "复制当前匹配文本",
            action_type="hotkey",
            value="cmd+c",
            active_app_name="Google Chrome",
            metadata={"browser_copy_mode": "selection"},
        )

        self.assertFalse(executor_module._workflow_requests_browser_page_copy(workflow, step))

    def test_browser_goal_repair_distinguishes_search_query_from_page_input(self):
        workflow = Workflow(
            "browser_goal_repair",
            "Browser Goal Repair",
            "Open ACM TechNews.",
            "Smoke",
            task_description="Search for acmtechnew and open the website.",
        )
        search_step = WorkflowStep(
            1,
            "Type the search query",
            action_type="type",
            value="acmtechnew",
            active_app_name="Google Chrome",
        )
        page_input_step = WorkflowStep(
            2,
            "Type ACM TechNews into the page title field",
            action_type="type",
            value="ACM TechNews",
            active_app_name="Google Chrome",
        )

        self.assertTrue(
            executor_module._is_browser_navigation_noise_after_goal(workflow, search_step)
        )
        self.assertFalse(
            executor_module._is_browser_navigation_noise_after_goal(workflow, page_input_step)
        )

    def test_record_first_replay_skips_screenshot_and_llm_for_deterministic_steps(self):
        workflow = Workflow(
            "record_first_web_to_mubu",
            "Record First Web To Mubu",
            "Search Google Chrome for a query, copy content, and paste it into Mubu.",
            "Smoke",
            task_description='Search the web for "acmtechnew", open a result, copy content from the page, and paste it into a Mubu document.',
            steps=[
                WorkflowStep(
                    1,
                    "Click the browser address bar",
                    id="open",
                    action_type="click",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    "Type the search query",
                    action_type="type",
                    value="acmtechnew",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    3,
                    "Submit the search",
                    action_type="hotkey",
                    value="enter",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    4,
                    "Open the relevant search result",
                    id="result",
                    action_type="click",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    5,
                    "Paste the copied content into Mubu",
                    action_type="hotkey",
                    value="cmd+v",
                    active_app_name="幕布",
                ),
            ],
        )
        subgraphs = {
            "open": StepSubgraph(
                target_element_id=0,
                click_coordinates=[10.0, 20.0],
                ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
                window_bounds=[0.0, 0.0, 1000.0, 800.0],
            ),
            "result": StepSubgraph(
                target_element_id=0,
                click_coordinates=[300.0, 350.0],
                ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
                window_bounds=[0.0, 0.0, 1000.0, 800.0],
            ),
        }
        opened = []
        clicks = []
        typed = []
        hotkeys = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _get_front_window_bounds=lambda app_name: [0, 0, 1000, 800],
            _set_front_window_bounds=lambda app_name, bounds: True,
            _open_url_in_browser=lambda url, app_name="": opened.append((url, app_name)),
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not be called in record-first fast path"),
            capture_screenshot=lambda: self.fail("screenshot should not be captured in record-first fast path"),
            parse_screenshot=lambda shot: self.fail("screenshot should not be parsed in record-first fast path"),
            get_active_app=lambda: "Google Chrome",
            click=lambda x, y: clicks.append((x, y)),
            type_text=lambda text: typed.append(text),
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, subgraphs, max_retries=0, agent_first=False).run()

        self.assertTrue(result.success)
        self.assertEqual(opened, [("https://technews.acm.org/", "Google Chrome")])
        self.assertEqual(clicks, [])
        self.assertEqual(typed, [])
        self.assertEqual(hotkeys, ["cmd+v"])
        self.assertFalse(any(item.observation_metrics for item in result.step_results))
        self.assertEqual(result.step_results[0].agent_decision["action_type"], "open_url")
        self.assertEqual(result.step_results[1].agent_decision["action_type"], "skip")

    def test_browser_copy_step_is_preserved_when_agent_requests_unlocalizable_click(self):
        selected_text = (
            "U.N. AI Safety Panel Says 'Catastrophic Harm' Can't Be Ruled Out\n"
            "The U.N.'s first independent scientific assessment of AI concluded that no one "
            "can guarantee advanced AI systems will not cause catastrophic harm."
        )
        workflow = Workflow(
            "copy_repair_with_agent_block",
            "Copy Repair With Agent Block",
            "Open ACM TechNews and copy the first news item.",
            "Smoke",
            task_description="打开acmtechnews，复制第一条新闻",
            steps=[
                WorkflowStep(
                    1,
                    "Open ACM TechNews",
                    action_type="open_url",
                    value="https://technews.acm.org/",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    2,
                    "Copy the selected news content",
                    action_type="hotkey",
                    value="cmd+c",
                    active_app_name="Google Chrome",
                    metadata={"recorded_clipboard_text": selected_text},
                ),
            ],
        )
        page_text = """
Welcome to the July 6, 2026 edition of ACM TechNews
U.N. AI Safety Panel Says 'Catastrophic Harm' Can't Be Ruled Out
The U.N.'s first independent scientific assessment of AI concluded that no one can guarantee advanced AI systems will not cause catastrophic harm.
[ » Read full article ]
Decrypt; Jose Antonio Lanz (July 1, 2026)
Second item
"""
        decisions = iter([
            {
                "should_execute": True,
                "action_type": "open_url",
                "value": "https://technews.acm.org/",
                "reason": "open",
            },
            {
                "requires_correction": True,
                "correction_action_type": "click",
                "correction": "Open first article before copying",
                "correction_target_hint": "first article title",
                "should_execute": False,
                "action_type": "hotkey",
                "skip_reason": "blocked",
                "reason": "No selected text yet.",
            },
        ])
        clipboard_reads = ["old", "old", page_text]
        hotkeys = []
        writes = []

        def fake_read_clipboard():
            if clipboard_reads:
                return clipboard_reads.pop(0)
            return page_text

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _open_url_in_browser=lambda *args, **kwargs: None,
            _get_front_window_bounds=lambda app_name: [0, 40, 1512, 982],
            _read_clipboard_text=fake_read_clipboard,
            _write_clipboard_text=lambda text: writes.append(text),
            _browser_page_text=lambda app_name: page_text,
            call_json_llm=lambda *args, **kwargs: next(decisions),
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            click=lambda *args, **kwargs: None,
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(hotkeys, [])
        self.assertEqual(result.step_results[1].agent_decision["action_type"], "hotkey")
        self.assertFalse(result.step_results[1].agent_decision["requires_correction"])
        self.assertEqual(writes, [executor_module._extract_first_news_item(page_text, workflow)])

    def test_browser_type_refuses_wrong_chatgpt_tab(self):
        workflow = Workflow(
            "wrong_chatgpt_tab",
            "Wrong ChatGPT Tab",
            "Translate article in ChatGPT.",
            "Smoke",
            steps=[
                WorkflowStep(
                    1,
                    "Type the translation prompt in ChatGPT",
                    action_type="type",
                    value="translate this",
                    active_app_name="Google Chrome",
                )
            ],
        )
        typed = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _browser_context=lambda app_name: {
                "title": "ACM TechNews",
                "url": "https://technews.acm.org/",
            },
            call_json_llm=lambda *args, **kwargs: self.fail("LLM should not be called for deterministic type"),
            capture_screenshot=lambda: self.fail("screenshot should not be captured for deterministic type"),
            parse_screenshot=lambda shot: self.fail("screenshot should not be parsed for deterministic type"),
            get_active_app=lambda: "Google Chrome",
            type_text=lambda text: typed.append(text),
        ):
            result = Executor(workflow, {}, max_retries=0, agent_first=False).run()

        self.assertFalse(result.success)
        self.assertEqual(typed, [])
        self.assertIn("Refusing to send type", result.error)

    def test_visual_chatgpt_step_refuses_wechat_coordinate_fallback(self):
        step = WorkflowStep(
            1,
            "Wait for ChatGPT to finish generating the Chinese translation",
            id="wait-chatgpt",
            action_type="scroll",
            active_app_name="WeChat",
        )
        workflow = Workflow(
            "chatgpt_wechat_mismatch",
            "ChatGPT WeChat Mismatch",
            "Wait for ChatGPT.",
            "Smoke",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[600.0, 400.0],
            ui_graph=UIGraph(nodes=[], image_size=[1000, 800]),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        scrolls = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "scroll",
                "action": step.action,
                "target_hint": "ChatGPT response",
                "reason": "Continue waiting for ChatGPT.",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "WeChat",
            scroll=lambda *args, **kwargs: scrolls.append(args),
        ):
            result = Executor(
                workflow,
                {"wait-chatgpt": subgraph},
                max_retries=0,
                agent_first=False,
            ).run()

        self.assertFalse(result.success)
        self.assertEqual(scrolls, [])
        self.assertIn("expects ChatGPT", result.error)

    def test_local_app_coordinate_click_is_not_skipped_only_because_app_is_active(self):
        step = WorkflowStep(
            1,
            "Switch to the software application: REDcity",
            id="redcity-focus",
            action_type="click",
            active_app_name="REDcity",
        )
        workflow = Workflow(
            "redcity_focus",
            "REDcity Focus",
            "REDcity Focus",
            "Focus the REDcity input before pasting.",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[277.0, 239.0],
            ui_graph=UIGraph(nodes=[], image_size=[1512, 982]),
            window_bounds=[0.0, 0.0, 1512.0, 982.0],
        )
        clicks = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": False,
                "action_type": "skip",
                "skip_reason": "already_done",
                "reason": "REDcity is already active",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "REDcity",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(workflow, {"redcity-focus": subgraph}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(len(clicks), 1)
        self.assertAlmostEqual(clicks[0][0], 277.0 * (1000.0 / 1512.0))
        self.assertAlmostEqual(clicks[0][1], 239.0 * (800.0 / 982.0))
        self.assertEqual(result.step_results[0].agent_decision["action_type"], "click")
        self.assertIn("field/control is focused", result.step_results[0].agent_decision["reason"])

    def test_selected_state_click_can_skip_without_dirtying_form(self):
        step = WorkflowStep(
            1,
            "将优先级设置为高",
            id="set-high",
            action_type="click",
            active_app_name="Google Chrome",
        )
        workflow = Workflow(
            "skip_selected_state",
            "Skip Selected State",
            "Skip Selected State",
            "Keep an already-selected option unchanged.",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[800.0, 400.0],
            ui_graph=UIGraph(
                nodes=[UINode(0, [760.0, 380.0, 80.0, 40.0], "text", "高")],
                image_size=[1000, 800],
            ),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        clicks = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _prepare_window_for_coordinate_replay=lambda *args, **kwargs: None,
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": False,
                "action_type": "skip",
                "skip_reason": "already_done",
                "confidence": 0.98,
                "reason": "High is already visibly selected.",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(workflow, {"set-high": subgraph}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(clicks, [])
        self.assertEqual(result.step_results[0].agent_decision["skip_reason"], "already_done")

    def test_agent_coordinates_override_stale_recorded_layout(self):
        step = WorkflowStep(
            1,
            "Click shifted save button",
            id="shifted-save",
            action_type="click",
            active_app_name="Google Chrome",
        )
        workflow = Workflow(
            "shifted_layout",
            "Shifted Layout",
            "Shifted Layout",
            "Use the currently visible target position.",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[100.0, 100.0],
            ui_graph=UIGraph(
                nodes=[UINode(0, [80.0, 80.0, 40.0, 40.0], "text", "Save")],
                image_size=[1000, 800],
            ),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        clicks = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _prepare_window_for_coordinate_replay=lambda *args, **kwargs: None,
            call_json_llm=lambda *args, **kwargs: {
                "should_execute": True,
                "action_type": "click",
                "target_hint": "Save",
                "target_x": 0.8,
                "target_y": 0.7,
                "target_coordinate_space": "normalized",
                "confidence": 0.97,
                "reason": "The button moved in the current layout.",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(workflow, {"shifted-save": subgraph}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(clicks, [(800.0, 560.0)])
        self.assertEqual(result.step_results[0].localization.method, "agent_action_normalized")

    def test_agent_correction_click_runs_then_redecides_same_step(self):
        step = WorkflowStep(
            1,
            "Paste copied article into REDcity self chat",
            action_type="hotkey",
            value="cmd+v",
            active_app_name="REDcity",
        )
        workflow = Workflow(
            "correction_before_paste",
            "Correction Before Paste",
            "Correction Before Paste",
            "Paste into the intended REDcity self chat.",
            task_description="把 ACM TechNews 的第一条新闻粘贴到 REDcity 的韩晨（实习）会话",
            steps=[step],
        )
        clicks = []
        hotkeys = []
        decisions = iter([
            {
                "requires_correction": True,
                "correction_action_type": "click",
                "correction": "Select the intended self chat before pasting",
                "correction_x": 0.2,
                "correction_y": 0.3,
                "correction_coordinate_space": "normalized",
                "should_execute": False,
                "action_type": "skip",
                "skip_reason": "unsafe",
                "reason": "Wrong REDcity conversation is currently open.",
            },
            {
                "requires_correction": False,
                "should_execute": True,
                "action_type": "hotkey",
                "value": "cmd+v",
                "reason": "The intended REDcity conversation is now selected.",
            },
        ])

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            call_json_llm=lambda *args, **kwargs: next(decisions),
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "REDcity",
            click=lambda x, y: clicks.append((x, y)),
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(clicks, [(200.0, 240.0)])
        self.assertEqual(hotkeys, ["cmd+v"])
        self.assertEqual(len(result.step_results[0].corrections), 1)
        self.assertEqual(result.step_results[0].corrections[0]["method"], "agent_correction_normalized")
        self.assertEqual(result.step_results[0].agent_decision["action_type"], "hotkey")

    def test_mislabeled_normalized_correction_pixels_are_not_multiplied(self):
        location = executor_module._agent_correction_localization(
            {
                "correction_x": 870,
                "correction_y": 765,
                "correction_coordinate_space": "normalized",
            },
            None,
            (1470, 956),
        )

        self.assertIsNotNone(location)
        self.assertEqual((location.x, location.y), (870.0, 765.0))
        self.assertEqual(location.method, "agent_correction_coordinate_repaired")

    def test_select_all_correction_executes_pending_type_without_reobserving(self):
        step = WorkflowStep(
            1,
            "Replace owner",
            action_type="type",
            value="Lin Chen",
            active_app_name="Google Chrome",
        )
        workflow = Workflow(
            "replace_existing_owner",
            "Replace Existing Owner",
            "Replace Existing Owner",
            "Replace the current owner exactly.",
            steps=[step],
        )
        hotkeys = []
        typed = []
        decision_calls = 0

        def fake_llm(*args, **kwargs):
            nonlocal decision_calls
            decision_calls += 1
            return {
                "requires_correction": True,
                "correction_action_type": "hotkey",
                "correction": "Select the current owner value",
                "correction_value": "cmd+a",
                "should_execute": False,
                "action_type": "type",
                "value": "Lin Chen",
                "reason": "The focused field contains a different value.",
            }

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _focused_control_snapshot=lambda app: {"role": "AXTextField", "value": "Morgan Lee"},
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "Google Chrome",
            _browser_context=lambda app: {"title": "Orders", "url": "http://127.0.0.1/orders"},
            press_hotkey=lambda combo: hotkeys.append(combo),
            type_text=lambda value: typed.append(value),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertTrue(result.success)
        self.assertEqual(decision_calls, 1)
        self.assertEqual(hotkeys, ["cmd+a"])
        self.assertEqual(typed, ["Lin Chen"])
        self.assertEqual(len(result.step_results[0].corrections), 1)

    def test_agent_correction_without_target_fails_before_paste(self):
        step = WorkflowStep(
            1,
            "Paste copied article into REDcity self chat",
            action_type="hotkey",
            value="cmd+v",
            active_app_name="REDcity",
        )
        workflow = Workflow(
            "missing_correction_target",
            "Missing Correction Target",
            "Missing Correction Target",
            "Paste into the intended REDcity self chat.",
            task_description="把 ACM TechNews 的第一条新闻粘贴到 REDcity 的韩晨（实习）会话",
            steps=[step],
        )
        hotkeys = []

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            call_json_llm=lambda *args, **kwargs: {
                "requires_correction": True,
                "correction_action_type": "click",
                "correction": "Select the intended chat",
                "should_execute": False,
                "action_type": "skip",
                "skip_reason": "unsafe",
                "reason": "The currently open chat is wrong.",
            },
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "REDcity",
            press_hotkey=lambda combo: hotkeys.append(combo),
        ):
            result = Executor(workflow, {}, max_retries=0).run()

        self.assertFalse(result.success)
        self.assertEqual(hotkeys, [])
        self.assertIn("no correction target could be localized", result.step_results[0].error)

    def test_ensure_active_app_prefers_system_events_process_activation(self):
        calls = []
        active_apps = iter(["Google Chrome", "REDcity"])

        def fake_run(command, **kwargs):
            calls.append(command)

        with patched_executor(
            get_active_app=lambda: next(active_apps, "REDcity"),
            _run_command_until_done=fake_run,
        ):
            old_system = executor_module.platform.system
            try:
                executor_module.platform.system = lambda: "Darwin"
                step = WorkflowStep(1, "Focus app", active_app_name="REDcity")

                self.assertTrue(executor_module._ensure_active_app(step, settle_seconds=0.01))
            finally:
                executor_module.platform.system = old_system

        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "osascript")
        self.assertIn("System Events", calls[0][2])
        self.assertIn("AXRaise", calls[0][2])

    def test_app_activation_commands_do_not_launch_apps_by_default(self):
        commands = executor_module._activation_commands("REDcity")

        self.assertEqual(commands[0][0], "osascript")
        self.assertEqual(len(commands), 1)
        self.assertNotIn(["open", "-a", "REDcity"], commands)

    def test_stop_terminates_app_activation_process_group(self):
        class FakeProcess:
            pid = 9876
            returncode = None

            def poll(self):
                return None

        process = FakeProcess()
        popen_calls = []
        terminated = []
        tracked = []

        def fake_start(command, **kwargs):
            popen_calls.append((command, kwargs))
            tracked.append(("track", process))
            return process

        with patched_executor(
            terminate_process=lambda proc: terminated.append(proc),
            start_action_process=fake_start,
            untrack_action_process=lambda proc: tracked.append(("untrack", proc)),
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped while activating"):
                executor_module._run_command_until_done(
                    ["osascript", "-e", "return 1"],
                    timeout_seconds=2.0,
                    should_stop=lambda: True,
                )

        self.assertEqual(terminated, [process])
        self.assertEqual(tracked, [("track", process), ("untrack", process)])
        self.assertEqual(popen_calls[0][0][0], "osascript")

    def test_app_activation_commands_include_launch_fallback_when_enabled(self):
        os.environ[executor_module.APP_LAUNCH_FALLBACK_ENV] = "1"

        commands = executor_module._activation_commands("REDcity")

        self.assertEqual(commands[0][0], "osascript")
        self.assertEqual(commands[1][0], "osascript")
        self.assertEqual(commands[2], ["open", "-a", "REDcity"])

    def test_open_url_in_browser_preserves_gpa_console_tab(self):
        scripts = []

        def fake_run_osascript(script, timeout=5.0, action_guard=False):
            scripts.append(script)
            return "http://127.0.0.1:8765/"

        with patched_executor(
            _run_osascript=fake_run_osascript,
            _sleep_with_action_guard=lambda duration: None,
        ):
            executor_module._open_url_in_browser("https://example.com", "Google Chrome")

        self.assertEqual(len(scripts), 2)
        self.assertIn("return URL of active tab of front window", scripts[0])
        self.assertIn("make new tab", scripts[1])
        self.assertIn("https://example.com", scripts[1])
        self.assertNotIn("set URL of active tab of front window", scripts[1])

    def test_open_url_in_browser_reuses_non_console_tab(self):
        scripts = []

        def fake_run_osascript(script, timeout=5.0, action_guard=False):
            scripts.append(script)
            return "https://google.com/"

        with patched_executor(
            _run_osascript=fake_run_osascript,
            _sleep_with_action_guard=lambda duration: None,
        ):
            executor_module._open_url_in_browser("https://example.com", "Google Chrome")

        self.assertEqual(len(scripts), 2)
        self.assertIn("return URL of active tab of front window", scripts[0])
        self.assertIn("set URL of active tab of front window", scripts[1])
        self.assertNotIn("make new tab", scripts[1])

    def test_open_url_in_browser_is_blocked_after_action_abort(self):
        scripts = []
        panic_stop()

        with patched_executor(
            _run_osascript=lambda script, timeout=5.0, action_guard=False: scripts.append(script) or "",
            time=type("FakeTime", (), {"sleep": staticmethod(lambda seconds: None)})(),
        ):
            with self.assertRaises(ActionAborted):
                executor_module._open_url_in_browser("https://example.com", "Google Chrome")

        self.assertEqual(scripts, [])

    def test_agent_payload_includes_documentation_guidance(self):
        workflow = Workflow(
            "doc_guided",
            "Doc Guided",
            "Configure PyCharm",
            "Use official docs to configure a run target.",
            variables=[WorkflowVariable("doc_context", "1. Click Run.\n2. Select Edit Configurations.", "Docs")],
            steps=[
                WorkflowStep(
                    1,
                    "Open Run menu",
                    action_type="hotkey",
                    value="cmd+r",
                    active_app_name="Google Chrome",
                )
            ],
            task_description="Configure PyCharm Run Debug for pytest.",
        )
        captured = {}

        def fake_llm(system, user, **kwargs):
            captured["system"] = system
            captured["payload"] = user
            return {
                "should_execute": True,
                "action_type": "hotkey",
                "value": "cmd+r",
                "reason": "follow docs",
            }

        with patched_executor(
            call_json_llm=fake_llm,
            get_active_app=lambda: "Google Chrome",
            _browser_context=lambda active_app: {"title": "Docs", "url": "https://example.com"},
        ):
            decision = executor_module._agent_step_decision(
                workflow,
                0,
                workflow.steps[0],
                {"doc_context": workflow.variables[0].default_value},
                runtime_graph=None,
                screenshot_image=None,
            )

        payload = __import__("json").loads(captured["payload"])
        guidance = payload["documentation_guidance"]
        self.assertIn("documentation_guidance", captured["system"])
        self.assertTrue(guidance["available"])
        self.assertEqual(guidance["source_variable"], "doc_context")
        self.assertEqual(guidance["hints"][0]["instruction"], "Click Run.")
        self.assertTrue(payload["workflow"]["document_search_queries"])
        self.assertEqual(decision["reason"], "follow docs")

    def test_successful_run_uses_post_run_input_fence(self):
        workflow = Workflow(
            "post_run_input_fence",
            "Post Run Input Fence",
            "Post Run Input Fence",
            "No-op workflow",
            steps=[],
        )
        calls = []

        with patched_executor(
            arm_actions=lambda: 42,
            finish_actions=lambda token=None: calls.append(("finish", token)),
            panic_stop=lambda token=None: calls.append(("panic", token)),
        ):
            result = Executor(workflow, {}).run()

        self.assertTrue(result.success)
        self.assertEqual(calls, [("finish", 42)])

    def test_final_state_verification_repairs_then_proves_completion(self):
        workflow = Workflow(
            "verified_final",
            "Verified Final",
            "Verified Final",
            "Persist a form",
            steps=[WorkflowStep(1, "Type value", action_type="type", value="done")],
            task_description="The form must visibly show a saved state.",
        )
        correction_clicks = []
        verification_calls = 0

        def fake_llm(system, user, **kwargs):
            nonlocal verification_calls
            if system == executor_module.FINAL_VERIFICATION_SYSTEM_PROMPT:
                verification_calls += 1
                if verification_calls == 1:
                    return {
                        "complete": False,
                        "requires_correction": True,
                        "correction_action_type": "click",
                        "correction": "Click Save",
                        "correction_x": 0.8,
                        "correction_y": 0.7,
                        "correction_coordinate_space": "normalized",
                        "reason": "The form is still unsaved.",
                    }
                return {
                    "complete": True,
                    "requires_correction": False,
                    "correction_action_type": "none",
                    "reason": "The saved state is visible.",
                }
            return {
                "should_execute": True,
                "action_type": "type",
                "value": "done",
                "reason": "Enter the requested value.",
            }

        with patched_executor(
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda image: UIGraph(image_size=[image.width, image.height]),
            get_active_app=lambda: "TextEdit",
            _ensure_active_app=lambda step, should_stop=None: True,
            type_text=lambda value: None,
            click=lambda x, y: correction_clicks.append((x, y)),
        ):
            result = Executor(workflow, {}, verify_final_state=True).run()

        self.assertTrue(result.success)
        self.assertEqual(correction_clicks, [(800.0, 560.0)])
        self.assertTrue(result.step_results[0].postcondition_verified)
        self.assertEqual(result.step_results[0].postcondition_attempts, 2)

    def test_final_state_verification_reobserves_async_progress(self):
        workflow = Workflow(
            "wait_for_async_save",
            "Wait For Async Save",
            "Wait For Async Save",
            "Wait until the visible save finishes.",
            steps=[WorkflowStep(1, "Type value", action_type="type", value="done")],
        )
        verification_calls = 0

        def fake_llm(system, user, **kwargs):
            nonlocal verification_calls
            if system == executor_module.FINAL_VERIFICATION_SYSTEM_PROMPT:
                verification_calls += 1
                if verification_calls == 1:
                    return {
                        "complete": False,
                        "requires_correction": False,
                        "correction_action_type": "none",
                        "reason": "The form is still saving and syncing is in progress.",
                    }
                return {
                    "complete": True,
                    "requires_correction": False,
                    "correction_action_type": "none",
                    "reason": "The saved state is now visible.",
                }
            return {"should_execute": True, "action_type": "type", "value": "done"}

        with patched_executor(
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda image: UIGraph(image_size=[image.width, image.height]),
            get_active_app=lambda: "TextEdit",
            _ensure_active_app=lambda step, should_stop=None: True,
            type_text=lambda value: None,
        ):
            executor = Executor(workflow, {}, verify_final_state=True)
            executor._sleep_interruptible = lambda duration: True
            result = executor.run()

        self.assertTrue(result.success)
        self.assertEqual(result.step_results[0].postcondition_attempts, 2)
        self.assertEqual(result.step_results[0].corrections[0]["action_type"], "wait")

    def test_final_retry_reuses_successful_completion_button_location(self):
        step = WorkflowStep(1, "Save order", id="save", action_type="click")
        workflow = Workflow(
            "retry_same_save_control",
            "Retry Same Save Control",
            "Retry Same Save Control",
            "Save the order even after a transient failure.",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[800.0, 560.0],
            ui_graph=UIGraph(image_size=[1000, 800]),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        clicks = []
        verification_calls = 0

        def fake_llm(system, user, **kwargs):
            nonlocal verification_calls
            if system == executor_module.FINAL_VERIFICATION_SYSTEM_PROMPT:
                verification_calls += 1
                if verification_calls == 1:
                    return {
                        "complete": False,
                        "requires_correction": True,
                        "correction_action_type": "click",
                        "correction": "Click Save",
                        "correction_x": 0.8,
                        "correction_y": 0.7,
                        "correction_coordinate_space": "normalized",
                    }
                if verification_calls == 2:
                    return {
                        "complete": False,
                        "requires_correction": True,
                        "correction_action_type": "click",
                        "correction": "Click Retry Save",
                        "correction_target_hint": "Retry Save",
                        "correction_x": 593,
                        "correction_y": 801,
                        "correction_coordinate_space": "normalized",
                    }
                return {
                    "complete": True,
                    "requires_correction": False,
                    "correction_action_type": "none",
                    "reason": "Saved state is visible.",
                }
            return {
                "should_execute": True,
                "action_type": "click",
                "target_x": 0.8,
                "target_y": 0.7,
                "target_coordinate_space": "normalized",
            }

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _prepare_window_for_coordinate_replay=lambda *args, **kwargs: None,
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "TextEdit",
            click=lambda x, y: clicks.append((x, y)),
            _browser_visible_control_center=lambda app, hint: None,
            _press_accessibility_control=lambda app, hint: False,
        ):
            result = Executor(
                workflow,
                {"save": subgraph},
                max_retries=0,
                verify_final_state=True,
            ).run()

        self.assertTrue(result.success)
        self.assertEqual(clicks, [(800.0, 560.0), (800.0, 560.0)])

    def test_final_state_verification_prevents_false_success(self):
        workflow = Workflow(
            "unverified_final",
            "Unverified Final",
            "Unverified Final",
            "Persist a form",
            steps=[WorkflowStep(1, "Type value", action_type="type", value="done")],
        )

        def fake_llm(system, user, **kwargs):
            if system == executor_module.FINAL_VERIFICATION_SYSTEM_PROMPT:
                return {
                    "complete": False,
                    "requires_correction": False,
                    "correction_action_type": "none",
                    "reason": "No saved confirmation is visible.",
                }
            return {"should_execute": True, "action_type": "type", "value": "done"}

        with patched_executor(
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda image: UIGraph(image_size=[image.width, image.height]),
            get_active_app=lambda: "TextEdit",
            _ensure_active_app=lambda step, should_stop=None: True,
            type_text=lambda value: None,
        ):
            result = Executor(workflow, {}, verify_final_state=True).run()

        self.assertFalse(result.success)
        self.assertFalse(result.step_results[0].postcondition_verified)
        self.assertIn("No saved confirmation", result.error)

    def test_final_preflight_skips_duplicate_save(self):
        step = WorkflowStep(1, "Save order", id="save", action_type="click")
        workflow = Workflow(
            "skip_duplicate_save",
            "Skip Duplicate Save",
            "Skip Duplicate Save",
            "Do not save an already-complete form twice.",
            steps=[step],
        )
        subgraph = StepSubgraph(
            target_element_id=0,
            click_coordinates=[800.0, 700.0],
            ui_graph=UIGraph(
                nodes=[UINode(0, [760.0, 680.0, 80.0, 40.0], "text", "Save")],
                image_size=[1000, 800],
            ),
            window_bounds=[0.0, 0.0, 1000.0, 800.0],
        )
        clicks = []

        def fake_llm(system, user, **kwargs):
            self.assertEqual(system, executor_module.FINAL_VERIFICATION_SYSTEM_PROMPT)
            return {
                "complete": True,
                "requires_correction": False,
                "correction_action_type": "none",
                "confidence": 1.0,
                "reason": "All exact values and the saved state are visible.",
            }

        with patched_executor(
            _ensure_active_app=lambda *args, **kwargs: True,
            _prepare_window_for_coordinate_replay=lambda *args, **kwargs: None,
            call_json_llm=fake_llm,
            capture_screenshot=lambda: FakeScreenshot(),
            parse_screenshot=lambda shot: UIGraph(nodes=[], image_size=[shot.width, shot.height]),
            get_active_app=lambda: "TextEdit",
            click=lambda x, y: clicks.append((x, y)),
        ):
            result = Executor(
                workflow,
                {"save": subgraph},
                max_retries=0,
                verify_final_state=True,
            ).run()

        self.assertTrue(result.success)
        self.assertEqual(clicks, [])
        self.assertTrue(result.step_results[0].postcondition_verified)
        self.assertEqual(result.step_results[0].agent_decision["skip_reason"], "already_done")

    def test_type_step_skips_when_focused_value_exactly_matches(self):
        workflow = Workflow(
            "idempotent_type",
            "Idempotent Type",
            "Idempotent Type",
            "Avoid duplicated form values",
            steps=[
                WorkflowStep(
                    1,
                    "Set owner",
                    action_type="type",
                    value="Lin Chen",
                    active_app_name="Google Chrome",
                )
            ],
        )
        typed = []

        with patched_executor(
            _ensure_active_app=lambda step, should_stop=None: True,
            _focused_control_snapshot=lambda app: {"role": "AXTextField", "value": "Lin Chen"},
            type_text=lambda value: typed.append(value),
        ):
            result = Executor(workflow, {}, verify_final_state=False).run()

        self.assertTrue(result.success)
        self.assertEqual(typed, [])
        self.assertEqual(result.step_results[0].agent_decision["skip_reason"], "already_done")

    def test_type_step_uses_deterministic_fast_path_when_focused_value_is_empty(self):
        workflow = Workflow(
            "empty_focused_type",
            "Empty Focused Type",
            "Empty Focused Type",
            "Fill an empty owner field",
            steps=[
                WorkflowStep(
                    1,
                    "Set owner",
                    action_type="type",
                    value="Lin Chen",
                    active_app_name="Google Chrome",
                )
            ],
        )
        typed = []

        with patched_executor(
            _ensure_active_app=lambda step, should_stop=None: True,
            _focused_control_snapshot=lambda app: {"role": "AXTextField", "value": ""},
            call_json_llm=lambda *args, **kwargs: self.fail("empty focused input must not call LLM"),
            get_active_app=lambda: "Google Chrome",
            _browser_context=lambda app: {"title": "Orders", "url": "http://127.0.0.1/orders"},
            type_text=lambda value: typed.append(value),
        ):
            result = Executor(workflow, {}, verify_final_state=False).run()

        self.assertTrue(result.success)
        self.assertEqual(typed, ["Lin Chen"])
        self.assertFalse(result.step_results[0].agent_decision["vision_input"])

    def test_semantic_checkpoint_steps_run_without_visual_model(self):
        workflow = Workflow(
            "checkpoint_flow",
            "Checkpoint Flow",
            "Checkpoint Flow",
            "Verify browser and clipboard state deterministically",
            steps=[
                WorkflowStep(1, "Wait briefly", action_type="wait", value="0.1"),
                WorkflowStep(
                    2,
                    "Verify repository URL",
                    action_type="assert_url",
                    value="github.com/hanshenmesen/gpa",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(3, "Write result", action_type="set_clipboard", value="audit:pass"),
                WorkflowStep(4, "Verify result", action_type="assert_clipboard", value="audit:pass"),
            ],
        )
        clipboard = {"value": ""}
        waits = []

        with patched_executor(
            _ensure_active_app=lambda step, should_stop=None: True,
            _sleep_with_action_guard=lambda seconds: waits.append(seconds),
            _browser_context=lambda app: {
                "title": "GPA",
                "url": "https://github.com/hanshenmesen/gpa",
            },
            _write_clipboard_text=lambda value: clipboard.update(value=value),
            _read_clipboard_text=lambda: clipboard["value"],
            call_json_llm=lambda *args, **kwargs: self.fail("checkpoints must not call LLM"),
        ):
            result = Executor(workflow, {}, verify_final_state=True).run()

        self.assertTrue(result.success)
        self.assertEqual(result.n_steps, 4)
        self.assertEqual(clipboard["value"], "audit:pass")
        self.assertTrue(result.step_results[-1].postcondition_verified)
        self.assertTrue(waits)

    def test_copy_accepts_an_already_satisfied_clipboard_postcondition(self):
        expected = "class Executor"
        workflow = Workflow(
            "idempotent_copy",
            "Idempotent Copy",
            "Idempotent Copy",
            "Copy a selected source contract",
            steps=[WorkflowStep(
                1,
                "Copy selected contract",
                action_type="hotkey",
                value="cmd+c",
                active_app_name="Google Chrome",
                metadata={
                    "browser_copy_mode": "selection",
                    "expected_clipboard_text": expected,
                },
            )],
        )

        with patched_executor(
            _ensure_active_app=lambda step, should_stop=None: True,
            _read_clipboard_text=lambda: expected,
            _wait_for_clipboard_copy=lambda before: expected,
            press_hotkey=lambda value: None,
            call_json_llm=lambda *args, **kwargs: self.fail("copy fast path must not call LLM"),
        ):
            result = Executor(workflow, {}, agent_first=False).run()

        self.assertTrue(result.success)
        self.assertTrue(result.step_results[0].postcondition_verified)


class patched_executor:
    def __init__(self, **replacements):
        self.replacements = replacements
        self.originals = {}

    def __enter__(self):
        for name, value in self.replacements.items():
            self.originals[name] = getattr(executor_module, name)
            setattr(executor_module, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(executor_module, name, value)


if __name__ == "__main__":
    unittest.main()
