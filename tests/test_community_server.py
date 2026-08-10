import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import demo_web.server as server
import gpa.storage.workflow as workflow_module
from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowStorage


class DummyHandler:
    def __init__(self, payload=None, *, origin=""):
        raw = json.dumps(payload or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        if origin:
            self.headers["Origin"] = origin
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class CommunityServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.old_workflows_dir = server.WORKFLOWS_DIR
        self.old_community_dir = server.COMMUNITY_DIR
        self.old_module_workflows_dir = workflow_module.WORKFLOWS_DIR
        server.WORKFLOWS_DIR = root / "workflows"
        server.COMMUNITY_DIR = root / "community"
        workflow_module.WORKFLOWS_DIR = server.WORKFLOWS_DIR
        server.WORKFLOWS_DIR.mkdir()
        WorkflowStorage().save(
            Workflow(
                workflow_id="publish_me",
                workflow_name="publish_me",
                workflow_title="Publish Me",
                description="Community API fixture.",
                steps=[WorkflowStep(1, "Click", action_type="click")],
            ),
            {},
        )

    def tearDown(self):
        server.WORKFLOWS_DIR = self.old_workflows_dir
        server.COMMUNITY_DIR = self.old_community_dir
        workflow_module.WORKFLOWS_DIR = self.old_module_workflows_dir
        self._tmp.cleanup()

    def publish(self):
        handler = DummyHandler(
            {
                "workflow_id": "publish_me",
                "author": "Tester",
                "tags": ["demo"],
                "record_license": "CC-BY-4.0",
                "privacy_reviewed": True,
            }
        )
        server._publish_community_record(handler)
        self.assertEqual(handler.status, 201)
        return handler.json()["record"]

    def test_publish_import_and_feedback_api_helpers(self):
        record = self.publish()

        feedback = DummyHandler({"success": True, "note": "works"})
        server._submit_community_feedback(feedback, record["record_id"])
        self.assertEqual(feedback.status, 201)

        imported = DummyHandler({"workflow_id": "imported_from_community"})
        with patch.object(server, "_start_replay") as start_replay:
            server._import_community_record(imported, record["record_id"])
            start_replay.assert_not_called()
        self.assertEqual(imported.status, 201)
        self.assertEqual(imported.json()["workflow_id"], "imported_from_community")
        self.assertFalse(imported.json()["already_saved"])
        self.assertFalse(server.STATE["run"]["active"])

        repeated = DummyHandler({"workflow_id": "another_copy"})
        server._import_community_record(repeated, record["record_id"])
        self.assertEqual(repeated.status, 200)
        self.assertTrue(repeated.json()["already_saved"])
        self.assertEqual(repeated.json()["workflow_id"], "imported_from_community")

        detail = server._community_repository().get_record(record["record_id"], include_feedback=True)
        self.assertEqual(detail["stats"]["imports"], 1)
        self.assertEqual(detail["stats"]["feedback_count"], 1)

    def test_publish_rejects_missing_privacy_confirmation(self):
        handler = DummyHandler({"workflow_id": "publish_me", "privacy_reviewed": False})

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 422)
        self.assertEqual(server._community_repository().list_records(), [])

    def test_demo_records_are_real_idempotent_packages_without_local_workflow_pollution(self):
        first = server._ensure_demo_community_records()
        records = server._community_repository().list_records()

        self.assertEqual(len(first), 4)
        self.assertEqual(len(records), 4)
        self.assertTrue(all("demo" in record["tags"] for record in records))
        self.assertEqual(
            {record["workflow_id"] for record in records},
            {
                "demo_web_research",
                "demo_project_dashboard",
                "demo_meeting_prep",
                "demo_daily_brief",
            },
        )
        self.assertFalse((server.COMMUNITY_DIR / ".demo-seed").exists())
        self.assertEqual(workflow_module.WORKFLOWS_DIR, server.WORKFLOWS_DIR)
        self.assertEqual(
            {path.name for path in server.WORKFLOWS_DIR.iterdir()},
            {"publish_me"},
        )

        second = server._ensure_demo_community_records()
        self.assertTrue(all(record["duplicate"] for record in second))
        self.assertEqual(len(server._community_repository().list_records()), 4)

        selected = next(record for record in records if record["workflow_id"] == "demo_web_research")
        imported = server._community_repository().import_record(
            selected["record_id"],
            workflow_id="saved_demo",
            storage=WorkflowStorage(),
        )
        workflow, _ = WorkflowStorage().load(imported.workflow_id)
        self.assertEqual(workflow.workflow_id, "saved_demo")
        self.assertEqual(workflow.variables[0].name, "query")
        self.assertEqual(workflow.steps[0].action_type, "open_url")

    def test_publish_rejects_oversized_body_without_reading_it(self):
        class NoRead(io.BytesIO):
            def read(self, *args, **kwargs):
                raise AssertionError("oversized request body must not be read")

        handler = DummyHandler()
        handler.headers["Content-Length"] = str(server.COMMUNITY_MAX_JSON_BYTES + 1)
        handler.rfile = NoRead(b"ignored")

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 413)

    def test_community_write_rejects_foreign_origin(self):
        handler = DummyHandler(
            {"workflow_id": "publish_me", "privacy_reviewed": True},
            origin="https://attacker.example",
        )

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 403)
        self.assertEqual(server._community_repository().list_records(), [])

    def test_community_write_rejects_null_origin(self):
        handler = DummyHandler(
            {"workflow_id": "publish_me", "privacy_reviewed": True},
            origin="null",
        )

        server._publish_community_record(handler)

        self.assertEqual(handler.status, 403)
        self.assertEqual(server._community_repository().list_records(), [])


if __name__ == "__main__":
    unittest.main()
