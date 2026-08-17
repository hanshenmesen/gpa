import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE:
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    import gpa.integration.mcp_server as mcp_server
    import gpa.storage.workflow as workflow_module
    from gpa.storage.workflow import Workflow, WorkflowStep, WorkflowVariable


@unittest.skipUnless(MCP_AVAILABLE, "mcp optional dependency is unavailable")
class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.old_mcp_env = os.environ.get(mcp_server.MCP_EXECUTION_ENV)
        self.old_desktop_env = os.environ.get(mcp_server.DESKTOP_AUTOMATION_ENV)
        self.old_storage = mcp_server.wf_storage
        self.old_executor_factory = mcp_server._executor_for
        self.workflow = Workflow(
            "mcp_demo",
            "mcp_demo",
            "MCP Demo",
            "A bounded MCP workflow",
            variables=[WorkflowVariable("message", "hello", "Text to enter")],
            steps=[WorkflowStep(1, "Type message", action_type="type", value="{{message}}")],
        )

        workflow = self.workflow

        class Storage:
            def list_workflows(self):
                return [{"id": workflow.workflow_id, "name": workflow.workflow_name}]

            def load(self, workflow_id):
                if workflow_id != workflow.workflow_id:
                    raise FileNotFoundError(workflow_id)
                return workflow, {}

        mcp_server.wf_storage = Storage()

    def tearDown(self):
        if self.old_mcp_env is None:
            os.environ.pop(mcp_server.MCP_EXECUTION_ENV, None)
        else:
            os.environ[mcp_server.MCP_EXECUTION_ENV] = self.old_mcp_env
        if self.old_desktop_env is None:
            os.environ.pop(mcp_server.DESKTOP_AUTOMATION_ENV, None)
        else:
            os.environ[mcp_server.DESKTOP_AUTOMATION_ENV] = self.old_desktop_env
        mcp_server.wf_storage = self.old_storage
        mcp_server._executor_for = self.old_executor_factory

    def test_discovery_lists_schema_without_enabling_execution(self):
        tools = asyncio.run(mcp_server.list_tools())

        self.assertEqual([tool.name for tool in tools], ["mcp_demo"])
        self.assertIn("explicit local opt-in", tools[0].description)
        self.assertEqual(tools[0].input_schema["properties"]["message"]["default"], "hello")

        result = asyncio.run(mcp_server._handle_list_tools(None, None))
        self.assertEqual([tool.name for tool in result.tools], ["mcp_demo"])

    def test_call_is_blocked_by_default(self):
        os.environ.pop(mcp_server.MCP_EXECUTION_ENV, None)
        os.environ[mcp_server.DESKTOP_AUTOMATION_ENV] = "1"

        response = asyncio.run(mcp_server.call_tool("mcp_demo", {"message": "safe"}))

        self.assertIn("MCP Replay execution is disabled", response[0].text)

    def test_call_requires_both_explicit_gates(self):
        os.environ[mcp_server.MCP_EXECUTION_ENV] = "1"
        os.environ[mcp_server.DESKTOP_AUTOMATION_ENV] = "0"

        response = asyncio.run(mcp_server.call_tool("mcp_demo", {}))

        self.assertIn("Desktop automation is disabled", response[0].text)

    def test_explicitly_enabled_call_uses_injected_executor(self):
        os.environ[mcp_server.MCP_EXECUTION_ENV] = "1"
        os.environ[mcp_server.DESKTOP_AUTOMATION_ENV] = "1"
        captured = {}

        class Executor:
            def run(self):
                return SimpleNamespace(success=True, n_steps=1, n_failed=0, error="")

        def executor_for(workflow, subgraphs, arguments):
            captured.update({
                "workflow": workflow.workflow_id,
                "subgraphs": subgraphs,
                "arguments": arguments,
            })
            return Executor()

        mcp_server._executor_for = executor_for
        response = asyncio.run(mcp_server.call_tool("mcp_demo", {"message": "confirmed"}))

        self.assertEqual(captured["arguments"], {"message": "confirmed"})
        self.assertTrue(json_from_text(response[0].text)["success"])

    def test_real_stdio_protocol_discovers_tool_and_blocks_default_execution(self):
        async def exercise(storage_root):
            environment = dict(os.environ)
            environment.update({
                "GPA_STORAGE_DIR": str(storage_root),
                "GPA_ENABLE_DESKTOP_AUTOMATION": "0",
                "GPA_MCP_ENABLE_EXECUTION": "1",  # CLI must override this without the flag.
            })
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "gpa.integration.cli", "mcp-serve"],
                env=environment,
                cwd=str(Path(__file__).resolve().parents[1]),
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    blocked = await session.call_tool("mcp_demo", {"message": "blocked"})
                    return initialized, tools, blocked

        with tempfile.TemporaryDirectory() as directory:
            storage_root = Path(directory)
            old_workflows_dir = workflow_module.WORKFLOWS_DIR
            workflow_module.WORKFLOWS_DIR = storage_root / "workflows"
            try:
                workflow_module.WorkflowStorage().save(self.workflow, {})
            finally:
                workflow_module.WORKFLOWS_DIR = old_workflows_dir

            initialized, tools, blocked = anyio.run(exercise, storage_root)

        self.assertEqual(initialized.server_info.name, "gpa-mcp-server")
        self.assertEqual([tool.name for tool in tools.tools], ["mcp_demo"])
        self.assertIn("MCP Replay execution is disabled", blocked.content[0].text)


def json_from_text(value):
    import json

    return json.loads(value)


if __name__ == "__main__":
    unittest.main()
