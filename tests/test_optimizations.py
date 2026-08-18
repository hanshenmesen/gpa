"""Tests for the optimization features added on top of the GPA framework:

- P0-1 pluggable visual grounding backend + executor integration
- P0-2 cross-step execution memory
- P0-3 content-injection / deception safety gate
- P1-4 multi-frame screen-stability observation
- P1-5 error-recovery strategy library
- P1-6 vision image-detail cost control
- P2-7 cross-workflow app knowledge graph
- P2-8 retrieval-augmented documentation guidance
- P2-9 grounding benchmark harness + adapter registry
"""
import os
import unittest

from PIL import Image

import gpa.core.doc_guidance as doc_guidance
import gpa.core.grounding as grounding
import gpa.execution.executor as executor_module
import gpa.execution.recovery as recovery
import gpa.execution.safety_gate as safety_gate
from gpa.core.app_graph import build_app_graphs
from gpa.core.grounding import GroundingRequest, GroundingResult, run_grounder
from gpa.execution.executor import Executor
from gpa.integration.benchmarks import (
    BenchmarkTask,
    GroundingSample,
    evaluate_grounding,
    load_benchmark_tasks,
    point_in_box,
    register_benchmark_adapter,
    unregister_benchmark_adapter,
)
from gpa.storage.workflow import Workflow, WorkflowStep


class FakeScreenshot:
    width = 1000
    height = 800


def _click_step(number=1, action="Click the OK button", app="TextEdit"):
    return WorkflowStep(number, action, id=f"s{number}", action_type="click", active_app_name=app)


def _workflow(steps):
    return Workflow("wf", "WF", "WF", "Smoke", steps=steps)


# ──────────────────────────────────────────────────────────────────────────── #
# P0-1 grounding
# ──────────────────────────────────────────────────────────────────────────── #

class GroundingTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get(grounding.GROUNDING_BACKEND_ENV)

    def tearDown(self):
        grounding.unregister_grounder("uground_test")
        if self._old_env is None:
            os.environ.pop(grounding.GROUNDING_BACKEND_ENV, None)
        else:
            os.environ[grounding.GROUNDING_BACKEND_ENV] = self._old_env

    def test_disabled_by_default(self):
        os.environ.pop(grounding.GROUNDING_BACKEND_ENV, None)
        self.assertFalse(grounding.grounding_enabled())
        self.assertIsNone(run_grounder(GroundingRequest(instruction="ok")))

    def test_register_and_run_screen_pixels(self):
        grounding.register_grounder(
            "uground_test",
            lambda req: GroundingResult(x=321.0, y=222.0, confidence=0.9),
        )
        os.environ[grounding.GROUNDING_BACKEND_ENV] = "uground_test"
        self.assertIn("uground_test", grounding.list_grounders())
        self.assertTrue(grounding.grounding_enabled())
        res = run_grounder(GroundingRequest(instruction="click ok", live_size=(1000, 800)))
        self.assertIsNotNone(res)
        self.assertEqual((res.x, res.y), (321.0, 222.0))

    def test_normalized_coordinates_scaled(self):
        grounding.register_grounder(
            "uground_test",
            lambda req: GroundingResult(x=0.5, y=0.25, confidence=0.8, coordinate_space="normalized"),
        )
        os.environ[grounding.GROUNDING_BACKEND_ENV] = "uground_test"
        res = run_grounder(GroundingRequest(instruction="x", live_size=(1000, 800)))
        self.assertEqual((res.x, res.y), (500.0, 200.0))

    def test_empty_instruction_returns_none(self):
        grounding.register_grounder("uground_test", lambda req: GroundingResult(1, 1, 1.0))
        os.environ[grounding.GROUNDING_BACKEND_ENV] = "uground_test"
        self.assertIsNone(run_grounder(GroundingRequest(instruction="   ")))

    def test_executor_try_grounder_uses_backend(self):
        grounding.register_grounder(
            "uground_test",
            lambda req: GroundingResult(x=640.0, y=360.0, confidence=0.95),
        )
        os.environ[grounding.GROUNDING_BACKEND_ENV] = "uground_test"
        ex = Executor(_workflow([_click_step()]), {})
        loc = ex._try_grounder(
            _click_step(),
            {"target_hint": "OK button"},
            FakeScreenshot(),
            (1000, 800),
        )
        self.assertIsNotNone(loc)
        self.assertEqual((loc.x, loc.y), (640.0, 360.0))
        self.assertTrue(loc.method.startswith("grounder:"))

    def test_executor_try_grounder_disabled_returns_none(self):
        os.environ.pop(grounding.GROUNDING_BACKEND_ENV, None)
        ex = Executor(_workflow([_click_step()]), {})
        self.assertIsNone(
            ex._try_grounder(_click_step(), {"target_hint": "OK"}, FakeScreenshot(), (1000, 800))
        )


