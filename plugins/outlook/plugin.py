"""Outlook Classic trigger plugin.

Polls the user's Outlook Inbox via pywin32 COM and emits an event for each
new message. Tools to read/compose/search are provided separately by the
globally-registered outlook-mcp-server, which Claude can call directly.

Loop prevention: each seen EntryID is stored in state/outlook_seen.json so
the same message never triggers twice. On first run, the existing inbox is
snapshotted (no backlog replay).
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("relay.plugin.outlook")


async def setup(api) -> None:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError:
        log.error("pywin32 not installed — outlook plugin disabled")
        return

    poll_sec = int(api.config.get("poll_sec", 120))
    state_file = Path(__file__).parent.parent.parent / "state" / "outlook_seen.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)

    if state_file.exists():
        try:
            seen: set[str] = set(json.loads(state_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            seen = set()
    else:
        seen = set()

    first_run = not state_file.exists()

    def _save_seen():
        # keep only most-recent ~5000 IDs to avoid unbounded growth
        truncated = list(seen)[-5000:]
        state_file.write_text(json.dumps(truncated), encoding="utf-8")

    async def poll_loop():
        nonlocal first_run
        # COM must be initialized in the thread that uses it.
        loop = asyncio.get_running_loop()

        def poll_once() -> list[dict]:
            pythoncom.CoInitialize()
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                namespace = outlook.GetNamespace("MAPI")
                # 6 = olFolderInbox
                inbox = namespace.GetDefaultFolder(6)
                items = inbox.Items
                items.Sort("[ReceivedTime]", True)  # newest first
                new: list[dict] = []
                count = 0
                for item in items:
                    count += 1
                    if count > 50:  # don't scan forever
                        break
                    try:
                        entry_id = item.EntryID
                    except Exception:
                        continue
                    if entry_id in seen:
                        continue
                    seen.add(entry_id)
                    try:
                        sender = item.SenderEmailAddress or item.SenderName or "(unknown)"
                        subject = item.Subject or "(no subject)"
                        body = (item.Body or "")[:3000]
                    except Exception as e:
                        log.warning("Could not read mail %s: %s", entry_id, e)
                        continue
                    new.append({"from": sender, "subject": subject, "body": body})
                return new
            finally:
                pythoncom.CoUninitialize()

        while True:
            try:
                new_mails = await loop.run_in_executor(None, poll_once)
            except Exception:
                log.exception("Outlook poll failed")
                new_mails = []

            if first_run:
                # On first run, snapshot current inbox (don't replay backlog).
                first_run = False
                _save_seen()
                log.info("Outlook first-run snapshot: %d messages marked seen",
                         len(seen))
            else:
                for mail in new_mails:
                    await api.emit(
                        body=mail["body"],
                        metadata={
                            "from": mail["from"],
                            "subject": mail["subject"],
                        },
                    )
                if new_mails:
                    _save_seen()
                    log.info("Emitted %d new Outlook event(s)", len(new_mails))

            await asyncio.sleep(poll_sec)

    api.spawn(poll_loop())
    log.info("Outlook poll loop started (poll_sec=%d)", poll_sec)
