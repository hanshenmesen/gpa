import unittest
import uuid

from gpa.cloud.agent_protocol import (
    AgentProtocolError,
    build_agent_hello,
    parse_cloud_command,
)


class CloudAgentProtocolTests(unittest.TestCase):
    def command(self, **updates):
        value = {
            "schema": "gpa.host-agent-command/v1",
            "protocol_version": "1.0",
            "command_id": str(uuid.uuid4()),
            "command_type": "replay.inspect",
            "device_id": "device_local",
            "replay_id": "replay_safe",
            "issued_at": 1000,
            "expires_at": 1060,
            "metadata": {"origin": "web"},
        }
        value.update(updates)
        return value

    def test_read_only_replay_command_is_accepted(self):
        command = parse_cloud_command(
            self.command(), expected_device_id="device_local", now=1030
        )
        self.assertEqual(command.command_type, "replay.inspect")
        self.assertEqual(command.metadata, {"origin": "web"})

    def test_desktop_command_requires_local_approval(self):
        with self.assertRaisesRegex(AgentProtocolError, "local approval"):
            parse_cloud_command(
                self.command(command_type="replay.run"),
                expected_device_id="device_local",
                now=1030,
            )
        command = parse_cloud_command(
            self.command(
                command_type="replay.run",
                local_approval_id="approval_0123456789abcdef",
            ),
            expected_device_id="device_local",
            now=1030,
        )
        self.assertEqual(command.local_approval_id, "approval_0123456789abcdef")

    def test_command_is_bound_to_device_and_short_expiry(self):
        with self.assertRaisesRegex(AgentProtocolError, "another device"):
            parse_cloud_command(
                self.command(), expected_device_id="device_other", now=1030
            )
        with self.assertRaisesRegex(AgentProtocolError, "expired"):
            parse_cloud_command(
                self.command(expires_at=1020), expected_device_id="device_local", now=1030
            )
        with self.assertRaisesRegex(AgentProtocolError, "expiry"):
            parse_cloud_command(
                self.command(expires_at=2000), expected_device_id="device_local", now=1030
            )

    def test_unknown_commands_fail_closed(self):
        with self.assertRaisesRegex(AgentProtocolError, "Unsupported cloud command"):
            parse_cloud_command(
                self.command(command_type="shell.execute"),
                expected_device_id="device_local",
                now=1030,
            )

    def test_agent_hello_exposes_capabilities_without_credentials(self):
        hello = build_agent_hello(
            device_id="device_local",
            platform="darwin",
            platform_release="25.6",
            architecture="arm64",
            capabilities={"recording": True, "replay": True},
            permissions={"accessibility": "granted", "screen": "unknown"},
        )
        self.assertEqual(hello["schema"], "gpa.host-agent-hello/v1")
        self.assertTrue(hello["capabilities"]["recording"])
        self.assertNotIn("token", hello)


if __name__ == "__main__":
    unittest.main()
