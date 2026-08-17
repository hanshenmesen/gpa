import unittest

from gpa.execution.executor import StepResult, StepState
from gpa.replay.trace import build_run_trace
from gpa.storage.workflow import Workflow, WorkflowStep


class ReplayTraceTests(unittest.TestCase):
    def test_failed_low_confidence_target_creates_intervention(self):
        step = WorkflowStep(
            1, "Click Save", id="save", action_type="click",
            metadata={"target_contract": {"name": "Save"}},
        )
        result = StepResult(1, StepState.FAILED, error="Multiple matching targets", agent_decision={"confidence": 0.4})
        trace = build_run_trace(Workflow("w", "w", "W", "", steps=[step]), [result])
        self.assertEqual(trace["failed_steps"], 1)
        self.assertEqual(trace["interventions"][0]["kind"], "choose_target")


if __name__ == "__main__":
    unittest.main()
