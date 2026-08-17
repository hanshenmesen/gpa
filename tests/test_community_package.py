import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import gpa.community.package as package_module
import gpa.storage.workflow as workflow_module
from gpa.community.package import (
    MANIFEST_NAME,
    export_workflow_package,
    import_workflow_package,
    inspect_workflow_package,
)
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage, WorkflowVariable


class CommunityPackageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workflows_dir = Path(self._tmp.name) / "workflows"
        self.packages_dir = Path(self._tmp.name) / "packages"
        self.workflows_dir.mkdir()
        self.packages_dir.mkdir()
        self._old_workflows_dir = workflow_module.WORKFLOWS_DIR
        workflow_module.WORKFLOWS_DIR = self.workflows_dir

    def tearDown(self):
        workflow_module.WORKFLOWS_DIR = self._old_workflows_dir
        self._tmp.cleanup()

    def rewrite_manifest(self, source: Path, destination: Path, mutate) -> None:
        with zipfile.ZipFile(source) as source_zip, zipfile.ZipFile(destination, "w") as target_zip:
            for info in source_zip.infolist():
                data = source_zip.read(info.filename)
                if info.filename == MANIFEST_NAME:
                    manifest = json.loads(data.decode("utf-8"))
                    mutate(manifest)
                    data = json.dumps(manifest).encode("utf-8")
                target_zip.writestr(info, data)

    def rewrite_member(self, source: Path, destination: Path, member: str, payload: dict) -> None:
        replacement = json.dumps(payload).encode("utf-8")
        with zipfile.ZipFile(source) as source_zip:
            files = {info.filename: source_zip.read(info.filename) for info in source_zip.infolist()}
            infos = {info.filename: info for info in source_zip.infolist()}
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
        files[member] = replacement
        for item in manifest["files"]:
            if item["path"] == member:
                item["bytes"] = len(replacement)
                item["sha256"] = hashlib.sha256(replacement).hexdigest()
        files[MANIFEST_NAME] = json.dumps(manifest).encode("utf-8")
        with zipfile.ZipFile(destination, "w") as target_zip:
            for name, data in files.items():
                target_zip.writestr(infos[name], data)

    def test_export_import_package_roundtrip(self):
        storage = WorkflowStorage()
        workflow = Workflow(
            workflow_id="shareable",
            workflow_name="shareable_record",
            workflow_title="Shareable Record",
            description="Workflow intended for community sharing.",
            variables=[
                WorkflowVariable(
                    name="message",
                    default_value="hello",
                    description="Message to type",
                )
            ],
            steps=[
                WorkflowStep(
                    step_number=1,
                    action="Type message",
                    id="type-step",
                    action_type="type",
                    value="{{message}}",
                    active_app_name="TextEdit",
                    metadata={
                        "recorded_event_indices": [1, 2, 3],
                        "intent_normalization": {
                            "strategy": "typed_correction_or_continuation",
                            "source_event_count": 3,
                        },
                        "recorded_clipboard_text": "final task content",
                        "recorded_clipboard_changed": True,
                    },
                )
            ],
        )
        storage.save(workflow, {})

        package_path = export_workflow_package("shareable", self.packages_dir, storage=storage)
        self.assertTrue(package_path.exists())

        with zipfile.ZipFile(package_path) as zf:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        inspected = inspect_workflow_package(package_path)
        self.assertEqual(manifest["workflow_id"], "shareable")
        self.assertEqual(inspected["workflow_id"], "shareable")
        self.assertEqual(manifest["workflow_name"], "shareable_record")
        self.assertTrue(manifest["privacy"]["review_required"])
        self.assertEqual(manifest["replay"]["schema"], "gpa.replay/v1")
        self.assertIn("keyboard", manifest["replay"]["capabilities"])

        result = import_workflow_package(
            package_path,
            workflow_id="imported_shareable",
            storage=storage,
        )

        loaded, subgraphs = storage.load("imported_shareable")
        self.assertEqual(result.workflow_id, "imported_shareable")
        self.assertEqual(loaded.workflow_name, "shareable_record")
        self.assertEqual(loaded.steps[0].value, "{{message}}")
        self.assertEqual(
            loaded.steps[0].metadata["intent_normalization"],
            {
                "strategy": "typed_correction_or_continuation",
                "source_event_count": 3,
            },
        )
        self.assertEqual(loaded.steps[0].metadata["recorded_clipboard_text"], "final task content")
        self.assertNotIn("recorded_clipboard_before", loaded.steps[0].metadata)
        self.assertEqual(subgraphs, {})

    def test_package_roundtrip_preserves_recording_and_reproduction_context(self):
        storage = WorkflowStorage()
        workflow = Workflow(
            workflow_id="recorded_fixture",
            workflow_name="recorded_fixture",
            workflow_title="Recorded Fixture",
            description="Portable replay evidence.",
            environment={"schema": "gpa.environment/v1", "system": {"name": "darwin"}},
            understanding={"schema": "gpa.agent-understanding/v1", "goal": "Reproduce it"},
            artifacts={
                "recording": {
                    "kind": "screen-recording",
                    "path": "recording.webm",
                    "mime_type": "video/webm",
                    "bytes": 12,
                }
            },
        )
        workflow_dir = storage.save(workflow, {})
        recording = b"\x1a\x45\xdf\xa3webm-fixture"
        (workflow_dir / "recording.webm").write_bytes(recording)
        workflow.artifacts["recording"]["bytes"] = len(recording)
        workflow.artifacts["recording"]["sha256"] = hashlib.sha256(recording).hexdigest()
        storage.save(workflow, {})

        package_path = export_workflow_package("recorded_fixture", self.packages_dir, storage=storage)
        manifest = inspect_workflow_package(package_path)
        self.assertEqual(manifest["environment"], workflow.environment)
        self.assertEqual(manifest["understanding"], workflow.understanding)
        self.assertEqual(manifest["artifacts"], workflow.artifacts)
        self.assertIn("workflow/recording.webm", {item["path"] for item in manifest["files"]})

        result = import_workflow_package(
            package_path,
            workflow_id="recorded_imported",
            storage=storage,
        )
        loaded, _ = storage.load(result.workflow_id)
        self.assertEqual(loaded.environment, workflow.environment)
        self.assertEqual(loaded.understanding, workflow.understanding)
        self.assertEqual(loaded.artifacts, workflow.artifacts)
        self.assertEqual((result.storage_dir / "recording.webm").read_bytes(), recording)

        bad_checksum = self.packages_dir / "bad-recording-checksum.gpa-record.zip"
        self.rewrite_manifest(
            package_path,
            bad_checksum,
            lambda manifest: manifest["artifacts"]["recording"].update({"sha256": "0" * 64}),
        )
        with self.assertRaisesRegex(ValueError, "recording checksum"):
            inspect_workflow_package(bad_checksum)

        bad_container = self.packages_dir / "bad-recording-container.gpa-record.zip"
        replacement = b"this-is-not-a-video"
        with zipfile.ZipFile(package_path) as source_zip:
            members = {info.filename: source_zip.read(info.filename) for info in source_zip.infolist()}
            infos = {info.filename: info for info in source_zip.infolist()}
        manifest = json.loads(members[MANIFEST_NAME].decode("utf-8"))
        recording_member = "workflow/recording.webm"
        replacement_sha = hashlib.sha256(replacement).hexdigest()
        members[recording_member] = replacement
        for item in manifest["files"]:
            if item["path"] == recording_member:
                item.update({"bytes": len(replacement), "sha256": replacement_sha})
        manifest["artifacts"]["recording"].update({
            "bytes": len(replacement),
            "sha256": replacement_sha,
        })
        members[MANIFEST_NAME] = json.dumps(manifest).encode("utf-8")
        with zipfile.ZipFile(bad_container, "w") as target_zip:
            for name, data in members.items():
                target_zip.writestr(infos[name], data)
        with self.assertRaisesRegex(ValueError, "container signature"):
            inspect_workflow_package(bad_container)

    def test_import_rejects_unsafe_zip_path(self):
        package_path = self.packages_dir / "bad.gpa-record.zip"
        with zipfile.ZipFile(package_path, "w") as zf:
            zf.writestr(MANIFEST_NAME, "{}")
            zf.writestr("../evil.txt", "nope")

        with self.assertRaisesRegex(ValueError, "Unsafe package path"):
            import_workflow_package(package_path, storage=WorkflowStorage())

    def test_inspection_rejects_invalid_manifest_number_and_large_metadata(self):
        storage = WorkflowStorage()
        storage.save(
            Workflow(
                workflow_id="manifest_fixture",
                workflow_name="manifest_fixture",
                workflow_title="Manifest Fixture",
                description="Package validation fixture.",
            ),
            {},
        )
        source = export_workflow_package("manifest_fixture", self.packages_dir, storage=storage)

        invalid_number = self.packages_dir / "invalid-number.gpa-record.zip"
        self.rewrite_manifest(
            source,
            invalid_number,
            lambda manifest: manifest["files"][0].update({"bytes": None}),
        )
        with self.assertRaisesRegex(ValueError, r"files\[\]\.bytes"):
            inspect_workflow_package(invalid_number)

        oversized_manifest = self.packages_dir / "large-manifest.gpa-record.zip"
        self.rewrite_manifest(
            source,
            oversized_manifest,
            lambda manifest: manifest.update({"padding": "x" * (300 * 1024)}),
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            inspect_workflow_package(oversized_manifest)

    def test_import_uses_validated_snapshot_if_source_path_changes(self):
        storage = WorkflowStorage()
        storage.save(
            Workflow(
                workflow_id="snapshot_fixture",
                workflow_name="snapshot_fixture",
                workflow_title="Snapshot Fixture",
                description="TOCTOU fixture.",
            ),
            {},
        )
        source = export_workflow_package("snapshot_fixture", self.packages_dir, storage=storage)
        real_inspect = package_module.inspect_workflow_package

        def inspect_after_source_replacement(path, **kwargs):
            self.assertNotEqual(Path(path), source)
            source.write_bytes(b"replaced after snapshot")
            return real_inspect(path, **kwargs)

        with patch.object(package_module, "inspect_workflow_package", side_effect=inspect_after_source_replacement):
            result = import_workflow_package(
                source,
                workflow_id="snapshot_imported",
                storage=storage,
            )

        self.assertEqual(result.workflow_id, "snapshot_imported")
        loaded, _ = storage.load("snapshot_imported")
        self.assertEqual(loaded.workflow_title, "Snapshot Fixture")

    def test_inspection_rejects_excessive_graph_complexity(self):
        storage = WorkflowStorage()
        storage.save(
            Workflow(
                workflow_id="graph_fixture",
                workflow_name="graph_fixture",
                workflow_title="Graph Fixture",
                description="Graph complexity fixture.",
                steps=[WorkflowStep(1, "Click target", id="graph-step", action_type="click")],
            ),
            {},
        )
        source = export_workflow_package("graph_fixture", self.packages_dir, storage=storage)
        oversized_graph = self.packages_dir / "oversized-graph.gpa-record.zip"
        self.rewrite_member(
            source,
            oversized_graph,
            "workflow/steps_data.json",
            {
                "graph-step": {
                    "ui_graph": {
                        "G": {
                            "nodes": [{} for _ in range(package_module.DEFAULT_MAX_GRAPH_NODES_PER_STEP + 1)],
                            "edges": [],
                        }
                    }
                }
            },
        )

        with self.assertRaisesRegex(ValueError, "too many nodes"):
            inspect_workflow_package(oversized_graph)

    def test_failed_overwrite_import_restores_existing_workflow(self):
        storage = WorkflowStorage()
        storage.save(
            Workflow(
                workflow_id="rollback_fixture",
                workflow_name="rollback_fixture",
                workflow_title="Original Workflow",
                description="Rollback fixture.",
                steps=[WorkflowStep(1, "Click target", id="rollback-step", action_type="click")],
            ),
            {},
        )
        source = export_workflow_package("rollback_fixture", self.packages_dir, storage=storage)
        broken = self.packages_dir / "broken-graph.gpa-record.zip"
        self.rewrite_member(
            source,
            broken,
            "workflow/steps_data.json",
            {
                "rollback-step": {
                    "target_element_id": 0,
                    "click_coordinates": [10, 10],
                    "window_bounds": [0, 0, 100, 100],
                    "ui_graph": {
                        "G": {
                            "directed": False,
                            "multigraph": False,
                            "graph": {},
                            "nodes": [{"id": 0}],
                            "edges": [],
                        }
                    },
                }
            },
        )

        with self.assertRaises((KeyError, TypeError, ValueError)):
            import_workflow_package(broken, overwrite=True, storage=storage)

        restored, _ = storage.load("rollback_fixture")
        self.assertEqual(restored.workflow_title, "Original Workflow")


if __name__ == "__main__":
    unittest.main()
