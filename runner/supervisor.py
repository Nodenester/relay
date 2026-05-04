"""Supervises the persistent Claude Code process.

Launches `claude` interactively in a hidden ConPTY (no visible window) so:

- The session is **interactive**, which is what `/remote-control` and the
  `--channels` mechanism require.
- We pass `--channels server:relay` so the supervisor's existing MCP
  server doubles as the channel — every plugin event the supervisor
  emits arrives in the session as a `<channel source="relay">` user
  turn. No separate channel plugin, no extra port.
- We pass `--remote-control <name>` so the session is reachable from
  `claude.ai/code` from any device. No bootstrap typing required.

Output from the PTY is tee'd to a log file so we can debug without
showing a window. We never type INTO the PTY; all events arrive via the
channel mechanism.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from winpty import PtyProcess

log = logging.getLogger("relay.supervisor")


@dataclass
class SupervisorConfig:
    agent_dir: Path
    session_id: str
    mcp_port: int
    model: str = "opus"
    remote_control_name: str | None = None
    additional_args: list[str] = field(default_factory=list)
    log_file: Path | None = None
    is_first_run: bool = True
    pty_cols: int = 200
    pty_rows: int = 50


class ClaudeSupervisor:
    def __init__(self, cfg: SupervisorConfig) -> None:
        self.cfg = cfg
        self.proc: PtyProcess | None = None
        self._read_task: asyncio.Task | None = None
        self._log_fh = None
        self._stopped = asyncio.Event()

    def _build_args(self) -> list[str]:
        mcp_config = json.dumps({
            "mcpServers": {
                "relay": {
                    "type": "http",
                    "url": f"http://127.0.0.1:{self.cfg.mcp_port}/mcp",
                }
            }
        })
        # On first run, create a new session via --session-id. On subsequent
        # restarts the session already exists on disk, so use --resume.
        session_flag = (
            ["--session-id", self.cfg.session_id] if self.cfg.is_first_run
            else ["--resume", self.cfg.session_id]
        )
        args = [
            "claude",
            *session_flag,
            "--mcp-config", mcp_config,
            "--dangerously-load-development-channels", "server:relay",
            "--dangerously-skip-permissions",
            "--model", self.cfg.model,
            "--add-dir", str(self.cfg.agent_dir),
        ]
        if self.cfg.remote_control_name:
            args.extend(["--remote-control", self.cfg.remote_control_name])
        args.extend(self.cfg.additional_args)
        return args

    async def start(self) -> None:
        args = self._build_args()
        log.info("Launching (interactive PTY): %s", " ".join(args))

        loop = asyncio.get_event_loop()
        self.proc = await loop.run_in_executor(
            None,
            lambda: PtyProcess.spawn(
                args,
                dimensions=(self.cfg.pty_rows, self.cfg.pty_cols),
                cwd=str(self.cfg.agent_dir),
            ),
        )
        log.info("Claude PTY spawned (pid=%s, cols=%d rows=%d)",
                 self.proc.pid, self.cfg.pty_cols, self.cfg.pty_rows)

        if self.cfg.log_file is not None:
            self.cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(self.cfg.log_file, "ab", buffering=0)

        self._read_task = asyncio.create_task(self._read_loop())
        # Dismiss the "Loading development channels" consent prompt by
        # pressing Enter on option 1 ("I am using this for local
        # development"), which is the default selection.
        # Same key dismisses the workspace-trust dialog if it appears.
        self._consent_task = asyncio.create_task(self._auto_consent())

    async def _auto_consent(self) -> None:
        """Send Enter at startup to dismiss interactive consent prompts.

        Done blindly twice with a small delay because we may need to clear:
          1) the development-channels warning
          2) any workspace-trust dialog that follows
        Sending \\r when there's no prompt is a no-op in interactive Claude.
        """
        if self.proc is None:
            return
        loop = asyncio.get_event_loop()
        for delay in (3.0, 5.0):
            await asyncio.sleep(delay)
            if self.proc is None or not self.proc.isalive():
                return
            try:
                await loop.run_in_executor(None, lambda: self.proc.write("\r"))
                log.info("Sent Enter to dismiss any startup prompt")
            except Exception:
                log.exception("Auto-consent write failed")

    async def _read_loop(self) -> None:
        loop = asyncio.get_event_loop()
        assert self.proc is not None
        while True:
            try:
                # PtyProcess.read is blocking; run in executor.
                # Use a small read so we drain promptly.
                data = await loop.run_in_executor(
                    None, self._safe_read, 4096
                )
            except Exception:
                log.exception("PTY read crashed")
                break
            if data is None:
                # EOF — process exited.
                log.info("PTY EOF — claude process exited")
                break
            if not data:
                # No data right now; brief breather.
                await asyncio.sleep(0.05)
                continue
            if isinstance(data, str):
                blob = data.encode("utf-8", errors="replace")
            else:
                blob = data
            if self._log_fh is not None:
                try:
                    self._log_fh.write(blob)
                except Exception:
                    log.exception("PTY log write failed")
        self._stopped.set()

    def _safe_read(self, n: int):
        if self.proc is None or not self.proc.isalive():
            return None
        try:
            return self.proc.read(n)
        except EOFError:
            return None
        except Exception as e:
            # pywinpty raises a generic exception when pipe is closed.
            msg = str(e).lower()
            if "exited" in msg or "closed" in msg or "broken" in msg:
                return None
            raise

    async def wait(self) -> int:
        if self.proc is None:
            return -1
        await self._stopped.wait()
        return self.proc.exitstatus or 0

    async def stop(self) -> None:
        if self.proc is not None and self.proc.isalive():
            try:
                # Give claude a chance to flush; force-terminate after a beat.
                self.proc.terminate(force=True)
            except Exception:
                log.exception("PTY terminate failed")
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


def load_or_create_session_id(state_dir: Path) -> tuple[str, bool]:
    """Return (session_id, is_first_run). First-run means "we just created it".

    Uses a sentinel file `session_initialized` to distinguish first run
    (where Claude has no transcript for this UUID yet) from subsequent
    runs (where we should --resume instead of --session-id).
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
    """Call after the supervisor has started successfully. Subsequent
    runs will --resume instead of --session-id."""
    (state_dir / "session_initialized").touch(exist_ok=True)
