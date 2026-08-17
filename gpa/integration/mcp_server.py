"""MCP Server: expose each GPA workflow as an MCP tool.

Each recorded workflow is registered as an MCP tool that accepts
the workflow's variables as arguments. Any MCP-capable AI agent
can then invoke a GPA workflow as a safe, bounded GUI action.

Usage:
    gpa mcp-serve         # start MCP server (stdio transport)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from gpa.runtime_config import RuntimeConfigurationError, env_bool
from gpa.storage.workflow import storage as wf_storage

logger = logging.getLogger(__name__)

MCP_EXECUTION_ENV = "GPA_MCP_ENABLE_EXECUTION"
DESKTOP_AUTOMATION_ENV = "GPA_ENABLE_DESKTOP_AUTOMATION"


def _execution_gate_error() -> str:
    try:
        mcp_enabled = env_bool(MCP_EXECUTION_ENV, False)
        desktop_enabled = env_bool(DESKTOP_AUTOMATION_ENV, False)
    except RuntimeConfigurationError as exc:
        return f"ERROR: Invalid GPA runtime configuration: {exc}"
    if not mcp_enabled:
        return (
            "ERROR: MCP Replay execution is disabled. Restart with "
            "`gpa mcp-serve --allow-execution` after reviewing the workflows."
        )
    if not desktop_enabled:
        return (
            "ERROR: Desktop automation is disabled. Explicitly enable "
            f"{DESKTOP_AUTOMATION_ENV}=1 before starting an execution-enabled MCP server."
        )
    return ""


def _executor_for(workflow, subgraphs, arguments):
    # Keep desktop drivers out of discovery-only MCP processes.
    from gpa.execution.executor import Executor

    return Executor(workflow, subgraphs, variables=arguments)


async def list_tools() -> list[types.Tool]:
    """Return one MCP tool per stored workflow."""
    tools = []
    for wf_info in wf_storage.list_workflows():
        wf_id = wf_info["id"]
        try:
            workflow, subgraphs = wf_storage.load(wf_id)
        except Exception:
            continue

        # Build JSON schema from workflow variables
        properties: dict[str, Any] = {}
        required_vars: list[str] = []
        for var in workflow.variables:
            properties[var.name] = {
                "type": "string",
                "description": var.description or f"Value for {var.name}",
                "default": var.default_value,
            }

        tools.append(types.Tool(
            name=workflow.workflow_name,
            description=(
                f"{workflow.workflow_title}: {workflow.description} "
                f"({len(workflow.steps)} steps). Execution requires explicit local opt-in."
            ),
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": required_vars,
            },
        ))
    return tools


async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Execute a workflow by name with the provided variable values."""
    gate_error = _execution_gate_error()
    if gate_error:
        return [types.TextContent(type="text", text=gate_error)]

    # Find workflow by name
    matched_id = None
    for wf_info in wf_storage.list_workflows():
        if wf_info["name"] == name:
            matched_id = wf_info["id"]
            break

    if matched_id is None:
        return [types.TextContent(type="text", text=f"ERROR: Workflow '{name}' not found.")]

    try:
        workflow, subgraphs = wf_storage.load(matched_id)
    except Exception as e:
        return [types.TextContent(type="text", text=f"ERROR loading workflow: {e}")]

    executor = _executor_for(workflow, subgraphs, dict(arguments or {}))
    result = executor.run()

    summary = {
        "success": result.success,
        "steps_run": result.n_steps,
        "steps_failed": result.n_failed,
        "error": result.error,
    }
    return [types.TextContent(type="text", text=json.dumps(summary, indent=2))]


async def _handle_list_tools(_context, _params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=await list_tools())


async def _handle_call_tool(_context, params) -> types.CallToolResult:
    content = await call_tool(params.name, dict(params.arguments or {}))
    return types.CallToolResult(content=content)


app = Server(
    "gpa-mcp-server",
    version="0.1.0",
    instructions=(
        "GPA exposes locally recorded workflows. Tool discovery is safe by default; "
        "execution requires explicit local opt-in and enabled desktop automation."
    ),
    on_list_tools=_handle_list_tools,
    on_call_tool=_handle_call_tool,
)


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


__all__ = [
    "DESKTOP_AUTOMATION_ENV",
    "MCP_EXECUTION_ENV",
    "app",
    "call_tool",
    "list_tools",
    "run_server",
]
