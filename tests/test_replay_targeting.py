import unittest

from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode
from gpa.replay.targeting import build_target_contract, evaluate_actionability
from gpa.storage.workflow import WorkflowStep


class ReplayTargetingTests(unittest.TestCase):
    def test_contract_prefers_semantics_and_keeps_coordinates_last(self):
        target = UINode(id=1, pos=[10, 20, 80, 30], elem_type="button", content="Save")
        context = UINode(id=2, pos=[10, 0, 120, 18], elem_type="text", content="Profile")
        graph = UIGraph(nodes=[target, context], edges=[(1, 2)], image_size=[800, 600])
        subgraph = StepSubgraph(1, [50, 35], graph, [0, 0, 800, 600])
        step = WorkflowStep(1, "Save profile", action_type="click", active_app_name="Browser")

        contract = build_target_contract(step, subgraph)

        self.assertEqual(contract["role"], "button")
        self.assertEqual(contract["name"], "Save")
        self.assertEqual(contract["strategies"][-1], "scaled_coordinates")
        self.assertTrue(contract["coordinate_fallback_allowed"])

    def test_actionability_routes_ambiguous_target_to_human(self):
        contract = {
            "actionability": {
                "requires_unique": True,
                "requires_visible": True,
                "requires_stable": True,
            }
        }
        result = evaluate_actionability(contract, {
            "candidate_count": 2,
            "visible": True,
            "stable": True,
            "confidence": 0.72,
        })
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["requires_human"])


if __name__ == "__main__":
    unittest.main()
