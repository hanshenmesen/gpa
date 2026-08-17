import os
import unittest
from pathlib import Path

import demo_web.server as server
from gpa.recording.recorder import RecordedEvent, Recording
from gpa.recording.worker import _write_recording_snapshot
from gpa.recording.worker_client import RecorderWorkerClient, RecorderWorkerError


def roundtrip_worker(connection, result_path, input_backend):
    events = []
    connection.send({
        "ok": True,
        "event": "started",
        "pid": os.getpid(),
        "input_backend": input_backend,
    })
    while True:
        message = connection.recv()
        if message["command"] == "status":
            connection.send({"ok": True, "event": "status", "event_count": len(events)})
        elif message["command"] == "append_external_event":
            payload = message["payload"]
            event = RecordedEvent(
                event_type=payload["event_type"],
                value=str(payload.get("value") or ""),
                active_app=str(payload.get("active_app") or ""),
                metadata={"input_source": "accessibility_automation"},
            )
            events.append(event)
            connection.send({
                "ok": True,
                "event": "external_event",
                "event_count": len(events),
                "recorded_event": {
                    "event_type": event.event_type,
                    "value": event.value,
                    "active_app": event.active_app,
                    "input_source": event.metadata["input_source"],
                },
            })
        elif message["command"] == "stop":
            _write_recording_snapshot(Path(result_path), Recording(events=events))
            connection.send({"ok": True, "event": "stopped", "event_count": len(events)})
            return
        elif message["command"] == "abort":
            connection.send({"ok": True, "event": "aborted"})
            return


def crashing_worker(connection, result_path, input_backend):
    connection.send({
        "ok": True,
        "event": "started",
        "pid": os.getpid(),
        "input_backend": input_backend,
    })
    connection.recv()
    os._exit(86)


def hanging_worker(connection, result_path, input_backend):
    connection.send({
        "ok": True,
        "event": "started",
        "pid": os.getpid(),
        "input_backend": input_backend,
    })
    connection.recv()
    import time
    time.sleep(10)


def mismatched_worker(connection, result_path, input_backend):
    connection.send({
        "ok": True,
        "event": "started",
        "pid": os.getpid(),
        "input_backend": input_backend,
    })
    connection.recv()
    connection.send({"ok": True, "event": "stopped", "event_count": 0})
    connection.recv()


class RecorderWorkerTests(unittest.TestCase):
    def test_client_rejects_unbounded_or_non_numeric_timeouts(self):
        for field, value in (
            ("start_timeout", float("nan")),
            ("start_timeout", float("inf")),
            ("command_timeout", 0),
            ("command_timeout", True),
        ):
            with self.subTest(field=field, value=value), self.assertRaises((TypeError, ValueError)):
                RecorderWorkerClient(input_backend="quartz", **{field: value})

    def test_worker_roundtrip_returns_recording_and_removes_private_snapshot(self):
        client = RecorderWorkerClient(
            input_backend="quartz",
            context_name="spawn",
            worker_target=roundtrip_worker,
        )
        client.start()
        worker_pid = client.worker_pid
        temporary_root = client._temporary_root

        event = client.append_external_event("hotkey", value="cmd+f", active_app="Chrome")
        self.assertEqual(client.refresh_event_count(), 1)
        recording = client.stop()

        self.assertNotEqual(worker_pid, os.getpid())
        self.assertEqual(client._context.get_start_method(), "spawn")
        self.assertEqual(event.event_type, "hotkey")
        self.assertEqual(event.metadata["input_source"], "accessibility_automation")
        self.assertEqual(len(recording.events), 1)
        self.assertEqual(recording.events[0].value, "cmd+f")
        self.assertFalse(temporary_root.exists())

    def test_invalid_request_timeout_does_not_desynchronize_worker(self):
        client = RecorderWorkerClient(
            input_backend="quartz",
            context_name="spawn",
            worker_target=roundtrip_worker,
        )
        client.start()
        try:
            with self.assertRaises(ValueError):
                client.refresh_event_count(timeout=float("nan"))
            self.assertEqual(client.refresh_event_count(), 0)
        finally:
            client.close()

    def test_native_worker_crash_does_not_terminate_parent(self):
        parent_pid = os.getpid()
        client = RecorderWorkerClient(
            input_backend="quartz",
            context_name="spawn",
            worker_target=crashing_worker,
            command_timeout=2,
        )
        client.start()
        worker_pid = client.worker_pid

        with self.assertRaisesRegex(RecorderWorkerError, "exited unexpectedly"):
            client.append_external_event("click", x=1, y=2)
        exit_code = client.worker_exit_code
        client.close()

        self.assertEqual(os.getpid(), parent_pid)
        self.assertNotEqual(worker_pid, parent_pid)
        self.assertEqual(exit_code, 86)

    def test_unresponsive_worker_is_terminated_without_desynchronizing_protocol(self):
        client = RecorderWorkerClient(
            input_backend="quartz",
            context_name="spawn",
            worker_target=hanging_worker,
            command_timeout=0.1,
        )
        client.start()

        with self.assertRaisesRegex(RecorderWorkerError, "became unresponsive"):
            client.append_external_event("click", x=1, y=2)

        self.assertFalse(client.is_alive())
        client.close()

    def test_protocol_mismatch_terminates_worker(self):
        client = RecorderWorkerClient(
            input_backend="quartz",
            context_name="spawn",
            worker_target=mismatched_worker,
        )
        client.start()

        with self.assertRaisesRegex(RecorderWorkerError, "protocol mismatch"):
            client.refresh_event_count()

        self.assertFalse(client.is_alive())
        client.close()

    def test_event_count_must_be_non_negative_integer(self):
        for invalid in (True, -1, "many"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                RecorderWorkerError, "invalid event count"
            ):
                from gpa.recording.worker_client import _event_count

                _event_count({"event_count": invalid})

    def test_server_marks_worker_failure_without_losing_console_state(self):
        class DeadWorker:
            worker_exit_code = -6

            def is_alive(self):
                return False

            def failure_reason(self):
                return "Recorder worker exited unexpectedly with code -6."

            def close(self):
                pass

        with server.STATE_LOCK:
            previous_recorder = server.STATE["recorder"]
            previous_recording = dict(server.STATE["recording"])
            server.STATE["recorder"] = DeadWorker()
            server._set_recording_status("recording")
        try:
            server._refresh_recording_runtime()
            with server.STATE_LOCK:
                self.assertIsNone(server.STATE["recorder"])
                self.assertEqual(server.STATE["recording"]["status"], "failed")
                self.assertEqual(server.STATE["recording"]["worker_exit_code"], -6)
        finally:
            with server.STATE_LOCK:
                server.STATE["recorder"] = previous_recorder
                server.STATE["recording"] = previous_recording


if __name__ == "__main__":
    unittest.main()
