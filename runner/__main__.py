"""relay entry point. Launched by install.ps1 via Task Scheduler."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from .event_bus import EventBus, Event
from .mcp_server import build_mcp, send_channel_event, serve_mcp_http
from .plugin_api import RegisteredTool
from .plugin_loader import load_plugins
from .supervisor import (
    ClaudeSupervisor,
    SupervisorConfig,
    load_or_create_session_id,
    mark_session_initialized,
)

log = logging.getLogger("relay")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def event_dispatcher(bus: EventBus) -> None:
    """Drain plugin events and inject them as channel notifications.

    Each plugin event becomes one `notifications/claude/channel` push.
    Claude receives them as `<channel source="...">` user turns inside
    its always-running interactive session.

    No turn-boundary tracking is needed in interactive mode — Claude
    queues incoming channel events and processes them as turns finish.
    """
    while True:
        event = await bus.get()
        rest = bus.drain_nowait()
        events = [event, *rest]

        for ev in events:
            ok = await send_channel_event(
                text=ev.body,
                source=ev.source,
                chat_id=ev.source,
                extra_meta=dict(ev.metadata or {}),
            )
            if not ok:
                # No live MCP session yet — requeue and back off briefly.
                await bus.put(ev)
                await asyncio.sleep(2.0)
                break


async def run(agent_dir: Path) -> int:
    config_path = agent_dir / "config.json"
    if not config_path.exists():
        example = agent_dir / "config.json.example"
        if example.exists():
            log.error("config.json missing. Copy from config.json.example and fill in.")
        else:
            log.error("config.json missing and no example to copy.")
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    setup_logging(config.get("runner", {}).get("log_level", "INFO"))

    state_dir = agent_dir / "state"
    session_id, is_first_run = load_or_create_session_id(state_dir)
    log.info("Session ID: %s (%s)", session_id,
             "NEW" if is_first_run else "resuming")

    # Build event bus, tool registry, task group
    bus = EventBus()
    tool_registry: list[RegisteredTool] = []
    plugin_tasks: list[asyncio.Task] = []

    plugins_dir = agent_dir / "plugins"
    loaded = await load_plugins(
        plugins_dir=plugins_dir,
        config=config,
        event_bus=bus,
        tool_registry=tool_registry,
        task_group=plugin_tasks,
    )
    log.info("Loaded %d plugin(s): %s", len(loaded), [p.name for p in loaded])
    log.info("Registered %d tool(s): %s", len(tool_registry),
             [t.name for t in tool_registry])

    # Build the MCP server (now also serving as the channel — see mcp_server.py)
    mcp = build_mcp(tool_registry)
    mcp_port = int(config.get("runner", {}).get("mcp_port", 9123))
    mcp_task = asyncio.create_task(serve_mcp_http(mcp, "127.0.0.1", mcp_port))

    # Give the MCP a moment to bind.
    await asyncio.sleep(1.0)

    # Spawn Claude interactively (PTY-wrapped, no visible window).
    runner_cfg = config.get("runner", {})
    claude_cfg = config.get("claude", {})
    remote_name = runner_cfg.get("remote_control_name")
    if remote_name is None:
        # Default: derive a sensible name from the agent dir.
        remote_name = f"relay-{agent_dir.name}"
    supervisor = ClaudeSupervisor(SupervisorConfig(
        agent_dir=agent_dir,
        session_id=session_id,
        mcp_port=mcp_port,
        model=claude_cfg.get("model", "opus"),
        remote_control_name=remote_name,
        additional_args=claude_cfg.get("additional_args", []),
        log_file=state_dir / "logs" / "claude_pty.log",
        is_first_run=is_first_run,
    ))
    await supervisor.start()
    log.info("Claude supervisor started (pid=%s, remote-control name=%r)",
             supervisor.proc.pid if supervisor.proc else "?", remote_name)
    mark_session_initialized(state_dir)

    dispatcher_task = asyncio.create_task(event_dispatcher(bus))

    # Handle shutdown gracefully
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # Shutdown only fires on:
    # - explicit stop_event (SIGINT/SIGTERM)
    # - Claude PTY closed (supervisor died)
    # - MCP server died
    # - dispatcher crashed
    # Plugin tasks that complete on their own (e.g. one-shot triggers)
    # should NOT shut down the runner.
    supervisor_wait = asyncio.create_task(supervisor.wait())
    critical_tasks = [dispatcher_task, supervisor_wait, mcp_task]
    stop_watcher = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        [stop_watcher, *critical_tasks],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in done:
        name = t.get_name() if hasattr(t, "get_name") else "?"
        if t.cancelled():
            continue
        exc = t.exception()
        if exc:
            log.error("Task %s failed: %r", name, exc)

    log.info("Shutting down...")
    await supervisor.stop()
    for t in pending:
        t.cancel()
    for t in plugin_tasks:
        t.cancel()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="relay")
    parser.add_argument(
        "--agent-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Folder that contains CLAUDE.md, config.json, plugins/",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.agent_dir))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
