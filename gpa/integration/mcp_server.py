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

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from gpa.storage.workflow import storage as wf_storage
from gpa.execution.executor import Executor

logger = logging.getLogger(__name__)

app = Server("gpa-mcp-server")


@app.list_tools()
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
                f"({len(workflow.steps)} steps)"
            ),
            inputSchema={
                "type": "object",
                "properties": properties,
                "required": required_vars,
            },
        ))
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Execute a workflow by name with the provided variable values."""
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

    executor = Executor(workflow, subgraphs, variables=arguments)
    result = executor.run()

    summary = {
        "success": result.success,
        "steps_run": result.n_steps,
        "steps_failed": result.n_failed,
        "error": result.error,
    }
    return [types.TextContent(type="text", text=json.dumps(summary, indent=2))]


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