# ──────────────────────────────────────────────────────────────────────────── #
# P0-2 execution memory
# ──────────────────────────────────────────────────────────────────────────── #

class ExecutionMemoryTests(unittest.TestCase):
    def test_append_and_bound(self):
        ex = Executor(_workflow([_click_step()]), {})
        for i in range(1, 20):
            step = _click_step(number=i, action=f"Click {i}")
            result = executor_module.StepResult(step_number=i, state=executor_module.StepState.DONE)
            ex._append_execution_memory(step, result)
        self.assertEqual(len(ex._execution_memory), 12)
        self.assertEqual(ex._execution_memory[-1]["step_number"], 19)
        self.assertEqual(ex._execution_memory[-1]["outcome"], "DONE")

    def test_agent_payload_includes_execution_memory(self):
        captured = {}

        def fake_llm(system, user, **kwargs):
            captured["payload"] = user
            return {"should_execute": True, "action_type": "hotkey", "value": "enter", "reason": "go"}

        old = executor_module.call_json_llm
        old_active = executor_module.get_active_app
        executor_module.call_json_llm = fake_llm
        executor_module.get_active_app = lambda: ""
        try:
            step = WorkflowStep(2, "Submit", action_type="hotkey", value="enter")
            executor_module._agent_step_decision(
                _workflow([step]),
                1,
                step,
                {},
                runtime_graph=None,
                screenshot_image=None,
                execution_memory=[{"step_number": 1, "action_type": "click", "outcome": "DONE"}],
            )
        finally:
            executor_module.call_json_llm = old
            executor_module.get_active_app = old_active

        import json
        payload = json.loads(captured["payload"])
        self.assertEqual(payload["execution_memory"][0]["step_number"], 1)


# ──────────────────────────────────────────────────────────────────────────── #
# P0-3 safety gate
# ──────────────────────────────────────────────────────────────────────────── #

class SafetyGateTests(unittest.TestCase):
    def tearDown(self):
        for name in (
            safety_gate.REQUIRE_CONFIRM_IRREVERSIBLE_ENV,
            safety_gate.ALLOWED_URL_HOSTS_ENV,
            safety_gate.ALLOWED_RECIPIENTS_ENV,
        ):
            os.environ.pop(name, None)

    def test_is_irreversible_action(self):
        self.assertTrue(safety_gate.is_irreversible_action("hotkey", "Send message", ""))
        self.assertTrue(safety_gate.is_irreversible_action("hotkey", "confirm", "cmd+enter"))
        self.assertTrue(safety_gate.is_irreversible_action("click", "删除会话", ""))
        self.assertFalse(safety_gate.is_irreversible_action("type", "type search query", "hello"))

    def test_url_allowlist(self):
        os.environ[safety_gate.ALLOWED_URL_HOSTS_ENV] = "example.com, xiaohongshu.com"
        self.assertIsNone(safety_gate.check_url_allowed("https://docs.example.com/x"))
        self.assertIsNone(safety_gate.check_url_allowed("example.com"))
        self.assertIsNotNone(safety_gate.check_url_allowed("https://evil.com/pay"))

    def test_url_allowlist_unset_allows_all(self):
        os.environ.pop(safety_gate.ALLOWED_URL_HOSTS_ENV, None)
        self.assertIsNone(safety_gate.check_url_allowed("https://anything.com"))

    def test_recipient_allowlist(self):
        os.environ[safety_gate.ALLOWED_RECIPIENTS_ENV] = "韩晨,teamlead"
        self.assertIsNone(safety_gate.check_recipient_allowed("韩晨"))
        self.assertIsNotNone(safety_gate.check_recipient_allowed("stranger"))

    def test_executor_gate_blocks_disallowed_url(self):
        os.environ[safety_gate.ALLOWED_URL_HOSTS_ENV] = "example.com"
        step = WorkflowStep(1, "Open site", action_type="open_url", value="https://evil.com")
        ex = Executor(_workflow([step]), {})
        error = ex._check_safety_gate(step, {"action_type": "open_url", "action": "Open site"})
        self.assertIsNotNone(error)
        self.assertIn("allow-list", error)

    def test_executor_gate_confirmation_required(self):
        os.environ[safety_gate.REQUIRE_CONFIRM_IRREVERSIBLE_ENV] = "1"
        step = WorkflowStep(1, "Send the message", action_type="hotkey", value="cmd+enter")
        # No confirmation handler -> blocked.
        ex = Executor(_workflow([step]), {})
        self.assertIsNotNone(ex._check_safety_gate(step, {"action": "Send the message", "action_type": "hotkey"}))
        # Approving handler -> allowed.
        ex2 = Executor(_workflow([step]), {}, on_confirm=lambda s, d: True)
        self.assertIsNone(ex2._check_safety_gate(step, {"action": "Send the message", "action_type": "hotkey"}))
        # Rejecting handler -> blocked.
        ex3 = Executor(_workflow([step]), {}, on_confirm=lambda s, d: False)
        self.assertIsNotNone(ex3._check_safety_gate(step, {"action": "Send the message", "action_type": "hotkey"}))

    def test_agent_prompt_declares_trust_boundary(self):
        self.assertIn("TRUST BOUNDARY", executor_module.AGENT_SYSTEM_PROMPT)
        self.assertIn("UNTRUSTED", executor_module.AGENT_SYSTEM_PROMPT)


