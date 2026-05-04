"""Signal Note-to-Self plugin via signal-cli.

Requires `signal-cli` installed and linked to the user's phone number. See
https://github.com/AsamK/signal-cli for setup.

- Trigger: polls `signal-cli -u <number> receive --json` for new messages
  where source == destination == own number (i.e. Note-to-Self).
- Tool: `signal_send_note_to_self(text)` posts via signal-cli.
- Loop prevention: outbound messages are tagged with a leading zero-width
  space (U+200B). Inbound messages starting with that marker are ignored.
"""
from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("relay.plugin.signal")

LOOP_MARKER = "​"  # zero-width space at start = bot-originated


async def setup(api) -> None:
    number = api.config.get("number", "").strip()
    poll_sec = int(api.config.get("poll_sec", 30))
    if not number:
        raise ValueError("signal.number not set in config.json")

    async def run_signal_cli(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "signal-cli", "-u", number, "--output=json", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", "timeout"
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    async def poll_loop():
        while True:
            try:
                rc, out, err = await run_signal_cli("receive", timeout=60.0)
                if rc != 0:
                    log.warning("signal-cli receive rc=%s: %s", rc, err[:500])
                else:
                    for line in out.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        envelope = ev.get("envelope") or ev
                        data_msg = envelope.get("dataMessage") or envelope.get("syncMessage", {}).get("sentMessage")
                        if not data_msg:
                            continue
                        source = envelope.get("source") or envelope.get("sourceNumber")
                        dest = data_msg.get("destination") or data_msg.get("destinationNumber")
                        text = data_msg.get("message") or ""
                        # Only Note-to-Self (source == dest == our number)
                        if source != number or dest != number:
                            continue
                        if not text:
                            continue
                        # Ignore our own outbound (loop prevention)
                        if text.startswith(LOOP_MARKER):
                            continue
                        await api.emit(
                            body=text,
                            metadata={"channel": "signal-note-to-self"},
                        )
                        log.info("Emitted Signal note-to-self event (%d chars)", len(text))
            except Exception:
                log.exception("Signal poll loop error")
            await asyncio.sleep(poll_sec)

    @api.tool("Send a message to the user's own Signal Note-to-Self conversation.")
    async def send_note_to_self(text: str) -> str:
        """Post a message to your own Signal chat (Note-to-Self).

        Args:
            text: the message body.
        """
        tagged = LOOP_MARKER + text
        rc, out, err = await run_signal_cli("send", number, "-m", tagged, timeout=20.0)
        if rc != 0:
            return f"ERROR: signal-cli rc={rc}: {err[:200]}"
        return "sent"

    api.spawn(poll_loop())
    log.info("Signal plugin ready (number=%s, poll_sec=%d)", number, poll_sec)
