import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import demo_web.server as server
from gpa.execution import worker


class FakeProcess:
    def __init__(self, events, return_code=0):
        self.pid = 4242
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.stderr = io.StringIO("")
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class DesktopReplayWorkerTests(unittest.TestCase):
    def tearDown(self):
        with server.STATE_LOCK:
            server.STATE["run_process"] = None
            server.STATE["run_control_dir"] = None
            server.STATE["run"] = {"active": False, "status": "idle"}

    def test_worker_environment_is_explicit_and_does_not_copy_unrelated_secrets(self):
        with patch.dict(os.environ, {
            "GPA_LLM_API_KEY": "allowed-key",
            "OPENAI_API_KEY": "must-not-leak",
            "UNRELATED_SECRET": "must-not-leak",
        }, clear=False):
            environment = server._desktop_replay_worker_environment()

        self.assertEqual(environment["GPA_LLM_API_KEY"], "allowed-key")
        self.assertEqual(environment["GPA_ENABLE_DESKTOP_AUTOMATION"], "1")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("UNRELATED_SECRET", environment)

    def test_supervisor_consumes_events_and_clears_worker_state(self):
        events = [
            {"schema": worker.SCHEMA, "event": "ready", "total_steps": 2},
            {"schema": worker.SCHEMA, "event": "step_start", "step": {
                "number": 1, "action": "Open page", "action_type": "open_url",
            }},
            {"schema": worker.SCHEMA, "event": "result", "result": {
                "success": True, "error": "", "n_steps": 2, "n_failed": 0,
                "llm_metrics": [], "steps": [],
            }},
        ]
        process = FakeProcess(events)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "STORAGE_DIR", Path(temporary)), \
                patch.object(server.subprocess, "Popen", return_value=process):
            result = server._run_desktop_replay_process(
                "workflow-1", {}, 0.6, 2, threading.Event(), False,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["n_steps"], 2)
        self.assertIsNone(server.STATE["run_process"])
        self.assertIsNone(server.STATE["run_control_dir"])
        self.assertEqual(server.STATE["run"]["worker_exit_code"], 0)

    def test_supervisor_turns_worker_crash_into_controlled_error(self):
        process = FakeProcess([
            {"schema": worker.SCHEMA, "event": "crash", "error": "simulated native crash"},
        ], return_code=77)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "STORAGE_DIR", Path(temporary)), \
                patch.object(server.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "simulated native crash"):
                server._run_desktop_replay_process(
                    "workflow-1", {}, 0.6, 2, threading.Event(), False,
                )

        self.assertIsNone(server.STATE["run_process"])

    def test_supervisor_rejects_result_followed_by_nonzero_exit(self):
        process = FakeProcess([
            {"schema": worker.SCHEMA, "event": "ready", "total_steps": 1},
            {"schema": worker.SCHEMA, "event": "result", "result": {
                "success": True, "error": "", "n_steps": 1, "n_failed": 0,
                "llm_metrics": [], "steps": [],
            }},
        ], return_code=70)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "STORAGE_DIR", Path(temporary)), \
                patch.object(server.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "code 70"):
                server._run_desktop_replay_process(
                    "workflow-1", {}, 0.6, 2, threading.Event(), False,
                )

        self.assertEqual(server.STATE["run"]["worker_exit_code"], 70)

    def test_supervisor_rejects_crash_after_result_even_with_zero_exit(self):
        process = FakeProcess([
            {"schema": worker.SCHEMA, "event": "ready", "total_steps": 1},
            {"schema": worker.SCHEMA, "event": "result", "result": {
                "success": True, "error": "", "n_steps": 1, "n_failed": 0,
                "llm_metrics": [], "steps": [],
            }},
            {"schema": worker.SCHEMA, "event": "crash", "error": "late crash"},
        ])
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "STORAGE_DIR", Path(temporary)), \
                patch.object(server.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "crashed after"):
                server._run_desktop_replay_process(
                    "workflow-1", {}, 0.6, 2, threading.Event(), False,
                )

    def test_supervisor_rejects_result_before_ready(self):
        process = FakeProcess([
            {"schema": worker.SCHEMA, "event": "result", "result": {
                "success": True, "error": "", "n_steps": 0, "n_failed": 0,
                "llm_metrics": [], "steps": [],
            }},
        ])
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "STORAGE_DIR", Path(temporary)), \
                patch.object(server.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "out of order"):
                server._run_desktop_replay_process(
                    "workflow-1", {}, 0.6, 2, threading.Event(), False,
                )

    def test_supervisor_rejects_malformed_worker_result(self):
        events = [
            {"schema": worker.SCHEMA, "event": "ready", "total_steps": 1},
            {"schema": worker.SCHEMA, "event": "result", "result": {
                "success": "yes", "error": "", "n_steps": 1, "n_failed": 0,
                "llm_metrics": [], "steps": [],
            }},
        ]
        process = FakeProcess(events)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server, "STORAGE_DIR", Path(temporary)), \
                patch.object(server.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "invalid success"):
                server._run_desktop_replay_process(
                    "workflow-1", {}, 0.6, 2, threading.Event(), False,
                )


if __name__ == "__main__":
    unittest.main()
