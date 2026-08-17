import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpa.recording.evidence import attach_recording_evidence
from gpa.storage import Workflow, WorkflowStep, WorkflowStorage


class RecordingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage = WorkflowStorage(self.root / "workflows")
        workflow = Workflow(
            workflow_id="verified_recording",
            workflow_name="verified_recording",
            workflow_title="Verified recording",
            description="Attach evidence",
            steps=[WorkflowStep(1, "Verify", action_type="assert_text", value="done")],
            environment={
                "schema": "gpa.environment/v1",
                "system": {"name": "darwin", "machine": "arm64"},
                "screen": {"width": 1280, "height": 720},
            },
            understanding={
                "schema": "gpa.agent-understanding/v1",
                "goal": "Verify a real source",
                "interaction_profile": {"step_count": 1},
                "success_criteria": [{"step": 1, "type": "assert_text", "expected": "done"}],
            },
            artifacts={
                "recording": {
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "sha256": "0" * 64,
                    "bytes": 3,
                }
            },
        )
        directory = self.storage.save(workflow, {})
        (directory / "recording.mp4").write_bytes(b"old")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def probe():
        return {
            "schema": "gpa.recording-media-probe/v1",
            "status": "verified",
            "verified": True,
            "frame_count": 100,
            "fps": 10.0,
            "duration_seconds": 10.0,
            "width": 1280,
            "height": 720,
            "decoded_sample_count": 3,
        }

    def test_attach_preserves_previous_recording_and_writes_privacy_contract(self):
        source = self.root / "new.mp4"
        source.write_bytes(b"new-real-video")
        with patch("gpa.recording.evidence.probe_recording", return_value=self.probe()):
            result = attach_recording_evidence(
                "verified_recording",
                source,
                storage=self.storage,
                capture_scope="browser",
                capture_method="browser-tab-frame-capture",
                privacy_reviewed=True,
                privacy_note="Reviewed first, middle and final frame.",
                source_run_id="run-123",
                browser_family="Codex In-app Browser",
            )

        workflow, _ = self.storage.load("verified_recording")
        recording = workflow.artifacts["recording"]
        self.assertEqual(recording["capture_scope"], "browser-tab")
        self.assertEqual(recording["source_run_id"], "run-123")
        self.assertEqual(recording["privacy_review"]["status"], "passed")
        self.assertFalse(recording["privacy_review"]["other_apps_visible"])
        self.assertEqual(recording["privacy_review"]["samples_reviewed"], [0, 50, 97])
        self.assertEqual(workflow.environment["browser"]["viewport_width"], 1280)
        self.assertEqual(workflow.environment["capture_surface"], {
            "scope": "browser-tab",
            "method": "browser-tab-frame-capture",
            "width": 1280,
            "height": 720,
            "source": "decoded-recording-frames",
        })
        archive = Path(result["archived_previous_recording"])
        self.assertTrue(archive.is_file())
        self.assertEqual(archive.read_bytes(), b"old")
        self.assertEqual((workflow.storage_dir / "recording.mp4").read_bytes(), b"new-real-video")

    def test_monitor_capture_is_rejected_before_workflow_changes(self):
        source = self.root / "monitor.mp4"
        source.write_bytes(b"monitor")
        with self.assertRaisesRegex(ValueError, "one browser tab or one application window"):
            attach_recording_evidence(
                "verified_recording",
                source,
                storage=self.storage,
                capture_scope="monitor",
                capture_method="screen-recorder",
                privacy_reviewed=True,
                privacy_note="Reviewed.",
            )
        workflow, _ = self.storage.load("verified_recording")
        self.assertEqual(workflow.artifacts["recording"]["sha256"], "0" * 64)

    def test_public_web_evidence_requires_and_preserves_verified_source_trace(self):
        source = self.root / "source-evidence.mp4"
        source.write_bytes(b"source-evidence-video")
        trace = self.root / "source-trace.json"
        trace.write_text(json.dumps({
            "schema": "gpa.safe-web-source-trace/v1",
            "workflow_id": "verified_recording",
            "source_run_id": "run-source-1",
            "run_success": True,
            "page_count": 1,
            "pages": [{
                "url": "https://example.com/report",
                "final_url": "https://example.com/report",
                "content_sha256": "a" * 64,
                "verified": True,
            }],
        }), encoding="utf-8")
        with patch("gpa.recording.evidence.probe_recording", return_value=self.probe()):
            result = attach_recording_evidence(
                "verified_recording",
                source,
                storage=self.storage,
                capture_scope="public-web-evidence",
                capture_method="safe-web-source-evidence-render",
                privacy_reviewed=True,
                privacy_note="Only public source evidence is rendered.",
                source_run_id="run-source-1",
                browser_family="GPA Safe Web",
                source_trace_path=trace,
            )

        workflow, _ = self.storage.load("verified_recording")
        recording = workflow.artifacts["recording"]
        source_trace = workflow.artifacts["source_trace"]
        self.assertEqual(recording["evidence_type"], "safe-web-source-evidence")
        self.assertEqual(recording["source_page_count"], 1)
        self.assertEqual(source_trace["verified_page_count"], 1)
        self.assertEqual(workflow.environment["capture_surface"]["source"], "safe-web-source-trace")
        self.assertTrue((workflow.storage_dir / "source_trace.json").is_file())
        self.assertEqual(result["source_trace"]["source_run_id"], "run-source-1")


if __name__ == "__main__":
    unittest.main()
