import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gpa.execution import worker as replay_worker
from gpa.recording import worker as recorder_worker
from gpa.replay import audit_worker


class FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def recv(self):
        if not self.messages:
            raise EOFError
        return self.messages.pop(0)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


class FakeRecorder:
    def __init__(self, *, input_backend):
        self._input_capture_backend = input_backend
        self._recording = SimpleNamespace(events=[])
        self.stop_calls = 0

    def start(self):
        return None

    def stop(self):
        self.stop_calls += 1
        return self._recording

    def append_external_event(self, **payload):
        if not payload.get("event_type"):
            raise ValueError("event_type is required")
        event = SimpleNamespace(
            event_type=payload["event_type"],
            value=str(payload.get("value") or ""),
            active_app=str(payload.get("active_app") or ""),
            metadata={"input_source": "accessibility_automation"},
        )
        self._recording.events.append(event)
        return event


class WorkerProtocolTests(unittest.TestCase):
    def test_recorder_worker_reports_validation_unknown_and_stop_events(self):
        connection = FakeConnection([
            "not-an-object",
            {"command": "status"},
            {"command": "append_external_event", "payload": {}},
            {"command": "unknown"},
            {
                "command": "append_external_event",
                "payload": {"event_type": "hotkey", "value": "cmd+f", "active_app": "Chrome"},
            },
            {"command": "stop"},
        ])
        with patch("gpa.recording.recorder.Recorder", FakeRecorder), patch.object(
            recorder_worker, "_write_recording_snapshot"
        ) as write_snapshot:
            recorder_worker.recorder_worker_main(connection, "/tmp/result.pkl", "quartz")

        self.assertEqual(connection.sent[0]["event"], "started")
        self.assertEqual(connection.sent[1]["error"], "Recorder command must be an object.")
        self.assertEqual(connection.sent[2]["event_count"], 0)
        self.assertEqual(connection.sent[3]["error_type"], "validation")
        self.assertIn("Unknown recorder command", connection.sent[4]["error"])
        self.assertEqual(connection.sent[5]["recorded_event"]["value"], "cmd+f")
        self.assertEqual(connection.sent[6], {"ok": True, "event": "stopped", "event_count": 1})
        self.assertTrue(connection.closed)
        write_snapshot.assert_called_once()

    def test_recorder_worker_turns_start_failure_into_worker_error(self):
        class BrokenRecorder(FakeRecorder):
            def start(self):
                raise RuntimeError("native backend failed")

        connection = FakeConnection([])
        with patch("gpa.recording.recorder.Recorder", BrokenRecorder):
            recorder_worker.recorder_worker_main(connection, "/tmp/result.pkl", "quartz")

        self.assertEqual(connection.sent[0]["event"], "worker_error")
        self.assertIn("native backend failed", connection.sent[0]["error"])
        self.assertTrue(connection.closed)

    def test_desktop_worker_serializes_full_step_contract(self):
        localization = SimpleNamespace(x=10, y=20, confidence=0.9, method="semantic")
        step = SimpleNamespace(
            step_number=2,
            state=SimpleNamespace(name="DONE"),
            retries=1,
            error="",
            duration_seconds=0.4,
            agent_decision_ms=12.3,
            agent_decision={"action_type": "click"},
            corrections=[{"action_type": "wait"}],
            observation_metrics=[{"phase": "agent_observe"}],
            postcondition_verified=True,
            postcondition_reason="Saved",
            postcondition_attempts=2,
            evidence_source="accessibility",
            localization=localization,
        )
        result = SimpleNamespace(
            success=True,
            error="",
            n_steps=1,
            n_failed=0,
            llm_metrics=[{"model": "test"}],
            step_results=[step],
        )

        payload = replay_worker._serialize_result(result)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["steps"][0]["state"], "done")
        self.assertEqual(payload["steps"][0]["localization"]["method"], "semantic")
        self.assertTrue(payload["steps"][0]["postcondition_verified"])

    def test_desktop_worker_main_emits_structured_crash_for_bad_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            request = Path(temporary) / "request.json"
            request.write_text(json.dumps({"schema": "unsupported"}), encoding="utf-8")
            output = io.StringIO()
            with patch("sys.stdout", output):
                exit_code = replay_worker.main(["--request", str(request)])

        event = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(event["schema"], replay_worker.SCHEMA)
        self.assertEqual(event["event"], "crash")
        self.assertIn("Unsupported", event["error"])

    def test_audit_worker_preserves_explicit_empty_environment(self):
        report = {"schema": "gpa.isolated-reproduction-audit/v1", "status": "passed"}
        stdin = io.StringIO(json.dumps({"target_environment": {}}))
        stdout = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch.object(
            audit_worker, "audit_reproduction_package", return_value=report
        ) as audit:
            exit_code = audit_worker.main(["--package", "/tmp/fixture.gpa-record.zip"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual(audit.call_args.kwargs["target_environment"], {})
        self.assertTrue(audit.call_args.kwargs["execute"])

    def test_audit_worker_rejects_falsey_non_object_environment(self):
        for invalid in ([], "", 0, False):
            with self.subTest(invalid=invalid), patch(
                "sys.stdin", io.StringIO(json.dumps({"target_environment": invalid}))
            ):
                with self.assertRaisesRegex(TypeError, "must be an object"):
                    audit_worker.main(["--package", "/tmp/fixture.gpa-record.zip"])


if __name__ == "__main__":
    unittest.main()
