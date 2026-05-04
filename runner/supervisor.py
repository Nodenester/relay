"""Supervises the persistent Claude Code process.

Launches `claude --print --input-format stream-json --output-format stream-json`
once and keeps it alive. Feeds user messages in via stdin, reads assistant
events from stdout. Signals turn boundaries so the event dispatcher knows
when Claude is idle and ready for the next batch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("relay.supervisor")


@dataclass
class SupervisorConfig:
    agent_dir: Path
    session_id: str
    mcp_port: int
    model: str = "opus"
    additional_args: list[str] = field(default_factory=list)
    log_file: Path | None = None
    is_first_run: bool = True


class ClaudeSupervisor:
    def __init__(self, cfg: SupervisorConfig) -> None:
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.turn_complete = asyncio.Event()
        self.turn_complete.set()  # start ready
        self._out_writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        mcp_config = json.dumps({
            "mcpServers": {
                "relay": {
                    "type": "http",
                    "url": f"http://127.0.0.1:{self.cfg.mcp_port}/mcp",
                }
            }
        })
        # On first run, create a new session via --session-id. On subsequent
        # restarts (session already exists on disk), use --resume to continue.
        session_flag = (
            ["--session-id", self.cfg.session_id] if self.cfg.is_first_run
            else ["--resume", self.cfg.session_id]
        )
        args = [
            "claude",
            "--print",
            *session_flag,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--mcp-config", mcp_config,
            "--model", self.cfg.model,
            "--add-dir", str(self.cfg.agent_dir),
            "--verbose",
            *self.cfg.additional_args,
        ]
        log.info("Launching: %s", " ".join(args))
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self.cfg.agent_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def send_user_message(self, content: str) -> None:
        """Write a stream-json user message into Claude's stdin."""
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("Supervisor not started")
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content,
            },
        }
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self.turn_complete.clear()
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()
        log.debug("Sent user message: %d chars", len(content))

    async def read_stdout_loop(self) -> None:
        """Consume Claude's stdout forever, signal turn boundaries."""
        assert self.proc is not None and self.proc.stdout is not None
        log_path = self.cfg.log_file
        log_fh = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8")
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    log.warning("Claude stdout closed")
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                if log_fh:
                    log_fh.write(decoded + "\n")
                    log_fh.flush()
                # Parse stream-json events and detect turn boundary.
                try:
                    ev = json.loads(decoded)
                except json.JSONDecodeError:
                    continue
                ev_type = ev.get("type")
                if ev_type == "result":
                    log.info("Turn complete")
                    self.turn_complete.set()
        finally:
            if log_fh:
                log_fh.close()

    async def read_stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            log.warning("claude stderr: %s", line.decode("utf-8", errors="replace").rstrip())

    async def wait(self) -> int:
        assert self.proc is not None
        return await self.proc.wait()

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()


def load_or_create_session_id(state_dir: Path) -> tuple[str, bool]:
    """Return (session_id, is_first_run). First-run means "we just created it".

    Uses a sentinel file `session_initialized` to distinguish first run (where
    Claude has no transcript for this UUID yet) from subsequent runs (where
    we should --resume instead of --session-id).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / "session.txt"
    init_flag = state_dir / "session_initialized"
    if f.exists():
        sid = f.read_text(encoding="utf-8").strip()
        if sid:
            return sid, not init_flag.exists()
    sid = str(uuid.uuid4())
    f.write_text(sid, encoding="utf-8")
    return sid, True


def mark_session_initialized(state_dir: Path) -> None:
    """Call after a user message has been successfully sent. Subsequent runs
    will --resume instead of --session-id."""
    (state_dir / "session_initialized").touch(exist_ok=True)
