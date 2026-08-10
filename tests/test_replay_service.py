import tempfile
import unittest
from pathlib import Path

from gpa.replay.service import ReplayService
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowVariable


class FakeWorkflowStorage:
    def __init__(self, workflow):
        self.workflow = workflow

    def list_workflows(self):
        return [{"id": self.workflow.workflow_id}]

    def load(self, replay_id):
        if replay_id != self.workflow.workflow_id:
            raise FileNotFoundError(replay_id)
        return self.workflow, {}


class ReplayServiceTests(unittest.TestCase):
    def setUp(self):
        self.workflow = Workflow(
            workflow_id="portable_demo",
            workflow_name="portable_demo",
            workflow_title="Portable demo",
            description="Cross-system text entry",
            task_description="在文本编辑器输入内容并保存",
            variables=[WorkflowVariable("content", "hello", "要输入的内容")],
            steps=[
                WorkflowStep(1, "输入内容", action_type="type", value="{{content}}", active_app_name="TextEdit"),
                WorkflowStep(2, "保存", action_type="hotkey", value="cmd+s", active_app_name="TextEdit"),
            ],
        )

    def test_adapts_legacy_workflow_to_digest_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReplayService(FakeWorkflowStorage(self.workflow), spaces_root=Path(directory))
            manifest = service.get_replay("portable_demo")

        self.assertEqual(manifest.schema, "gpa.replay/v1")
        self.assertEqual(manifest.intent.variables, ("content",))
        self.assertTrue(manifest.digest.startswith("sha256:"))
        self.assertEqual(len(manifest.steps), 2)

    def test_plan_creates_inspectable_isolated_space(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReplayService(FakeWorkflowStorage(self.workflow), spaces_root=Path(directory))
            plan = service.plan("portable_demo", platform="windows")
            space = service.spaces.get(plan.space_id)

        self.assertEqual(plan.compatibility.status, "degraded")
        self.assertEqual(plan.steps[0].app, "Notepad")
        self.assertEqual(space["state"], "planned")
        self.assertEqual(space["replay_id"], "portable_demo")

    def test_list_replays_includes_intent_and_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReplayService(FakeWorkflowStorage(self.workflow), spaces_root=Path(directory))
            rows = service.list_replays(platform="linux")

        self.assertEqual(rows[0]["replay_id"], "portable_demo")
        self.assertEqual(rows[0]["compatibility"]["status"], "degraded")
        self.assertEqual(rows[0]["intent"]["goal"], "在文本编辑器输入内容并保存")
