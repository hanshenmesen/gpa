import json
import os
import platform
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from gpa.cloud.service_config import CloudServiceConfig
from gpa.cloud.website_agent import (
    AgentCredentialStore,
    CloudAgentService,
    WebsiteAgentClient,
)
from gpa.storage.workflow import WorkflowStorage


class FakeCloud:
    def __init__(self):
        self.paired = False
        self.device_id = str(uuid.uuid4())
        self.command_id = str(uuid.uuid4())
        self.reports = []

    def __call__(self, method, path, body, token):
        if path == "/api/agent/pair/start":
            return {
                "pairing_id": str(uuid.uuid4()),
                "device_secret": "gpa_pair_" + "a" * 43,
                "device_token": "gpa_device_" + "b" * 43,
                "claim_url": "https://example.test/app/agents/pair#claim=secret",
                "expires_at": int(time.time() * 1000) + 600_000,
            }
        if path == "/api/agent/pair/status":
            return {"status": "paired" if self.paired else "pending", "device_id": self.device_id, "label": "Test Mac"}
        if path == "/api/agent/heartbeat":
            self.assert_token(token)
            return {"status": "online"}
        if path == "/api/agent/commands":
            self.assert_token(token)
            now = time.time()
            return {"commands": [{
                "schema": "gpa.host-agent-command/v1",
                "protocol_version": "1.0",
                "command_id": self.command_id,
                "command_type": "replay.prepare",
                "device_id": self.device_id,
                "replay_id": "browser-safe-example",
                "issued_at": now - 1,
                "expires_at": now + 240,
                "metadata": {
                    "title": "Browser-safe example",
                    "description": "Review a public page",
                    "supported_platforms": [platform.system().casefold()],
                    "required_capabilities": ["browser"],
                    "recorded_environment": {"system": {"name": platform.system().casefold()}},
                    "steps": [{"title": "Review the page", "success": "Evidence saved"}],
                },
            }]}
        if path.endswith("/result"):
            self.assert_token(token)
            self.reports.append(dict(body or {}))
            return {"status": body["status"]}
        raise AssertionError(f"unexpected request: {method} {path}")

    def assert_token(self, token):
        if token != "gpa_device_" + "b" * 43:
            raise AssertionError("device token missing")


class WebsiteAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.cloud = FakeCloud()
        client = WebsiteAgentClient(
            CloudServiceConfig(web_base_url="https://example.test"),
            transport=self.cloud,
        )
        self.credentials = AgentCredentialStore(root / "credentials.json")
        self.service = CloudAgentService(
            client=client,
            credentials=self.credentials,
            inbox_path=root / "inbox.json",
            workflow_storage=WorkflowStorage(root / "workflows"),
        )

    def tearDown(self):
        self.service.stop()
        self.temporary.cleanup()

    def test_pairing_preflight_and_local_import_are_end_to_end(self):
        pending = self.service.begin_pairing("Test Mac")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["web_base_url"], "https://example.test")
        self.assertNotIn("device_token", pending)

        self.cloud.paired = True
        active = self.service.poll_pairing()
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["inbox"][0]["status"], "compatible")
        self.assertEqual(self.cloud.reports[0]["status"], "compatible")

        accepted = self.service.accept(self.cloud.command_id)
        item = accepted["inbox"][0]
        self.assertEqual(item["status"], "accepted")
        workflow, _ = self.service.workflow_storage.load(item["workflow_id"])
        self.assertFalse(workflow.understanding["execution_ready"])
        self.assertEqual(workflow.steps[0].action_type, "manual_review")
        self.assertEqual(self.cloud.reports[-1]["status"], "accepted")

    def test_credentials_are_private_and_status_never_returns_secrets(self):
        status = self.service.begin_pairing("Test Mac")
        self.assertNotIn("device_secret", json.dumps(status))
        self.assertNotIn("device_token", json.dumps(status))
        if os.name != "nt":
            self.assertEqual(self.credentials.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
