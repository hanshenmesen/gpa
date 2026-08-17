import json
import tempfile
import unittest
from pathlib import Path

import yaml

import gpa.storage.workflow as workflow_module
from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode
from gpa.storage.workflow import (
    Workflow,
    WorkflowStep,
    WorkflowStorage,
    WorkflowVariable,
)


class WorkflowStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workflows_dir = Path(self._tmp.name)
        self._old_workflows_dir = workflow_module.WORKFLOWS_DIR
        workflow_module.WORKFLOWS_DIR = self.workflows_dir

    def tearDown(self):
        workflow_module.WORKFLOWS_DIR = self._old_workflows_dir
        self._tmp.cleanup()

    def test_save_load_roundtrip_preserves_step_action_fields(self):
        wf = Workflow(
            workflow_id="roundtrip",
            workflow_name="roundtrip_workflow",
            workflow_title="Roundtrip Workflow",
            description="Exercise persisted action fields.",
            provenance={
                "kind": "public-benchmark",
                "benchmark": "AssistantBench",
                "task_id": "abc123",
            },
            environment={"schema": "gpa.environment/v1", "system": {"name": "darwin"}},
            understanding={"schema": "gpa.agent-understanding/v1", "goal": "Round trip"},
            artifacts={"recording": {"path": "recording.webm", "mime_type": "video/webm"}},
            variables=[
                WorkflowVariable(
                    name="message_text",
                    default_value="Hello",
                    description="Text to type",
                )
            ],
            steps=[
                WorkflowStep(
                    step_number=1,
                    action="Click editor",
                    id="click-step",
                    action_type="click",
                    pause_duration=1.25,
                    active_app_name="TextEdit",
                ),
                WorkflowStep(
                    step_number=2,
                    action="Type text",
                    id="type-step",
                    action_type="type",
                    value="{{message_text}}",
                    pause_duration=0.75,
                    active_app_name="TextEdit",
                ),
                WorkflowStep(
                    step_number=3,
                    action="Save file",
                    id="hotkey-step",
                    action_type="hotkey",
                    value="cmd+s",
                    pause_duration=0.2,
                    active_app_name="TextEdit",
                    metadata={"recorded_clipboard_text": "selected text"},
                ),
                WorkflowStep(
                    step_number=4,
                    action="Scroll",
                    id="scroll-step",
                    action_type="scroll",
                    value="",
                    pause_duration=0.4,
                    active_app_name="Finder",
                ),
            ],
        )

        subgraph = StepSubgraph(
            target_element_id=1,
            click_coordinates=[120.0, 240.0],
            ui_graph=UIGraph(
                nodes=[
                    UINode(id=1, pos=[100.0, 220.0, 40.0, 40.0], elem_type="icon", content=None),
                    UINode(id=2, pos=[160.0, 220.0, 80.0, 30.0], elem_type="text", content="Save"),
                ],
                edges=[(1, 2)],
                image_size=[800, 600],
                window_bounds=[0.0, 0.0, 800.0, 600.0],
            ),
            window_bounds=[0.0, 0.0, 800.0, 600.0],
        )

        storage = WorkflowStorage()
        storage.save(wf, {"click-step": subgraph})
        loaded, subgraphs = storage.load("roundtrip")

        self.assertEqual(sorted(subgraphs), ["click-step"])
        self.assertEqual(subgraphs["click-step"].target_element_id, 1)
        self.assertEqual(subgraphs["click-step"].click_coordinates, [120.0, 240.0])
        self.assertEqual(len(subgraphs["click-step"].ui_graph.nodes), 2)
        self.assertEqual(subgraphs["click-step"].ui_graph.edges, [(1, 2)])
        self.assertEqual(loaded.workflow_id, wf.workflow_id)
        self.assertEqual(len(loaded.steps), len(wf.steps))

        for expected, actual in zip(wf.steps, loaded.steps, strict=True):
            self.assertEqual(actual.step_number, expected.step_number)
            self.assertEqual(actual.action, expected.action)
            self.assertEqual(actual.id, expected.id)
            self.assertEqual(actual.action_type, expected.action_type)
            self.assertEqual(actual.value, expected.value)
            self.assertEqual(actual.pause_duration, expected.pause_duration)
            self.assertEqual(actual.active_app_name, expected.active_app_name)
            self.assertEqual(actual.metadata, expected.metadata)

        self.assertEqual(loaded.variables[0].description, "Text to type")
        self.assertEqual(loaded.provenance, wf.provenance)
        self.assertEqual(loaded.environment, wf.environment)
        self.assertEqual(loaded.understanding, wf.understanding)
        self.assertEqual(loaded.artifacts, wf.artifacts)
        self.assertEqual(
            json.loads((self.workflows_dir / "roundtrip" / "environment.json").read_text()),
            wf.environment,
        )
        self.assertEqual(
            json.loads((self.workflows_dir / "roundtrip" / "understanding.json").read_text()),
            wf.understanding,
        )

    def test_load_legacy_yaml_defaults_missing_step_action_fields(self):
        workflow_dir = self.workflows_dir / "legacy"
        workflow_dir.mkdir()
        with open(workflow_dir / "workflow.yaml", "w") as f:
            yaml.safe_dump(
                {
                    "workflow_id": "legacy",
                    "workflow_name": "legacy_workflow",
                    "workflow_title": "Legacy Workflow",
                    "description": "Old paper-compatible format.",
                    "running_config": {
                        "variable_values": {"name": "Ada"},
                        "category": "",
                    },
                    "steps": [
                        {
                            "step_number": 1,
                            "Action": "Click target",
                            "id": "legacy-step",
                        }
                    ],
                },
                f,
            )
        with open(workflow_dir / "metadata.json", "w") as f:
            json.dump(
                {
                    "created_at": "2026-07-08T00:00:00+00:00",
                    "workflow_metadata": {
                        "variables": [
                            {
                                "name": "name",
                                "default_value": "Ada",
                                "description": "Display name",
                            }
                        ]
                    },
                },
                f,
            )

        loaded, subgraphs = WorkflowStorage().load("legacy")

        self.assertEqual(subgraphs, {})
        self.assertEqual(loaded.steps[0].action_type, "click")
        self.assertEqual(loaded.steps[0].value, "")
        self.assertEqual(loaded.steps[0].pause_duration, 0.5)
        self.assertEqual(loaded.steps[0].active_app_name, "")
        self.assertEqual(loaded.variables[0].description, "Display name")

    def test_workflow_identity_cannot_escape_or_disagree_with_repository_path(self):
        storage = WorkflowStorage()
        with self.assertRaisesRegex(ValueError, "Unsafe workflow_id"):
            storage.save(Workflow("../escape", "escape", "Escape", "Unsafe"), {})

        workflow_dir = self.workflows_dir / "expected"
        workflow_dir.mkdir()
        with open(workflow_dir / "workflow.yaml", "w") as f:
            yaml.safe_dump(
                {
                    "workflow_id": "different",
                    "workflow_name": "different",
                    "workflow_title": "Different",
                    "description": "Mismatched identity",
                    "steps": [],
                },
                f,
            )

        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            storage.load("expected")

    def test_instance_storage_root_is_isolated_from_global_repository(self):
        isolated_root = self.workflows_dir / "other-agent"
        isolated = WorkflowStorage(isolated_root)
        workflow = Workflow(
            "portable",
            "portable",
            "Portable workflow",
            "Imported into another Agent workspace.",
            steps=[WorkflowStep(1, "Wait", action_type="wait", value="0")],
        )

        isolated.save(workflow, {})
        loaded, _ = isolated.load("portable")

        self.assertEqual(isolated.workflows_dir, isolated_root.resolve())
        self.assertEqual(loaded.storage_dir, isolated_root.resolve() / "portable")
        self.assertTrue((loaded.storage_dir / "workflow.yaml").is_file())
        self.assertFalse((self.workflows_dir / "portable").exists())
        self.assertEqual([item["id"] for item in isolated.list_workflows()], ["portable"])


if __name__ == "__main__":
    unittest.main()
