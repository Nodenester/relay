"""Plugin API — what a plugin's `setup(api)` function receives.

Plugins interact with the runner through this surface. They emit events via
`api.emit(...)`, register tools via `api.tool(...)`, and launch long-running
tasks via `api.spawn(...)`. Plugins never touch the runner's internals.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from .event_bus import Event, EventBus


@dataclass
class RegisteredTool:
    name: str            # namespaced e.g. "discord.send"
    description: str
    handler: Callable[..., Coroutine[Any, Any, Any]]
    plugin_name: str


class PluginAPI:
    """Handed to each plugin's setup() function. Scoped to the plugin name."""

    def __init__(
        self,
        plugin_name: str,
        event_bus: EventBus,
        config: dict,
        tool_registry: list[RegisteredTool],
        task_group: list[asyncio.Task],
    ) -> None:
        self.plugin_name = plugin_name
        self.config = config
        self._bus = event_bus
        self._tools = tool_registry
        self._tasks = task_group

    async def emit(self, body: str, metadata: dict | None = None) -> None:
        """Push an event into the queue. Shows up as a user message to Claude."""
        await self._bus.put(Event(
            source=self.plugin_name,
            body=body,
            metadata=metadata or {},
        ))

    def tool(self, description: str | None = None, name: str | None = None):
        """Decorator to register a plugin function as an MCP tool.

        Usage:
            @api.tool("Send a message to the watched Discord channel")
            async def send(message: str) -> str:
                ...

        The tool name exposed to Claude is namespaced as "<plugin>.<func>".
        """
        def decorator(func):
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"Tool {func.__name__} must be an async function "
                    f"(add `async` before `def`)"
                )
            tool_local_name = name or func.__name__
            full_name = f"{self.plugin_name}_{tool_local_name}"
            desc = description or (func.__doc__ or "").strip()
            self._tools.append(RegisteredTool(
                name=full_name,
                description=desc,
                handler=func,
                plugin_name=self.plugin_name,
            ))
            return func
        return decorator

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Launch a background task (e.g. a Discord bot loop)."""
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task
