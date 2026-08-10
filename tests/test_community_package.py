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
        self.assertEqual(subgraphs, {})

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
