import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import demo_web.server as server


class ServerRecoveryTests(unittest.TestCase):
    def test_unclean_dead_session_is_detected_but_live_session_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "server_session.json"
            with patch.object(server, "SERVER_SESSION_FILE", marker):
                marker.write_text(json.dumps({"status": "running", "pid": 999_999_999}))
                self.assertTrue(server._previous_session_was_unclean())

                marker.write_text(json.dumps({"status": "running", "pid": os.getpid()}))
                self.assertFalse(server._previous_session_was_unclean())

                marker.write_text(json.dumps({"status": "stopped", "pid": 999_999_999}))
                self.assertFalse(server._previous_session_was_unclean())

    def test_server_session_marker_is_atomic_and_records_safety_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "server_session.json"
            with patch.object(server, "SERVER_SESSION_FILE", marker):
                server._mark_server_session("running")

            payload = json.loads(marker.read_text())
            self.assertEqual(payload["schema"], "gpa.server-session/v1")
            self.assertEqual(payload["status"], "running")
            self.assertEqual(payload["pid"], os.getpid())
            self.assertIn("desktop_automation_enabled", payload)

    def test_crash_diagnostics_correlates_report_with_unclean_previous_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "Python-test.ips"
            report.write_text("TISCopyCurrentKeyboardInputSource", encoding="utf-8")
            marker_time = time.time() - 5
            os.utime(report, (time.time(), time.time()))
            previous = {
                "status": "running",
                "pid": 999_999_999,
                "updated_at": datetime_from_timestamp(marker_time),
            }
            with patch.object(server, "PREVIOUS_SERVER_SESSION", previous), patch.object(
                server, "PREVIOUS_SESSION_UNCLEAN", True
            ), patch.object(server, "PYTHON_CRASH_REPORTS_AT_START", frozenset({report.name})), patch.object(
                server, "_python_crash_report_paths", return_value=[report]
            ), patch.object(server, "_effective_recording_input_backend", return_value="quartz"):
                result = server._python_crash_diagnostics()

        self.assertEqual(result["incident_status"], "recovered")
        self.assertTrue(result["previous_session_crash_suspected"])
        self.assertEqual(result["reports_during_previous_session"], 1)
        self.assertTrue(result["known_signature_mitigated"])


def datetime_from_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(value))


if __name__ == "__main__":
    unittest.main()
