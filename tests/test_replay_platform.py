import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpa.replay.domain import ReplayIntent, ReplayManifest, ReplayStep
from gpa.replay.intent import IntentParser
from gpa.replay.platforms import PlatformPlanner, current_platform
from gpa.replay.spaces import ReplaySpaceManager


class ReplayIntentTests(unittest.TestCase):
    def test_parser_derives_apps_capabilities_permissions_and_risk(self):
        steps = [
            ReplayStep(1, "open_url", "打开订单页", value="https://example.test", app="Safari"),
            ReplayStep(2, "type", "输入订单号", value="{{order_id}}", app="Safari"),
            ReplayStep(3, "hotkey", "提交订单", value="cmd+enter", app="Safari"),
        ]

        intent = IntentParser().parse("填写并提交订单", steps, ["order_id"])

        self.assertEqual(intent.apps, ("Safari",))
        self.assertEqual(intent.capabilities, ("keyboard", "navigation"))
        self.assertEqual(intent.permissions, ("browser_control", "input_control"))
        self.assertEqual(intent.variables, ("order_id",))
        self.assertTrue(intent.irreversible)

    def test_parser_does_not_require_network_or_llm(self):
        intent = IntentParser().parse("", [ReplayStep(1, "click", "点击保存")])
        self.assertIn("1 步", intent.summary)
        self.assertEqual(intent.confidence, 0.75)


class PlatformPlannerTests(unittest.TestCase):
    def _manifest(self, *steps):
        return ReplayManifest(
            replay_id="sample",
            title="Sample",
            description="",
            version="1.0.0",
            author="test",
            source="test",
            intent=ReplayIntent(goal="test", summary="test"),
            steps=tuple(steps),
        )

    def test_windows_plan_maps_app_and_hotkey(self):
        manifest = self._manifest(
            ReplayStep(1, "hotkey", "保存", value="cmd+s", app="TextEdit"),
        )

        steps, report = PlatformPlanner().plan_steps(manifest, "windows")

        self.assertEqual(steps[0].app, "Notepad")
        self.assertEqual(steps[0].value, "ctrl+s")
        self.assertEqual(report.status, "degraded")
        self.assertTrue(report.runnable)

    def test_coordinate_only_step_fails_closed_across_systems(self):
        manifest = self._manifest(
            ReplayStep(1, "click", "点击按钮", metadata={"coordinate_only": True}),
        )

        steps, report = PlatformPlanner().plan_steps(manifest, "linux")

        self.assertFalse(steps[0].supported)
        self.assertEqual(report.status, "unsupported")
        self.assertIn("semantic_target", report.missing_capabilities)
        self.assertFalse(report.runnable)

    def test_host_platform_names_are_canonical(self):
        with patch("gpa.replay.platforms.host_platform.system", return_value="Windows"):
            self.assertEqual(current_platform(), "windows")


class ReplaySpaceTests(unittest.TestCase):
    def test_space_persists_plan_and_enforces_state_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ReplaySpaceManager(Path(directory))
            space = manager.create("sample", "darwin")
            planned = manager.attach_plan(space["space_id"], {"steps": []})

            self.assertEqual(planned["state"], "planned")
            self.assertTrue((Path(directory) / space["space_id"] / "plan.json").is_file())
            with self.assertRaises(ValueError):
                manager.transition(space["space_id"], "running")
