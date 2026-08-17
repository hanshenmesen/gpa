import unittest

from gpa.execution.decision_policy import (
    FinalStateKind,
    StepDecisionKind,
    classify_final_state,
    classify_step_decision,
    decision_requests_correction,
)


class DecisionPolicyTests(unittest.TestCase):
    def test_executes_an_ordinary_agent_action(self):
        self.assertIs(
            classify_step_decision({"should_execute": True, "action_type": "click"}),
            StepDecisionKind.EXECUTE,
        )

    def test_stop_has_precedence_over_conflicting_correction(self):
        self.assertIs(
            classify_step_decision({
                "action_type": "stop",
                "requires_correction": True,
                "correction_action_type": "click",
            }),
            StepDecisionKind.STOP,
        )

    def test_actionable_correction_has_precedence_over_execute(self):
        self.assertIs(
            classify_step_decision({
                "should_execute": True,
                "action_type": "type",
                "requires_correction": True,
                "correction_action_type": "hotkey",
            }),
            StepDecisionKind.CORRECT,
        )

    def test_non_actionable_correction_does_not_loop(self):
        decision = {
            "should_execute": True,
            "action_type": "type",
            "requires_correction": True,
            "correction_action_type": "none",
        }
        self.assertFalse(decision_requests_correction(decision))
        self.assertIs(classify_step_decision(decision), StepDecisionKind.EXECUTE)

    def test_explicit_idempotent_skip_succeeds(self):
        self.assertIs(
            classify_step_decision({
                "should_execute": False,
                "action_type": "skip",
                "skip_reason": "already_done",
            }),
            StepDecisionKind.SUCCEED,
        )

    def test_explained_redundant_skip_succeeds(self):
        self.assertIs(
            classify_step_decision({
                "action_type": "skip",
                "reason": "This navigation is redundant.",
            }),
            StepDecisionKind.SUCCEED,
        )

    def test_unexplained_decline_fails_closed(self):
        self.assertIs(
            classify_step_decision({
                "should_execute": False,
                "action_type": "skip",
                "reason": "I am uncertain.",
            }),
            StepDecisionKind.FAIL,
        )

    def test_final_state_complete_has_precedence(self):
        self.assertIs(
            classify_final_state({"complete": True, "reason": "Still saving"}),
            FinalStateKind.COMPLETE,
        )

    def test_transient_final_state_waits(self):
        self.assertIs(
            classify_final_state({"complete": False, "reason": "保存中，请稍候"}),
            FinalStateKind.WAIT,
        )

    def test_actionable_final_state_correction_repairs(self):
        self.assertIs(
            classify_final_state({
                "complete": False,
                "requires_correction": True,
                "correction_action_type": "click",
            }),
            FinalStateKind.CORRECT,
        )

    def test_non_actionable_final_state_correction_fails(self):
        self.assertIs(
            classify_final_state({
                "complete": False,
                "requires_correction": True,
                "correction_action_type": "none",
            }),
            FinalStateKind.FAIL,
        )


if __name__ == "__main__":
    unittest.main()
