import json
import unittest
import zipfile
from io import BytesIO

from gpa.diagnostics import diagnostic_report, redact, support_bundle


class DiagnosticTests(unittest.TestCase):
    def test_recursive_redaction_removes_secrets_and_home_names(self):
        value = redact({
            "api_key": "sk-super-secret",
            "message": "Bearer sk_example_secret_value at /Users/alice/project",
            "nested": [{"cookie": "session"}],
        })
        rendered = json.dumps(value)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("alice", rendered)
        self.assertNotIn("session", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_bundle_has_only_redacted_report_and_readme(self):
        report = diagnostic_report(
            dependency_health={"ok": True},
            runtime={"desktop": {"enabled": False}},
            crash={"incident_status": "none"},
            recent_runs=[{"run_id": "run_1", "authorization_token": "secret"}],
            workflow_count=3,
            cloud={"status": "active", "device_token": "secret"},
        )
        data = support_bundle(report)
        with zipfile.ZipFile(BytesIO(data)) as archive:
            self.assertEqual(set(archive.namelist()), {"gpa-diagnostics.json", "README.txt"})
            content = archive.read("gpa-diagnostics.json").decode()
        self.assertNotIn("device_token", content)
        self.assertNotIn("authorization_token", content)
        self.assertIn('"recordings_included": false', content)
        self.assertIn('"credentials_included": false', content)


if __name__ == "__main__":
    unittest.main()
