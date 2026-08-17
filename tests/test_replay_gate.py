import unittest

from gpa.replay.gate import build_reproduction_gate
from gpa.storage.workflow import Workflow, WorkflowStep


def workflow_with(environment):
    return Workflow(
        workflow_id="gate-test",
        workflow_name="gate-test",
        workflow_title="Gate test",
        description="Test reproduction policy.",
        environment=environment,
        steps=[WorkflowStep(1, "Wait", action_type="wait", value="1")],
    )


class ReplayGateTests(unittest.TestCase):
    def test_missing_environment_blocks_desktop_but_not_safe_web(self):
        desktop = build_reproduction_gate(
            workflow_with({}),
            quality={"runnable": True, "score": 100},
            current_environment={"system": {"name": "darwin"}},
            safe_web={"runnable": False},
        )
        safe_web = build_reproduction_gate(
            workflow_with({}),
            quality={"runnable": True, "score": 100},
            current_environment={"system": {"name": "darwin"}},
            safe_web={"runnable": True},
        )

        self.assertFalse(desktop["can_execute"])
        self.assertEqual(desktop["status"], "blocked")
        self.assertEqual(desktop["blockers"][0]["code"], "environment_replan_required")
        self.assertTrue(safe_web["can_execute"])
        self.assertEqual(safe_web["execution_mode"], "safe_web")

    def test_decision_id_changes_when_target_environment_changes(self):
        workflow = workflow_with({"system": {"name": "darwin"}})
        first = build_reproduction_gate(
            workflow,
            quality={"runnable": True, "score": 80},
            current_environment={"system": {"name": "darwin"}},
            safe_web={"runnable": False},
        )
        second = build_reproduction_gate(
            workflow,
            quality={"runnable": True, "score": 80},
            current_environment={"system": {"name": "windows"}},
            safe_web={"runnable": False},
        )

        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertTrue(first["can_execute"])
        self.assertFalse(second["can_execute"])


if __name__ == "__main__":
    unittest.main()
