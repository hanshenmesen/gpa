import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import demo_web.server as server_module
from gpa.storage.workflow import Workflow, WorkflowStep


class ReplayServerApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.previous_workflows = server_module.WORKFLOWS_DIR
        self.previous_spaces = server_module.REPLAY_SPACES_DIR
        self.previous_community = server_module.COMMUNITY_DIR
        server_module.WORKFLOWS_DIR = root / "workflows"
        server_module.REPLAY_SPACES_DIR = root / "spaces"
        server_module.COMMUNITY_DIR = root / "community"
        server_module.WORKFLOWS_DIR.mkdir(parents=True)
        storage = server_module._storage()
        storage.save(
            Workflow(
                workflow_id="api_replay",
                workflow_name="api_replay",
                workflow_title="API Replay",
                description="Replay endpoint fixture",
                task_description="打开网页并输入内容",
                steps=[
                    WorkflowStep(1, "打开网页", action_type="open_url", value="https://example.test", active_app_name="Safari"),
                    WorkflowStep(2, "输入内容", action_type="type", value="hello", active_app_name="Safari"),
                ],
            ),
            {},
        )
        storage.save(
            Workflow(
                workflow_id="unsupported_replay",
                workflow_name="unsupported_replay",
                workflow_title="Unsupported Replay",
                description="Unsupported action fixture",
                task_description="执行不支持的系统动作",
                steps=[WorkflowStep(1, "执行 shell", action_type="shell")],
            ),
            {},
        )
        records = server_module._ensure_demo_community_records()
        self.community_record_id = records[0]["record_id"]
        self.httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server_module.WORKFLOWS_DIR = self.previous_workflows
        server_module.REPLAY_SPACES_DIR = self.previous_spaces
        server_module.COMMUNITY_DIR = self.previous_community
        self.temporary.cleanup()

    def get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read())

    def get_text(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.read().decode()

    def post_json(self, path, body):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_replay_manifest_intent_and_space_endpoints(self):
        studio = self.get_text("/")
        store = self.get_text("/store")
        listing = self.get_json("/api/replays?platform=windows")
        detail = self.get_json("/api/replays/api_replay")
        intent_status, intent = self.post_json("/api/replays/intent", {
            "goal": "在浏览器搜索资料",
            "steps": [{"action_type": "type", "app": "Safari", "description": "输入关键词"}],
        })
        plan_status, planned = self.post_json("/api/replays/api_replay/plan", {"platform": "windows"})
        space = self.get_json(f"/api/replay-spaces/{planned['plan']['space_id']}")
        community_detail = self.get_json(f"/api/community/records/{self.community_record_id}")

        self.assertTrue(listing["ok"])
        self.assertIn("Replay Studio", studio)
        self.assertIn("上传 Replay 插件", store)
        self.assertEqual(listing["replays"][0]["compatibility"]["status"], "degraded")
        self.assertEqual(detail["replay"]["schema"], "gpa.replay/v1")
        self.assertEqual(intent_status, 200)
        self.assertEqual(intent["intent"]["apps"], ["Safari"])
        self.assertEqual(plan_status, 201)
        self.assertEqual(planned["plan"]["steps"][0]["app"], "Microsoft Edge")
        self.assertEqual(space["space"]["state"], "planned")
        self.assertEqual(community_detail["record"]["record_id"], self.community_record_id)

    def test_foreign_origin_cannot_heartbeat_or_arm_replay(self):
        for path, body in (
            ("/api/client/heartbeat", {"client_id": "foreign"}),
            ("/api/run/arm", {"workflow_id": "api_replay"}),
        ):
            request = urllib.request.Request(
                self.base + path,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://attacker.example",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 403)

    def test_supplied_space_cannot_bypass_compatibility(self):
        service = server_module._replay_service()
        plan = service.plan("unsupported_replay")
        self.assertFalse(plan.compatibility.runnable)

        with self.assertRaisesRegex(ValueError, "incompatible"):
            server_module._prepare_replay_space("unsupported_replay", plan.space_id)

        original_desktop_gate = server_module._require_desktop_automation
        original_dependencies = server_module._ensure_dependencies
        original_permissions = server_module._ensure_permissions
        server_module._require_desktop_automation = lambda *args, **kwargs: True
        server_module._ensure_dependencies = lambda *args, **kwargs: None
        server_module._ensure_permissions = lambda *args, **kwargs: None
        try:
            request = urllib.request.Request(
                self.base + "/api/workflows/unsupported_replay/run",
                data=json.dumps({"arm_token": "unused", "space_id": plan.space_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 422)
        finally:
            server_module._require_desktop_automation = original_desktop_gate
            server_module._ensure_dependencies = original_dependencies
            server_module._ensure_permissions = original_permissions


if __name__ == "__main__":
    unittest.main()
