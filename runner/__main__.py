"""relay entry point. Launched by install.ps1 via Task Scheduler."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from .event_bus import EventBus
from .mcp_server import build_mcp, serve_mcp_http
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


async def event_dispatcher(supervisor: ClaudeSupervisor, bus: EventBus) -> None:
    """Wait for turn boundaries, then flush queued events as a single user message."""
    while True:
        first = await bus.get()
        rest = bus.drain_nowait()
        pending = [first, *rest]
        # Wait for Claude to be idle before sending.
        await supervisor.turn_complete.wait()
        if len(pending) == 1:
            content = pending[0].to_user_message()
        else:
            content = "\n\n---\n\n".join(ev.to_user_message() for ev in pending)
            content = (
                f"[BATCH: {len(pending)} events arrived together]\n\n"
                + content
            )
        try:
            await supervisor.send_user_message(content)
        except Exception:
            log.exception("Failed to send user message; requeueing")
            for ev in pending:
                await bus.put(ev)
            await asyncio.sleep(2.0)


async def run(agent_dir: Path) -> int:
    # Load config
    config_path = agent_dir / "config.json"
    if not config_path.exists():
        example = agent_dir / "config.json.example"
        if example.exists():
            log.error("config.json missing. Copy from config.json.example and fill in.")
        else:
            log.error("config.json missing and no example to copy.")
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))
    setup_logging(config.get("runner", {}).get("log_level", "INFO"))

    state_dir = agent_dir / "state"
    session_id, is_first_run = load_or_create_session_id(state_dir)
    log.info("Session ID: %s (%s)", session_id,
             "NEW" if is_first_run else "resuming")

    # Build event bus, tool registry, task group
    bus = EventBus()
    tool_registry: list[RegisteredTool] = []
    plugin_tasks: list[asyncio.Task] = []

    # Load plugins
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

    # Build MCP
    mcp = build_mcp(tool_registry)
    mcp_port = int(config.get("runner", {}).get("mcp_port", 9123))
    mcp_task = asyncio.create_task(serve_mcp_http(mcp, "127.0.0.1", mcp_port))

    # Give the MCP a moment to bind.
    await asyncio.sleep(1.0)

    # Spawn Claude
    supervisor = ClaudeSupervisor(SupervisorConfig(
        agent_dir=agent_dir,
        session_id=session_id,
        mcp_port=mcp_port,
        model=config.get("claude", {}).get("model", "opus"),
        additional_args=config.get("claude", {}).get("additional_args", []),
        log_file=state_dir / "logs" / "claude_stream.jsonl",
        is_first_run=is_first_run,
    ))
    await supervisor.start()
    log.info("Claude supervisor started (pid=%s)", supervisor.proc.pid)
    # After successful start, mark session as initialized so the next run
    # uses --resume.
    mark_session_initialized(state_dir)

    dispatcher_task = asyncio.create_task(event_dispatcher(supervisor, bus))
    stdout_task = asyncio.create_task(supervisor.read_stdout_loop())
    stderr_task = asyncio.create_task(supervisor.read_stderr_loop())

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
    # - Claude stdout closed (supervisor died)
    # - MCP server died
    # - dispatcher crashed
    # Plugin tasks that complete on their own (e.g. one-shot triggers)
    # should NOT shut down the runner.
    critical_tasks = [dispatcher_task, stdout_task, stderr_task, mcp_task]
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
