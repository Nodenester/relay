"""Aggregating MCP server.

Exposes every registered plugin tool as an MCP tool under the `relay`
server. Claude Code connects to this single MCP over local HTTP; all
tool calls flow through here and are dispatched to the owning plugin's
handler.

Also declares the `experimental.claude/channel` capability so the same
server doubles as a Claude Code channel. The supervisor's dispatcher
calls `send_channel_event(text, meta)` to inject events into the
running session as `<channel source="relay">` user turns. Same MCP
connection, no extra port, no extra plugin.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastmcp import FastMCP
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from .plugin_api import RegisteredTool

log = logging.getLogger("relay.mcp")


# --- Capture the active server session via monkey-patch -----------------
#
# FastMCP creates a ServerSession per connecting client (Claude Code). To
# push notifications/claude/channel events to that session from outside
# any request context (i.e. when a relay plugin emits an event), we need
# a reference to the session. We grab it at construction time.
#
# This is safe for relay's single-client topology — only one Claude
# session connects to each relay agent's MCP server. If multiple clients
# ever connect, the most recently constructed session wins.

_active_session: ServerSession | None = None
_orig_session_init = ServerSession.__init__


def _capturing_init(self, *args, **kwargs):
    global _active_session
    _orig_session_init(self, *args, **kwargs)
    _active_session = self
    log.info("MCP session connected — channel notifications now available")


ServerSession.__init__ = _capturing_init


# --- Patch in the experimental.claude/channel capability ----------------
#
# FastMCP doesn't expose a way to declare experimental capabilities. The
# underlying lowlevel.Server's create_initialization_options() does — so
# we wrap that method on the FastMCP instance's internal server.

def _inject_channel_capability(mcp: FastMCP) -> None:
    lowlevel = mcp._mcp_server
    orig = lowlevel.create_initialization_options

    def patched(notification_options=None, experimental_capabilities=None):
        caps = dict(experimental_capabilities or {})
        caps.setdefault("claude/channel", {})
        return orig(notification_options=notification_options,
                    experimental_capabilities=caps)

    lowlevel.create_initialization_options = patched


def build_mcp(tool_registry: list[RegisteredTool]) -> FastMCP:
    mcp = FastMCP(
        "relay",
        instructions=(
            "External events arrive via the channel mechanism as "
            "`<channel source=\"relay\" chat_id=\"...\" message_id=\"...\">` "
            "user turns. Treat each like a user speaking. Use the registered "
            "tools to do work."
        ),
    )
    _inject_channel_capability(mcp)

    for tool in tool_registry:
        # fastmcp infers the parameter schema from the handler's signature
        # and type hints; we override only the public name + description.
        mcp.tool(name=tool.name, description=tool.description)(tool.handler)
        log.debug("Registered MCP tool: %s", tool.name)

    return mcp


async def serve_mcp_http(mcp: FastMCP, host: str, port: int) -> None:
    """Run the MCP server on the given HTTP endpoint. Blocks forever."""
    await mcp.run_async(transport="http", host=host, port=port, show_banner=False)


async def send_channel_event(
    text: str,
    source: str = "relay",
    chat_id: str = "default",
    message_id: str | None = None,
    extra_meta: dict | None = None,
) -> bool:
    """Push a `notifications/claude/channel` notification into the live
    Claude session.

    Returns True if pushed, False if no session is connected yet.
    """
    if _active_session is None:
        log.warning("send_channel_event: no active session yet, dropping event")
        return False

    if message_id is None:
        message_id = f"m{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    meta = {
        "chat_id": chat_id,
        "user": source,
        "ts": datetime.now(timezone.utc).isoformat(),
        "message_id": message_id,
    }
    if extra_meta:
        meta.update(extra_meta)

    notif = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": text, "meta": meta},
    )
    msg = SessionMessage(message=JSONRPCMessage(notif))
    try:
        await _active_session._write_stream.send(msg)
        return True
    except Exception:
        log.exception("send_channel_event failed")
        return False
