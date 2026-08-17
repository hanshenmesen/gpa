import unittest

from gpa.replay.health import assert_share_safe, build_replay_health, sensitive_findings
from gpa.storage.workflow import Workflow, WorkflowStep


class ReplayHealthTests(unittest.TestCase):
    def test_health_reports_missing_outcome_and_target_contract(self):
        workflow = Workflow(
            "health", "health", "Health", "Check health",
            environment={"system": {"name": "darwin"}, "runtime": {"python": "3.13"}},
            understanding={"success_criteria": []},
            steps=[WorkflowStep(1, "Click", action_type="click")],
        )
        health = build_replay_health(workflow)
        self.assertIn("success_criteria", health["blockers"])
        self.assertIn("semantic_targets", health["blockers"])

    def test_secret_material_is_blocked_but_reference_name_is_allowed(self):
        self.assertEqual(sensitive_findings({"credential_reference": "vault://mail"}), [])
        workflow = Workflow(
            "secret", "secret", "Secret", "Unsafe",
            artifacts={"session_cookie": "abc123"},
        )
        with self.assertRaisesRegex(ValueError, "credential or session"):
            assert_share_safe(workflow)


if __name__ == "__main__":
    unittest.main()
