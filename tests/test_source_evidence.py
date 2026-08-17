import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpa.recording.source_evidence import build_safe_web_source_evidence
from gpa.storage import Workflow, WorkflowStep, WorkflowStorage


class SourceEvidenceTests(unittest.TestCase):
    def test_successful_safe_web_run_renders_video_and_machine_trace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            storage = WorkflowStorage(root / "workflows")
            workflow = Workflow(
                workflow_id="source_trace",
                workflow_name="source_trace",
                workflow_title="Public source trace",
                description="Verify a public source",
                steps=[
                    WorkflowStep(1, "Open", action_type="open_url", value="https://example.test/2023"),
                    WorkflowStep(2, "Present", action_type="assert_text", value="Precipitation"),
                    WorkflowStep(3, "Absent", action_type="assert_not_text", value="Snow"),
                ],
            )
            storage.save(workflow, {})
            runs = root / "runs" / workflow.workflow_id
            runs.mkdir(parents=True)
            run_id = "run-source-trace"
            (runs / f"{run_id}.json").write_text(json.dumps({
                "workflow_id": workflow.workflow_id,
                "run_id": run_id,
                "status": "succeeded",
                "success": True,
                "steps_run": 3,
                "execution_mode": "safe_web",
            }), encoding="utf-8")
            output = root / "source-evidence.mp4"
            with patch(
                "gpa.recording.source_evidence.fetch_public_page",
                return_value=(
                    "https://example.test/2023",
                    "Example Weather History\n2023\nPrecipitation 0.00",
                    200,
                ),
            ):
                result = build_safe_web_source_evidence(
                    workflow.workflow_id,
                    run_id,
                    output,
                    storage=storage,
                    runs_dir=root / "runs",
                    fps=2,
                    seconds_per_source=0.5,
                )

            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            trace = result["trace"]
            self.assertEqual(trace["schema"], "gpa.safe-web-source-trace/v1")
            self.assertEqual(trace["page_count"], 1)
            self.assertEqual(trace["pages"][0]["negative_terms"], ["Snow"])
            self.assertTrue(trace["pages"][0]["verified"])
            self.assertTrue(Path(result["trace_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
