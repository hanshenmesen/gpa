import io
import json
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest

import demo_web.server as server
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowVariable


class DummyHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        pass


class ServerHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.old_audit_event = server._audit_event
        server._audit_event = lambda *args, **kwargs: None

    def tearDown(self):
        server._audit_event = self.old_audit_event
        server.SHUTDOWN_EVENT.clear()
        server._client_disconnect()
        with server.STATE_LOCK:
            server.STATE["run"] = {
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
            }
            server.STATE["run_stop_event"] = None
            server.STATE["run_started_monotonic"] = None
            server.STATE["run_thread"] = None
            server.STATE["replay_arm"] = {
                "token": "",
                "workflow_id": "",
                "client_id": "",
                "expires_at": 0.0,
                "issued_at": "",
            }
            server.STATE["visual_warmup"] = {
                "enabled": server.PRELOAD_VISUAL_MODELS_ENABLED,
                "status": "idle",
                "started_at": None,
                "finished_at": None,
                "duration_seconds": 0.0,
                "loaded": [],
                "errors": [],
            }
        with server.HEALTH_CACHE_LOCK:
            server.HEALTH_CACHE["value"] = None
            server.HEALTH_CACHE["expires_at"] = 0.0

    def test_client_starts_disconnected(self):
        server._client_disconnect()

        self.assertFalse(server._client_connected())
        self.assertFalse(server._client_status()["connected"])

    def test_mark_client_seen_opens_short_lived_lease(self):
        status = server._mark_client_seen("test-client")

        self.assertTrue(status["connected"])
        self.assertTrue(server._client_connected())
        self.assertEqual(server._client_status()["id"], "test-client")

    def test_expired_heartbeat_is_disconnected(self):
        server._mark_client_seen("expired-client")
        with server.STATE_LOCK:
            server.STATE["client"]["last_seen_monotonic"] = (
                time.monotonic() - server.CLIENT_HEARTBEAT_TIMEOUT - 0.1
            )

        self.assertFalse(server._client_connected())
        self.assertFalse(server._client_status()["connected"])

    def test_disconnect_clears_lease(self):
        server._mark_client_seen("closing-client")
        server._client_disconnect()

        self.assertFalse(server._client_connected())

    def test_panic_keeps_run_active_until_worker_finishes(self):
        stop_event = threading.Event()
        with server.STATE_LOCK:
            server.STATE["run"].update({
                "active": True,
                "status": "running",
                "run_id": "run-1",
                "workflow_id": "workflow-1",
            })
            server.STATE["run_stop_event"] = stop_event
            server.STATE["run_started_monotonic"] = time.monotonic()

        handler = DummyHandler()
        server._panic_replay(handler)

        self.assertEqual(handler.status, 200)
        self.assertTrue(stop_event.is_set())
        with server.STATE_LOCK:
            self.assertTrue(server.STATE["run"]["active"])
            self.assertEqual(server.STATE["run"]["status"], "panic_stopping")
            self.assertIs(server.STATE["run_stop_event"], stop_event)

    def test_stale_client_heartbeat_does_not_stop_replay(self):
        stop_event = threading.Event()
        runtime_state = {"timed_out": False, "client_disconnected": False, "client_stale": False}
        server._client_disconnect()
        with server.STATE_LOCK:
            server.STATE["run"].update({
                "active": True,
                "status": "running",
                "run_id": "run-2",
                "workflow_id": "workflow-2",
            })
            server.STATE["run_stop_event"] = stop_event

        self.assertFalse(server._stop_run_for_client_disconnect(stop_event, runtime_state))

        self.assertFalse(stop_event.is_set())
        self.assertFalse(runtime_state["client_disconnected"])
        self.assertTrue(runtime_state["client_stale"])
        with server.STATE_LOCK:
            self.assertFalse(server.STATE["run"]["stop_requested"])
            self.assertEqual(server.STATE["run"]["error"], "")

    def test_explicit_client_disconnect_panics_replay(self):
        stop_event = threading.Event()
        server._mark_client_seen("closing-client")
        with server.STATE_LOCK:
            server.STATE["run"].update({
                "active": True,
                "status": "running",
                "run_id": "run-3",
                "workflow_id": "workflow-3",
            })
            server.STATE["run_stop_event"] = stop_event
            server.STATE["run_started_monotonic"] = time.monotonic()

        handler = DummyHandler()
        server._client_disconnect_request(handler)

        self.assertEqual(handler.status, 200)
        self.assertTrue(stop_event.is_set())
        self.assertFalse(server._client_connected())
        with server.STATE_LOCK:
            self.assertEqual(server.STATE["run"]["status"], "panic_stopping")

    def test_active_client_disconnect_aborts_without_releasing_inputs(self):
        stop_event = threading.Event()
        calls = []
        old_abort = server._abort_desktop_actions
        old_panic = server._panic_desktop_actions
        server._abort_desktop_actions = lambda: calls.append("abort")
        server._panic_desktop_actions = lambda: calls.append("panic")
        server._mark_client_seen("closing-client")
        with server.STATE_LOCK:
            server.STATE["run"].update({
                "active": True,
                "status": "running",
                "run_id": "run-4",
                "workflow_id": "workflow-4",
            })
            server.STATE["run_stop_event"] = stop_event
            server.STATE["run_started_monotonic"] = time.monotonic()
        try:
            handler = DummyHandler()
            server._client_disconnect_request(handler)
        finally:
            server._abort_desktop_actions = old_abort
            server._panic_desktop_actions = old_panic

        self.assertEqual(handler.status, 200)
        self.assertTrue(stop_event.is_set())
        self.assertEqual(calls, ["abort"])

    def test_shutdown_abort_sets_stop_event_without_releasing_inputs(self):
        stop_event = threading.Event()
        calls = []
        old_abort = server._abort_desktop_actions
        old_panic = server._panic_desktop_actions
        server._abort_desktop_actions = lambda: calls.append("abort")
        server._panic_desktop_actions = lambda: calls.append("panic")
        with server.STATE_LOCK:
            server.STATE["run"].update({
                "active": True,
                "status": "running",
                "run_id": "run-5",
                "workflow_id": "workflow-5",
            })
            server.STATE["run_stop_event"] = stop_event
        try:
            active, run_id, workflow_id = server._abort_active_replay("Service shutdown requested.")
        finally:
            server._abort_desktop_actions = old_abort
            server._panic_desktop_actions = old_panic

        self.assertTrue(active)
        self.assertEqual(run_id, "run-5")
        self.assertEqual(workflow_id, "workflow-5")
        self.assertTrue(stop_event.is_set())
        self.assertEqual(calls, ["abort"])

    def test_shutdown_waits_for_replay_worker_without_holding_state_lock(self):
        joins = []

        class FakeWorker:
            def __init__(self):
                self.alive = True

            def join(self, timeout=None):
                acquired = server.STATE_LOCK.acquire(blocking=False)
                if acquired:
                    server.STATE_LOCK.release()
                joins.append((timeout, acquired))
                self.alive = False

            def is_alive(self):
                return self.alive

        worker = FakeWorker()
        with server.STATE_LOCK:
            server.STATE["run_thread"] = worker

        self.assertTrue(server._wait_for_replay_worker(timeout_seconds=1.25))
        self.assertEqual(joins, [(1.25, True)])

    def test_shutdown_tolerates_worker_not_started_yet(self):
        class UnstartedWorker:
            def join(self, timeout=None):
                raise RuntimeError("cannot join thread before it is started")

            def is_alive(self):
                return False

        with server.STATE_LOCK:
            server.STATE["run_thread"] = UnstartedWorker()

        self.assertTrue(server._wait_for_replay_worker(timeout_seconds=0.1))

    def test_finished_worker_keeps_run_slot_busy_until_thread_exits(self):
        class FinishingWorker:
            def is_alive(self):
                return True

        with server.STATE_LOCK:
            server.STATE["run"]["active"] = False
            server.STATE["run_thread"] = FinishingWorker()

        self.assertTrue(server._replay_run_slot_busy())

    def test_shutdown_gate_refuses_to_start_a_new_replay_worker(self):
        class FakeWorker:
            def __init__(self):
                self.started = False

            def start(self):
                self.started = True

            def is_alive(self):
                return self.started

        worker = FakeWorker()
        stop_event = threading.Event()
        run_state = dict(server.STATE["run"])
        run_state.update({"active": True, "status": "running", "run_id": "blocked-run"})
        server.SHUTDOWN_EVENT.set()

        error = server._begin_replay_worker(worker, run_state, stop_event)

        self.assertIn("shutting down", error.lower())
        self.assertFalse(worker.started)
        with server.STATE_LOCK:
            self.assertFalse(server.STATE["run"]["active"])
            self.assertIsNone(server.STATE["run_thread"])

    def test_idle_client_disconnect_does_not_touch_desktop_actions(self):
        calls = []
        old_panic = server._panic_desktop_actions
        server._panic_desktop_actions = lambda: calls.append("panic")
        server._mark_client_seen("idle-client")
        try:
            handler = DummyHandler()
            server._client_disconnect_request(handler)
        finally:
            server._panic_desktop_actions = old_panic

        self.assertEqual(handler.status, 200)
        self.assertEqual(calls, [])
        self.assertFalse(server._client_connected())

    def test_idle_panic_request_does_not_touch_desktop_actions(self):
        calls = []
        old_panic = server._panic_desktop_actions
        server._panic_desktop_actions = lambda: calls.append("panic")
        try:
            handler = DummyHandler()
            server._panic_replay(handler)
        finally:
            server._panic_desktop_actions = old_panic

        self.assertEqual(handler.status, 200)
        self.assertEqual(calls, [])

    def test_replay_arm_token_is_required_and_single_use(self):
        server._mark_client_seen("client-1")

        arm = server._issue_replay_arm("workflow-1")
        ok, error = server._consume_replay_arm("workflow-1", "")
        self.assertFalse(ok)
        self.assertIn("missing arm token", error)

        arm = server._issue_replay_arm("workflow-1")
        ok, error = server._consume_replay_arm("workflow-1", arm["arm_token"])
        self.assertTrue(ok)
        self.assertEqual(error, "")

        ok, error = server._consume_replay_arm("workflow-1", arm["arm_token"])
        self.assertFalse(ok)
        self.assertIn("no armed replay", error)

    def test_replay_arm_rejects_wrong_workflow_and_expired_token(self):
        server._mark_client_seen("client-1")

        arm = server._issue_replay_arm("workflow-1")
        ok, error = server._consume_replay_arm("workflow-2", arm["arm_token"])
        self.assertFalse(ok)
        self.assertIn("different workflow", error)

        arm = server._issue_replay_arm("workflow-1")
        with server.STATE_LOCK:
            server.STATE["replay_arm"]["expires_at"] = time.monotonic() - 1
        ok, error = server._consume_replay_arm("workflow-1", arm["arm_token"])
        self.assertFalse(ok)
        self.assertIn("expired", error)

    def test_replay_arm_rejects_blocking_workflow_quality(self):
        workflow = Workflow(
            workflow_id="wf-chatgpt",
            workflow_name="wf",
            workflow_title="Workflow",
            description="Translate in ChatGPT.",
            task_description="Open ACM TechNews, then translate it with ChatGPT.",
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="prompt",
                    action="Type the translation prompt in ChatGPT",
                    action_type="type",
                    value="translate",
                    active_app_name="Google Chrome",
                )
            ],
        )

        class FakeStorage:
            def load(self, workflow_id):
                return workflow, {}

        old_read_json = server._read_json
        old_storage = server._storage
        server._read_json = lambda handler: {"workflow_id": "wf-chatgpt"}
        server._storage = lambda: FakeStorage()
        server._mark_client_seen("client-1")
        try:
            handler = DummyHandler()
            server._arm_replay(handler)
        finally:
            server._read_json = old_read_json
            server._storage = old_storage

        self.assertEqual(handler.status, 422)
        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["quality"]["runnable"])
        self.assertIn(
            "missing_chatgpt_navigation",
            {item["code"] for item in payload["quality"]["issues"]},
        )

    def test_visual_warmup_loads_parser_components(self):
        from gpa.core import ui_parser

        calls = []
        originals = {
            "_load_yolo": ui_parser._load_yolo,
            "_load_ocr": ui_parser._load_ocr,
            "_load_clip": ui_parser._load_clip,
            "_load_e5": ui_parser._load_e5,
        }
        ui_parser._load_yolo = lambda: calls.append("yolo")
        ui_parser._load_ocr = lambda: calls.append("ocr")
        ui_parser._load_clip = lambda: calls.append("clip")
        ui_parser._load_e5 = lambda: calls.append("e5")
        try:
            server._warm_visual_models()
        finally:
            for name, value in originals.items():
                setattr(ui_parser, name, value)

        self.assertEqual(calls, ["yolo", "ocr", "clip", "e5"])
        warmup = server._visual_warmup_payload()
        self.assertEqual(warmup["status"], "ready")
        self.assertEqual(warmup["loaded"], calls)
        self.assertEqual(warmup["errors"], [])

    def test_run_with_timeout_raises_for_slow_component(self):
        with self.assertRaisesRegex(TimeoutError, "slow warmup exceeded"):
            server._run_with_timeout(lambda: time.sleep(0.05), timeout_seconds=0.001, label="slow")

    def test_visual_warmup_gate_rejects_partial_startup(self):
        old_preload = server.PRELOAD_VISUAL_MODELS_ENABLED
        old_require = server.REQUIRE_VISUAL_WARMUP_READY
        old_warm = server._warm_visual_models
        try:
            server.PRELOAD_VISUAL_MODELS_ENABLED = True
            server.REQUIRE_VISUAL_WARMUP_READY = True

            def fake_warmup():
                server._set_visual_warmup_status(
                    "partial",
                    loaded=["yolo"],
                    errors=[{"component": "e5", "error": "timeout"}],
                )

            server._warm_visual_models = fake_warmup
            with self.assertRaisesRegex(RuntimeError, "refusing to start server"):
                server._ensure_visual_warmup_ready()
        finally:
            server.PRELOAD_VISUAL_MODELS_ENABLED = old_preload
            server.REQUIRE_VISUAL_WARMUP_READY = old_require
            server._warm_visual_models = old_warm

    def test_visual_warmup_gate_accepts_ready_startup(self):
        old_preload = server.PRELOAD_VISUAL_MODELS_ENABLED
        old_require = server.REQUIRE_VISUAL_WARMUP_READY
        old_warm = server._warm_visual_models
        try:
            server.PRELOAD_VISUAL_MODELS_ENABLED = True
            server.REQUIRE_VISUAL_WARMUP_READY = True

            def fake_warmup():
                server._set_visual_warmup_status("ready", loaded=["yolo", "ocr", "clip", "e5"], errors=[])

            server._warm_visual_models = fake_warmup
            server._ensure_visual_warmup_ready()
        finally:
            server.PRELOAD_VISUAL_MODELS_ENABLED = old_preload
            server.REQUIRE_VISUAL_WARMUP_READY = old_require
            server._warm_visual_models = old_warm

    def test_input_monitoring_listener_success_reports_ready(self):
        old_platform = server.sys.platform
        old_pynput = sys.modules.get("pynput")
        old_pynput_keyboard = sys.modules.get("pynput.keyboard")

        class FakeListener:
            def __init__(self, on_press=None):
                self.on_press = on_press

            def start(self):
                return None

            def stop(self):
                return None

        fake_keyboard = types.SimpleNamespace(Listener=FakeListener)
        fake_pynput = types.SimpleNamespace(keyboard=fake_keyboard)
        sys.modules["pynput"] = fake_pynput
        sys.modules["pynput.keyboard"] = fake_keyboard
        server.sys.platform = "darwin"
        try:
            item = server._check_input_monitoring_permission()
        finally:
            server.sys.platform = old_platform
            if old_pynput is None:
                sys.modules.pop("pynput", None)
            else:
                sys.modules["pynput"] = old_pynput
            if old_pynput_keyboard is None:
                sys.modules.pop("pynput.keyboard", None)
            else:
                sys.modules["pynput.keyboard"] = old_pynput_keyboard

        self.assertTrue(item["ready"])
        self.assertEqual(item["status"], "ready")

    def test_status_health_checks_are_cached(self):
        calls = []
        old_health = server._dependency_health
        server._dependency_health = lambda: calls.append("health") or {"ok": True}
        try:
            first = server._cached_dependency_health()
            second = server._cached_dependency_health()
        finally:
            server._dependency_health = old_health

        self.assertEqual(first, {"ok": True})
        self.assertEqual(second, {"ok": True})
        self.assertEqual(calls, ["health"])

    def test_workflow_editor_payload_preserves_step_metadata(self):
        workflow = Workflow(
            workflow_id="wf-1",
            workflow_name="wf",
            workflow_title="Workflow",
            description="",
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="step-1",
                    action="Copy selected text",
                    action_type="hotkey",
                    value="cmd+c",
                    active_app_name="Google Chrome",
                    metadata={
                        "recorded_clipboard_text": "selected text",
                        "selection_drag_start": [10, 20],
                    },
                )
            ],
        )

        payload = {
            "name": "wf",
            "title": "Workflow",
            "description": "",
            "task_description": "",
            "variables": [],
            "steps": [
                {
                    "id": "step-1",
                    "action": "Copy selected article text",
                    "action_type": "hotkey",
                    "value": "cmd+c",
                    "pause_duration": 0.1,
                    "active_app_name": "Google Chrome",
                }
            ],
        }

        updated, _ = server._apply_workflow_payload(workflow, {}, payload)

        self.assertEqual(updated.steps[0].action, "Copy selected article text")
        self.assertEqual(
            updated.steps[0].metadata["recorded_clipboard_text"],
            "selected text",
        )
        self.assertEqual(updated.steps[0].metadata["selection_drag_start"], [10, 20])

    def test_workflow_payload_flags_codex_target_as_blocking(self):
        workflow = Workflow(
            workflow_id="wf-codex",
            workflow_name="wf",
            workflow_title="Workflow",
            description="Open a browser page.",
            task_description="Open ACM TechNews and copy page content.",
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="step-codex",
                    action="Click the loaded ACM TechNews page to focus it",
                    action_type="click",
                    active_app_name="Codex",
                )
            ],
        )

        payload = server._workflow_payload(workflow, {})

        self.assertFalse(payload["quality"]["runnable"])
        self.assertEqual(payload["quality"]["blocking_count"], 2)
        self.assertIn("targets_console", {item["code"] for item in payload["quality"]["issues"]})

    def test_workflow_payload_blocks_chatgpt_steps_without_navigation(self):
        workflow = Workflow(
            workflow_id="wf-chatgpt",
            workflow_name="wf",
            workflow_title="Workflow",
            description="Translate in ChatGPT.",
            task_description="Open ACM TechNews, then translate it with ChatGPT.",
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="prompt",
                    action="Type the translation prompt in ChatGPT",
                    action_type="type",
                    value="translate",
                    active_app_name="Google Chrome",
                )
            ],
        )

        payload = server._workflow_payload(workflow, {})

        self.assertFalse(payload["quality"]["runnable"])
        self.assertIn(
            "missing_chatgpt_navigation",
            {item["code"] for item in payload["quality"]["issues"]},
        )

    def test_workflow_payload_allows_chatgpt_url_variable_navigation(self):
        workflow = Workflow(
            workflow_id="wf-chatgpt-nav",
            workflow_name="wf",
            workflow_title="Workflow",
            description="Translate in ChatGPT.",
            task_description="Open ACM TechNews, then translate it with ChatGPT.",
            variables=[
                WorkflowVariable("chatgpt_url", "https://chatgpt.com", "ChatGPT URL"),
            ],
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="chatgpt-url",
                    action="Type the ChatGPT website URL",
                    action_type="type",
                    value="{{chatgpt_url}}",
                    active_app_name="Google Chrome",
                )
            ],
        )

        payload = server._workflow_payload(workflow, {})

        self.assertNotIn(
            "missing_chatgpt_navigation",
            {item["code"] for item in payload["quality"]["issues"]},
        )

    def test_workflow_payload_blocks_incomplete_wechat_delivery_goal(self):
        workflow = Workflow(
            workflow_id="wf-incomplete-wechat",
            workflow_name="wf",
            workflow_title="Workflow",
            description="Translate with ChatGPT and send to WeChat.",
            task_description="Open ChatGPT, translate the article, then send it to WeChat File Transfer Assistant.",
            variables=[WorkflowVariable("chatgpt_url", "https://chatgpt.com", "ChatGPT URL")],
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="chatgpt-url",
                    action="Type the ChatGPT website URL",
                    action_type="type",
                    value="{{chatgpt_url}}",
                    active_app_name="Google Chrome",
                ),
                WorkflowStep(
                    step_number=2,
                    id="wait-chatgpt",
                    action="Wait for ChatGPT to finish generating the Chinese translation",
                    action_type="scroll",
                    active_app_name="WeChat",
                ),
            ],
        )

        payload = server._workflow_payload(workflow, {})
        codes = {item["code"] for item in payload["quality"]["issues"]}

        self.assertFalse(payload["quality"]["runnable"])
        self.assertIn("missing_wechat_delivery", codes)
        self.assertIn("target_app_mismatch", codes)

    def test_run_history_persists_step_diagnostics(self):
        old_runs_dir = server.RUNS_DIR

        class FakeState:
            name = "DONE"

        class FakeStep:
            step_number = 1
            state = FakeState()
            retries = 2
            error = ""
            duration_seconds = 3.4
            agent_decision_ms = 120.5
            agent_decision = {"action_type": "click"}
            corrections = [{"action_type": "click"}]
            observation_metrics = [{"phase": "readiness_retry", "parse_ms": 42.0}]
            localization = None

        class FakeResult:
            step_results = [FakeStep()]

            @property
            def n_steps(self):
                return 1

            @property
            def n_failed(self):
                return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            server.RUNS_DIR = pathlib.Path(temp_dir)
            try:
                path = server._save_run_history(
                    "workflow-1",
                    "run-1",
                    {
                        "status": "succeeded",
                        "success": True,
                        "started_at": "start",
                        "finished_at": "finish",
                        "elapsed_seconds": 4,
                        "error": "",
                    },
                    FakeResult(),
                )
                payload = json.loads(path.read_text())
            finally:
                server.RUNS_DIR = old_runs_dir

        step = payload["steps"][0]
        self.assertEqual(step["agent_decision_ms"], 120.5)
        self.assertEqual(step["corrections"], [{"action_type": "click"}])
        self.assertEqual(step["observation_metrics"][0]["phase"], "readiness_retry")

    def test_workflow_editor_payload_exposes_metadata_and_supports_drag_coordinates(self):
        workflow = Workflow(
            workflow_id="wf-1",
            workflow_name="wf",
            workflow_title="Workflow",
            description="",
            steps=[
                WorkflowStep(
                    step_number=1,
                    id="drag-1",
                    action="Select text",
                    action_type="drag",
                    metadata={"drag_start": [10, 20], "drag_end": [30, 40]},
                )
            ],
        )

        payload = server._workflow_payload(workflow, {})
        self.assertEqual(payload["steps"][0]["metadata"]["drag_end"], [30, 40])

        updated, subgraphs = server._apply_workflow_payload(
            workflow,
            {},
            {
                "name": "wf",
                "title": "Workflow",
                "description": "",
                "variables": [],
                "steps": [
                    {
                        "id": "drag-1",
                        "action": "Select text",
                        "action_type": "drag",
                        "click_coordinates": [15, 25],
                    }
                ],
            },
        )

        self.assertEqual(updated.steps[0].metadata["drag_start"], [10, 20])
        self.assertIn("drag-1", subgraphs)


if __name__ == "__main__":
    unittest.main()
