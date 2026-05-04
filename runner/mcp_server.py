"""Aggregating MCP server.

Exposes every registered plugin tool as an MCP tool under the `relay` server.
Claude Code connects to this single MCP over local HTTP; all tool calls
flow through here and are dispatched to the owning plugin's handler.
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP

from .plugin_api import RegisteredTool

log = logging.getLogger("relay.mcp")


def build_mcp(tool_registry: list[RegisteredTool]) -> FastMCP:
    mcp = FastMCP("relay")

    for tool in tool_registry:
        # fastmcp infers the parameter schema from the handler's signature
        # and type hints; we override only the public name + description.
        mcp.tool(name=tool.name, description=tool.description)(tool.handler)
        log.debug("Registered MCP tool: %s", tool.name)

    return mcp


async def serve_mcp_http(mcp: FastMCP, host: str, port: int) -> None:
    """Run the MCP server on the given HTTP endpoint. Blocks forever."""
    await mcp.run_async(transport="http", host=host, port=port, show_banner=False)
