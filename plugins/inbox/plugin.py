"""Inbox plugin — tails a JSONL file and emits each new line as an event.

Use case: the user (or another local process) wants to inject ad-hoc
messages into the running session without going through a network plugin.
Append one JSON object per line to `state/inbox.jsonl`:

    {"body": "hello", "source": "user"}
    {"body": "another message", "source": "ops", "metadata": {"prio": "high"}}

The plugin polls the file (default every 2 seconds), emits new lines as
events, and tracks the byte offset in `state/inbox.offset` so it doesn't
re-emit on restart.

Config::

    "inbox": {
      "enabled": true,
      "path": "state/inbox.jsonl",   # relative to agent dir
      "poll_sec": 2,
      "default_source": "inbox"      # used if a line lacks "source"
    }

Each line should be valid JSON. Lines that aren't are emitted with
`source=inbox.malformed` so the agent at least sees them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("relay.plugin.inbox")


async def setup(api) -> None:
    cfg = api.config
    poll_sec = float(cfg.get("poll_sec", 2))
    default_source = str(cfg.get("default_source", "inbox"))

    # Resolve path relative to the agent dir (= plugin's parent.parent.parent).
    rel = cfg.get("path", "state/inbox.jsonl")
    agent_dir = Path(__file__).parent.parent.parent
    inbox_path = (agent_dir / rel).resolve()
    offset_path = inbox_path.with_suffix(inbox_path.suffix + ".offset")

    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if not inbox_path.exists():
        inbox_path.touch()

    # Restore offset; if the file was truncated, reset to 0.
    offset = 0
    if offset_path.exists():
        try:
            offset = int(offset_path.read_text().strip() or "0")
        except (ValueError, OSError):
            offset = 0
    try:
        size = inbox_path.stat().st_size
    except OSError:
        size = 0
    if offset > size:
        log.info("Inbox truncated (offset %d > size %d); resetting", offset, size)
        offset = 0

    log.info(
        "Inbox plugin started (path=%s poll=%ss starting_offset=%d)",
        inbox_path, poll_sec, offset,
    )

    async def loop() -> None:
        nonlocal offset
        while True:
            try:
                cur_size = inbox_path.stat().st_size
            except OSError:
                await asyncio.sleep(poll_sec)
                continue
            if cur_size < offset:
                # File rotated/truncated.
                log.info("Inbox truncated; resetting offset to 0")
                offset = 0
            if cur_size > offset:
                try:
                    with open(inbox_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(offset)
                        new_text = f.read()
                        offset = f.tell()
                except OSError:
                    log.exception("Inbox: read failed")
                    await asyncio.sleep(poll_sec)
                    continue

                for raw_line in new_text.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        await api.emit(
                            body=line,
                            metadata={"source_override": "inbox.malformed"},
                        )
                        continue
                    if not isinstance(obj, dict):
                        await api.emit(body=str(obj), metadata={})
                        continue
                    body = str(obj.get("body", ""))
                    if not body:
                        continue
                    src = str(obj.get("source", default_source))
                    md = obj.get("metadata") or {}
                    md = {**md, "via": "inbox"}
                    if src != default_source:
                        md["source"] = src
                    await api.emit(body=body, metadata=md)

                try:
                    offset_path.write_text(str(offset), encoding="utf-8")
                except OSError:
                    log.exception("Inbox: failed to persist offset")

            await asyncio.sleep(poll_sec)

    api.spawn(loop())
