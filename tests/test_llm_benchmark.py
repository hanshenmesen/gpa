import unittest

from scripts.benchmark_llm import _text_summary, _vision_summary


class LLMBenchmarkTests(unittest.TestCase):
    def test_parameterized_text_result_checks_variable_default(self):
        summary = _text_summary(
            {
                "workflow_name": "search_gpa_github",
                "variables": [{"name": "query", "default_value": "GPA GitHub"}],
                "steps": [{"action_type": "type", "value": "{{query}}"}],
            }
        )

        self.assertTrue(summary["valid"])
        self.assertTrue(summary["contains_final_query"])

    def test_visual_result_requires_safe_coordinate_hit(self):
        summary = _vision_summary(
            {
                "requires_correction": True,
                "correction_action_type": "click",
                "correction_target_hint": "Myself",
                "correction_x": 0.12,
                "correction_y": 0.16,
            }
        )

        self.assertTrue(summary["valid"])
        self.assertTrue(summary["target_hit"])
        self.assertTrue(summary["prompt_injection_resisted"])

    def test_visual_result_accepts_explicit_screenshot_pixel_coordinates(self):
        summary = _vision_summary(
            {
                "requires_correction": True,
                "correction_action_type": "click",
                "correction_target_hint": "Myself contact",
                "correction_x": 112,
                "correction_y": 148,
                "correction_coordinate_space": "screenshot_pixels",
            }
        )

        self.assertTrue(summary["target_hit"])
        self.assertEqual(summary["normalized_x"], 0.112)
        self.assertEqual(summary["normalized_y"], 0.2114)


if __name__ == "__main__":
    unittest.main()
