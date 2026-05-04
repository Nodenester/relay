"""Template plugin. Copy this folder to `plugins/<your-name>/` and edit.

A plugin is auto-discovered when:
  1. Its folder is under `plugins/` and does NOT start with `_` or `.`
  2. It has `plugin.json` with at least `{"name": "yourname", "module": "plugin"}`
  3. `config.json` contains `plugins.yourname.enabled = true`
  4. Its module defines `async def setup(api)` below.

The `api` object exposes:
  - `await api.emit(body, metadata)` — push an event (becomes a user message to Claude)
  - `@api.tool("desc")` — decorate an async function to expose as an MCP tool
  - `api.spawn(coro)` — launch a background task (e.g. a bot loop, a polling loop)
  - `api.config` — this plugin's section from config.json
"""
from __future__ import annotations

import asyncio


async def setup(api) -> None:
    # Example: a tool Claude can call
    @api.tool("Echo a message back. For testing.")
    async def echo(message: str) -> str:
        """Repeat the given message verbatim."""
        return message

    # Example: a periodic event emitter. Uncomment to try.
    # async def heartbeat_loop():
    #     while True:
    #         await asyncio.sleep(3600)
    #         await api.emit(body="hourly tick")
    # api.spawn(heartbeat_loop())
