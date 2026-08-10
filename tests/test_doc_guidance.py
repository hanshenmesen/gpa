import unittest

from gpa.core.doc_guidance import (
    build_document_search_queries,
    document_guidance_payload,
    documentation_context_from_variables,
    extract_documentation_hints,
)
from gpa.storage.workflow import Workflow, WorkflowStep


class DocGuidanceTests(unittest.TestCase):
    def test_extracts_header_aware_procedural_hints(self):
        text = """
        # Run/Debug Configuration
        Before running, open the project.
        1. Click the Run menu.
        2. Select Edit Configurations.
        - Enter the script path.
        """

        hints = extract_documentation_hints(text)

        self.assertEqual(hints[0].section, "Run/Debug Configuration")
        self.assertIn("Before running", hints[0].instruction)
        self.assertIn("Click the Run menu", hints[0].instruction)
        self.assertEqual(hints[1].instruction, "Select Edit Configurations.")
        self.assertEqual(hints[2].instruction, "Enter the script path.")

    def test_finds_documentation_context_variable_alias(self):
        name, value = documentation_context_from_variables({
            "ticket": "INC-1",
            "official_documentation": "1. Click Save.",
        })

        self.assertEqual(name, "official_documentation")
        self.assertEqual(value, "1. Click Save.")

    def test_builds_official_documentation_queries(self):
        queries = build_document_search_queries(
            "Open PyCharm and configure Run/Debug for pytest",
            app_name="Google Chrome",
        )

        self.assertIn("Chrome official documentation Open PyCharm configure Run Debug pytest", queries)
        self.assertTrue(any("official documentation" in item for item in queries))

    def test_document_guidance_payload_includes_hints_and_queries(self):
        workflow = Workflow(
            "wf-docs",
            "wf-docs",
            "PyCharm setup",
            "Configure a run target.",
            task_description="Configure PyCharm Run Debug for pytest.",
            steps=[WorkflowStep(1, "Open settings", active_app_name="Google Chrome")],
        )

        payload = document_guidance_payload(
            workflow,
            {"doc_context": "Steps:\n1. Open Run menu.\n2. Select Edit Configurations."},
            current_step=workflow.steps[0],
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["source_variable"], "doc_context")
        self.assertEqual(len(payload["hints"]), 2)
        self.assertTrue(payload["search_queries"])


if __name__ == "__main__":
    unittest.main()