# ──────────────────────────────────────────────────────────────────────────── #
# P1-4 screen stability
# ──────────────────────────────────────────────────────────────────────────── #

class ScreenStabilityTests(unittest.TestCase):
    def test_identical_frames_similar(self):
        a = Image.new("RGB", (200, 150), (10, 20, 30))
        b = Image.new("RGB", (200, 150), (10, 20, 30))
        self.assertTrue(executor_module._screens_similar(a, b))

    def test_different_frames_not_similar(self):
        a = Image.new("RGB", (200, 150), (0, 0, 0))
        b = Image.new("RGB", (200, 150), (255, 255, 255))
        self.assertFalse(executor_module._screens_similar(a, b))

    def test_single_frame_by_default(self):
        os.environ.pop(executor_module.SCREEN_STABILITY_FRAMES_ENV, None)
        self.assertEqual(executor_module._screen_stability_frames(), 1)


# ──────────────────────────────────────────────────────────────────────────── #
# P1-5 recovery
# ──────────────────────────────────────────────────────────────────────────── #

class RecoveryTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(recovery.RECOVERY_ENABLED_ENV, None)

    def test_classify_dialog_from_error(self):
        strat = recovery.classify_failure("A modal dialog is blocking the target")
        self.assertIsNotNone(strat)
        self.assertEqual(strat.mode, "dismiss_dialog")
        self.assertTrue(strat.safe_autofix)
        self.assertEqual(strat.value, "esc")

    def test_classify_loading(self):
        self.assertEqual(recovery.classify_failure("screen still loading").mode, "wait")

    def test_classify_focus(self):
        self.assertEqual(recovery.classify_failure("target app is not active").mode, "reactivate_app")

    def test_classify_moved(self):
        self.assertEqual(recovery.classify_failure("SMC failed, target not found").mode, "reobserve")

    def test_no_match_returns_none(self):
        self.assertIsNone(recovery.classify_failure("totally unrelated message"))

    def test_safe_recovery_enabled_by_default_and_can_be_disabled(self):
        os.environ.pop(recovery.RECOVERY_ENABLED_ENV, None)
        self.assertTrue(recovery.recovery_enabled())
        os.environ[recovery.RECOVERY_ENABLED_ENV] = "0"
        self.assertFalse(recovery.recovery_enabled())


# ──────────────────────────────────────────────────────────────────────────── #
# P1-6 vision detail
# ──────────────────────────────────────────────────────────────────────────── #

class VisionDetailTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(executor_module.VISION_IMAGE_DETAIL_ENV, None)

    def test_auto_high_for_visual_low_for_text(self):
        os.environ.pop(executor_module.VISION_IMAGE_DETAIL_ENV, None)
        self.assertEqual(executor_module._vision_image_detail("click"), "high")
        self.assertEqual(executor_module._vision_image_detail("type"), "low")

    def test_forced_level(self):
        os.environ[executor_module.VISION_IMAGE_DETAIL_ENV] = "low"
        self.assertEqual(executor_module._vision_image_detail("click"), "low")


# ──────────────────────────────────────────────────────────────────────────── #
# P2-7 app graph
# ──────────────────────────────────────────────────────────────────────────── #

