import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from gpa.community.package import export_workflow_package
from gpa.replay.audit import audit_reproduction_package
from gpa.replay.understanding import build_agent_understanding
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage


class ReproductionAuditTests(unittest.TestCase):
    def test_real_package_is_imported_and_executed_in_isolated_repository(self):
        try:
            import cv2
        except ImportError:
            self.skipTest("OpenCV is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_storage = WorkflowStorage(root / "source-workflows")
            workflow = Workflow(
                workflow_id="portable_public_task",
                workflow_name="portable_public_task",
                workflow_title="Portable public task",
                description="Verify a public answer without desktop input.",
                task_description="Find and verify the answer.",
                provenance={"kind": "public-source", "source_url": "https://example.org/"},
                environment={
                    "schema": "gpa.environment/v1",
                    "system": {"name": "darwin", "machine": "arm64"},
                    "locale": {"language": "zh-CN", "timezone": "America/Recife"},
                    "screen": {"width": 2940, "height": 1912, "pixel_ratio": 2},
                    "browser": {"family": "Google Chrome"},
                },
                steps=[
                    WorkflowStep(1, "Open source", action_type="open_url", value="https://example.org/"),
                    WorkflowStep(2, "Wait for answer", action_type="wait_for_text", value="answer"),
                    WorkflowStep(3, "Assert answer", action_type="assert_text", value="answer"),
                    WorkflowStep(4, "Store answer", action_type="set_clipboard", value="42"),
                    WorkflowStep(5, "Verify answer", action_type="assert_clipboard", value="42"),
                ],
            )
            workflow.understanding = build_agent_understanding(workflow)
            source_dir = source_storage.save(workflow, {})
            recording_path = source_dir / "recording.mp4"
            writer = cv2.VideoWriter(
                str(recording_path),
                cv2.VideoWriter_fourcc(*"mp4v"),
                12.0,
                (64, 48),
            )
            if not writer.isOpened():
                self.skipTest("OpenCV MP4 writer is unavailable")
            try:
                for index in range(12):
                    frame = np.zeros((48, 64, 3), dtype=np.uint8)
                    frame[:, :, index % 3] = 40 + index * 8
                    writer.write(frame)
            finally:
                writer.release()
            recording = recording_path.read_bytes()
            workflow.artifacts = {
                "recording": {
                    "kind": "screen-recording",
                    "path": "recording.mp4",
                    "mime_type": "video/mp4",
                    "bytes": len(recording),
                    "sha256": hashlib.sha256(recording).hexdigest(),
                    "duration_seconds": 1.0,
                    "width": 64,
                    "height": 48,
                    "source_run_id": "source-run",
                    "capture_scope": "browser-tab",
                    "capture_method": "browser-tab-frame-capture",
                    "privacy_review": {
                        "status": "passed",
                        "other_apps_visible": False,
                        "scope_confirmed": "browser-tab",
                    },
                }
            }
            source_storage.save(workflow, {})
            package = export_workflow_package(
                workflow.workflow_id,
                root / "packages",
                storage=source_storage,
            )
            target = {
                "schema": "gpa.environment/v1",
                "system": {"name": "linux", "machine": "x86_64"},
                "locale": {"language": "en-US", "timezone": "UTC"},
                "screen": {"width": 1920, "height": 1080, "pixel_ratio": 1},
                "browser": {"family": "Chromium"},
            }
            isolated_workspace = root / "other-agent"

            with patch(
                "gpa.execution.safe_web.fetch_public_page",
                return_value=("https://example.org/", "answer", 200),
            ):
                report = audit_reproduction_package(
                    package,
                    target_environment=target,
                    workspace=isolated_workspace,
                )

            self.assertEqual(report["recording"]["source_run_id"], "source-run")
            self.assertEqual(report["recording"]["run_id"], "source-run")

            self.assertEqual(report["schema"], "gpa.isolated-reproduction-audit/v1")
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["cross_agent_reproducible"])
            self.assertTrue(report["isolation"]["separate_workflow_repository"])
            self.assertTrue(report["recording"]["verified"])
            self.assertEqual(report["reproduction_contract"]["score"], 100)
            self.assertEqual(report["reproduction_contract"]["status"], "adaptation_required")
            self.assertEqual(report["environment_diff"]["status"], "blocked")
            self.assertEqual(report["execution"]["mode"], "safe_web")
            self.assertTrue(report["execution"]["success"])
            self.assertEqual(report["execution"]["steps_run"], 5)
            self.assertFalse(report["execution"]["desktop_input"])
            self.assertTrue(
                (isolated_workspace / "workflows" / "portable_public_task" / "recording.mp4").is_file()
            )
            self.assertTrue((source_dir / "recording.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
