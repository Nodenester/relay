"""GitHub trigger plugin.

Two trigger sources:

1. **Notifications** — polls `gh api notifications` so any repo where you're
   @-mentioned, review-requested, or assigned shows up. Account-wide.

2. **Watchers** — list of configurable per-repo polls (issues / runs / pulls),
   each with its own filter, poll cadence, template, and seen-set state file.

Both run in independent background tasks. A failure in one watcher does not
stop the others.

Config shape::

    "github": {
      "enabled": true,
      "notifications": { "enabled": true, "poll_sec": 300 },
      "watchers": [
        {
          "name": "queued-issues",
          "kind": "issues",
          "repo": "your-org/your-repo",
          "filter": "--label ai:queued --state open",
          "poll_sec": 120
        }
      ]
    }

Backward-compat: if `notifications` is missing, the legacy top-level `poll_sec`
(default 300) is used and notifications are enabled.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from . import watcher as watcher_mod  # type: ignore[import-not-found]

log = logging.getLogger("relay.plugin.github")

_NOTIF_STATE_NAME = "github_seen.json"


async def _fetch_notifications() -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", "notifications", "--paginate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh api notifications failed: %s",
                    stderr.decode("utf-8", errors="replace").rstrip())
        return []
    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        log.warning("Could not decode gh notifications output")
        return []
    if not isinstance(data, list):
        return []
    return data


async def _run_notifications(api, poll_sec: int, state_dir: Path) -> None:
    state_file = state_dir / _NOTIF_STATE_NAME
    state_file.parent.mkdir(parents=True, exist_ok=True)
    first_run = not state_file.exists()
    if state_file.exists():
        try:
            seen: set[str] = set(json.loads(state_file.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            seen = set()
    else:
        seen = set()

    def _save():
        truncated = list(seen)[-2000:]
        state_file.write_text(json.dumps(truncated), encoding="utf-8")

    log.info("GitHub poll loop started (poll_sec=%d)", poll_sec)
    while True:
        try:
            notifs = await _fetch_notifications()
        except Exception:
            log.exception("GitHub notifications poll failed")
            notifs = []

        new = [n for n in notifs if n.get("id") not in seen]
        for n in new:
            seen.add(n["id"])

        if first_run:
            first_run = False
            _save()
            log.info(
                "GitHub first-run snapshot: %d notifications marked seen",
                len(seen),
            )
        else:
            for n in new:
                repo = n.get("repository", {}).get("full_name", "?")
                subj = n.get("subject", {})
                title = subj.get("title", "")
                n_type = subj.get("type", "")
                reason = n.get("reason", "")
                url = subj.get("url") or subj.get("latest_comment_url") or ""
                body = (
                    f"{n_type}: {title}\n"
                    f"Repo: {repo}\n"
                    f"Reason: {reason}\n"
                    f"Url: {url}"
                )
                await api.emit(
                    body=body,
                    metadata={
                        "repo": repo,
                        "type": n_type,
                        "reason": reason,
                    },
                )
            if new:
                _save()
                log.info("Emitted %d new GitHub notification(s)", len(new))

        await asyncio.sleep(poll_sec)


def _resolve_notifications_cfg(plugin_cfg: dict) -> tuple[bool, int]:
    """Returns (enabled, poll_sec) for the notifications poller.

    Honors the explicit `notifications` block when present, otherwise falls
    back to the legacy top-level `poll_sec`.
    """
    notif = plugin_cfg.get("notifications")
    legacy_poll = int(plugin_cfg.get("poll_sec", 300))
    if isinstance(notif, dict):
        return bool(notif.get("enabled", True)), int(notif.get("poll_sec", legacy_poll))
    return True, legacy_poll


async def setup(api) -> None:
    state_dir = Path(__file__).parent.parent.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    notif_enabled, notif_poll = _resolve_notifications_cfg(api.config)
    if notif_enabled:
        api.spawn(_run_notifications(api, notif_poll, state_dir))
    else:
        log.info("GitHub notifications poller disabled by config")

    watchers, errors = watcher_mod.parse_watchers(api.config.get("watchers"))
    for err in errors:
        log.error("github config: %s", err)
    for cfg in watchers:
        api.spawn(watcher_mod.run_watcher(api, cfg, state_dir))
    if watchers:
        log.info(
            "Started %d github watcher(s): %s",
            len(watchers),
            [w.name for w in watchers],
        )
    elif not notif_enabled:
        log.warning(
            "github plugin enabled but neither notifications nor watchers "
            "are active — nothing to do"
        )