class AppGraphTests(unittest.TestCase):
    def test_build_and_suggest(self):
        wf1 = Workflow(
            "wf1", "WF1", "WF1", "d",
            steps=[
                WorkflowStep(1, "Click address bar", action_type="click", active_app_name="Google Chrome"),
                WorkflowStep(2, "Type query", action_type="type", value="hi", active_app_name="Google Chrome"),
            ],
        )
        wf2 = Workflow(
            "wf2", "WF2", "WF2", "d",
            steps=[
                WorkflowStep(1, "Click address bar", action_type="click", active_app_name="Chrome"),
                WorkflowStep(2, "Type query", action_type="type", value="yo", active_app_name="Chrome"),
            ],
        )
        graphs = build_app_graphs([wf1, wf2])
        self.assertIn("Google Chrome", graphs)
        graph = graphs["Google Chrome"]
        src = graph.node_key_for("Click address bar", "click")
        suggestions = graph.suggest_next(src)
        self.assertTrue(suggestions)
        # Both workflows share the same normalized transition -> count 2.
        self.assertEqual(suggestions[0].count, 2)
        self.assertEqual(suggestions[0].action_type, "type")

    def test_cross_app_transition_skipped(self):
        wf = Workflow(
            "wf", "WF", "WF", "d",
            steps=[
                WorkflowStep(1, "Copy", action_type="hotkey", value="cmd+c", active_app_name="Google Chrome"),
                WorkflowStep(2, "Paste", action_type="hotkey", value="cmd+v", active_app_name="Notes"),
            ],
        )
        graphs = build_app_graphs([wf])
        # No same-app consecutive pair -> no edges.
        self.assertTrue(all(not g.edges for g in graphs.values()) or not graphs)


# ──────────────────────────────────────────────────────────────────────────── #
# P2-8 RAG documentation guidance
# ──────────────────────────────────────────────────────────────────────────── #

class DocRetrieverTests(unittest.TestCase):
    def tearDown(self):
        doc_guidance.clear_doc_retriever()

    def test_no_retriever_returns_empty(self):
        doc_guidance.clear_doc_retriever()
        self.assertEqual(doc_guidance.retrieve_documentation(["q"]), "")

    def test_retriever_feeds_guidance_payload(self):
        doc_guidance.register_doc_retriever(
            lambda queries: "Setup guide\n1. Click Run.\n2. Select Edit Configurations."
        )
        wf = Workflow("w", "W", "W", "d", task_description="Configure run target")
        step = WorkflowStep(1, "Open Run", action_type="hotkey", value="cmd+r", active_app_name="PyCharm")
        payload = doc_guidance.document_guidance_payload(wf, {}, current_step=step)
        self.assertTrue(payload["available"])
        self.assertTrue(payload["retrieved"])
        self.assertEqual(payload["source_variable"], "retriever")
        self.assertTrue(payload["hints"])

    def test_pasted_doc_takes_priority_over_retriever(self):
        doc_guidance.register_doc_retriever(lambda queries: "1. Click Retrieved.")
        wf = Workflow("w", "W", "W", "d", task_description="task")
        step = WorkflowStep(1, "Open", action_type="hotkey", value="cmd+r")
        payload = doc_guidance.document_guidance_payload(
            wf, {"doc_context": "1. Click Pasted."}, current_step=step
        )
        self.assertFalse(payload["retrieved"])
        self.assertEqual(payload["source_variable"], "doc_context")
        self.assertEqual(payload["hints"][0]["instruction"], "Click Pasted.")


# ──────────────────────────────────────────────────────────────────────────── #
# P2-9 benchmarks
# ──────────────────────────────────────────────────────────────────────────── #

class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get(grounding.GROUNDING_BACKEND_ENV)

    def tearDown(self):
        grounding.unregister_grounder("bench_grounder")
        unregister_benchmark_adapter("demo_bench")
        if self._old_env is None:
            os.environ.pop(grounding.GROUNDING_BACKEND_ENV, None)
        else:
            os.environ[grounding.GROUNDING_BACKEND_ENV] = self._old_env

    def test_point_in_box(self):
        self.assertTrue(point_in_box(15, 25, (10, 20, 20, 20)))
        self.assertFalse(point_in_box(5, 25, (10, 20, 20, 20)))

    def test_evaluate_grounding_hit_and_miss(self):
        # Grounder always predicts (15, 25).
        grounding.register_grounder("bench_grounder", lambda req: GroundingResult(15.0, 25.0, 0.9))
        os.environ[grounding.GROUNDING_BACKEND_ENV] = "bench_grounder"
        samples = [
            GroundingSample("hit target", target_box=(10, 20, 20, 20), sample_id="a"),
            GroundingSample("miss target", target_box=(100, 100, 20, 20), sample_id="b"),
        ]
        result = evaluate_grounding(samples)
        self.assertEqual(result.total, 2)
        self.assertEqual(result.hits, 1)
        self.assertEqual(result.accuracy, 0.5)
        self.assertEqual(result.misses, ["b"])

    def test_adapter_registry(self):
        register_benchmark_adapter(
            "demo_bench",
            lambda: [BenchmarkTask("t1", "do something", app="TextEdit", platform="macos")],
        )
        tasks = load_benchmark_tasks("demo_bench")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_id, "t1")
        with self.assertRaises(ValueError):
            load_benchmark_tasks("unknown_bench")


if __name__ == "__main__":
    unittest.main()
