import unittest
from unittest.mock import patch

from gpa.replay.evidence import merge_recorded_environment, prepare_workflow_evidence
from gpa.storage.workflow import Workflow, WorkflowStep


class ReplayEvidenceTests(unittest.TestCase):
    @patch("gpa.replay.evidence.capture_environment")
    def test_client_enrichment_fills_gaps_without_overwriting_recorded_host(self, capture):
        capture.return_value = {
            "system": {"name": "linux", "machine": "x86_64"},
            "browser": {"family": "Chrome", "viewport_width": 1280},
            "screen": {"width": 1512, "height": 982},
            "locale": {"language": "zh-CN", "timezone": "Asia/Shanghai"},
        }
        recorded = {
            "schema": "gpa.environment/v1",
            "system": {"name": "darwin", "machine": "arm64"},
            "browser": {"family": "Safari"},
            "screen": {"width": 1728, "height": 1117},
        }

        result = merge_recorded_environment(recorded, {"browser": {"family": "Chrome"}})

        self.assertEqual(result["system"]["name"], "darwin")
        self.assertEqual(result["browser"]["family"], "Safari")
        self.assertEqual(result["browser"]["viewport_width"], 1280)
        self.assertEqual(result["screen"]["width"], 1728)
        self.assertEqual(result["locale"]["language"], "zh-CN")
        self.assertIn("browser.viewport_width", result["client_enriched_fields"])
        self.assertNotIn("host_enriched_fields", result)

    @patch("gpa.replay.evidence.capture_environment")
    def test_new_environment_records_evidence_sources_and_original_timestamp(self, capture):
        capture.return_value = {
            "schema": "gpa.environment/v1",
            "captured_at": "new",
            "system": {"name": "darwin"},
        }

        result = merge_recorded_environment({}, {"language": "zh-CN"}, created_at="created")

        self.assertEqual(result["captured_at"], "created")
        self.assertEqual(result["evidence_sources"], ["host-runtime", "browser-client"])

    @patch("gpa.replay.evidence.capture_environment")
    def test_partial_legacy_environment_receives_missing_host_identity_with_provenance(self, capture):
        capture.return_value = {
            "schema": "gpa.environment/v1",
            "system": {"name": "darwin", "machine": "arm64"},
            "runtime": {"python": "3.13.5", "executable_family": "cpython"},
            "input_safety": {"desktop_automation_enabled": False},
            "browser": {"family": ""},
            "screen": {"width": 0, "height": 0},
            "locale": {"language": "", "timezone": ""},
        }

        result = merge_recorded_environment({"schema": "gpa.environment/v1"})

        self.assertEqual(result["system"]["name"], "darwin")
        self.assertEqual(result["runtime"]["python"], "3.13.5")
        self.assertIn("host-runtime-enrichment", result["evidence_sources"])
        self.assertIn("system.name", result["host_enriched_fields"])
        self.assertNotIn("client_enriched_fields", result)

    @patch("gpa.replay.evidence.capture_environment")
    def test_external_import_does_not_invent_recorded_host_identity(self, capture):
        capture.return_value = {
            "system": {"name": "darwin", "machine": "arm64"},
            "browser": {"family": "Chrome", "viewport_width": 1280},
            "screen": {"width": 1512, "height": 982},
            "locale": {"language": "zh-CN", "timezone": "Asia/Shanghai"},
        }

        empty = merge_recorded_environment(
            {},
            {"browser": {"family": "Chrome"}},
            allow_host_enrichment=False,
            allow_client_enrichment=False,
        )
        partial = merge_recorded_environment(
            {"schema": "gpa.environment/v1"},
            {"browser": {"family": "Chrome"}},
            allow_host_enrichment=False,
            allow_client_enrichment=False,
        )

        self.assertEqual(empty, {})
        self.assertNotIn("system", partial)
        self.assertNotIn("browser", partial)
        self.assertNotIn("client_enriched_fields", partial)

    def test_prepare_workflow_evidence_always_refreshes_semantic_understanding(self):
        workflow = Workflow(
            workflow_id="evidence",
            workflow_name="evidence",
            workflow_title="Evidence",
            description="Verify evidence",
            environment={"schema": "gpa.environment/v1", "system": {"name": "darwin"}},
            understanding={"schema": "stale"},
            steps=[WorkflowStep(1, "Wait", action_type="wait", value="1")],
        )

        prepared = prepare_workflow_evidence(workflow)

        self.assertEqual(prepared.understanding["schema"], "gpa.agent-understanding/v1")
        self.assertEqual(prepared.understanding["interaction_profile"]["step_count"], 1)


if __name__ == "__main__":
    unittest.main()
