"""Generic shell-command poller plugin.

Runs arbitrary shell commands on a schedule and emits an event for every
new item produced. Two modes:

- **json** (default) — command is expected to print a JSON array. Each
  element is one item. Items are deduplicated by `id_field` (a dotted
  JSONPath like `metadata.uid`).
- **lines** — each non-empty stdout line is one item. Items are
  deduplicated by line content (after stripping whitespace).

Each target is independent: own state file, own poll cadence, own
template, own optional regex filter (`match`).

Config shape::

    "poller": {
      "enabled": true,
      "targets": [
        {
          "name": "arenden",
          "command": "gh issue list --repo X/Y --json number,title,body,labels",
          "shell": false,
          "mode": "json",
          "id_field": "number",
          "poll_sec": 120,
          "template": "Issue #{number}: {title}\\n{body}",
          "match": null,
          "timeout_sec": 60
        }
      ]
    }

`shell: true` runs the command via the system shell so pipes/redirects
work. `shell: false` (default) tokenises with shlex — safer.

Templates support dotted access via `_DotDict`: `{metadata.namespace}`
resolves nested fields. Missing keys render as the literal placeholder.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runner.template import render_template

log = logging.getLogger("relay.plugin.poller")

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")
_BODY_MAX = 4000


@dataclass
class TargetConfig:
    name: str
    command: str
    shell: bool = False
    mode: str = "json"
    id_field: str = ""
    poll_sec: int = 300
    template: str = "{_raw}"
    match: str | None = None
    timeout_sec: int = 60

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("target missing 'name'")
        elif _NAME_SAFE.search(self.name):
            errors.append(f"target '{self.name}': name must match [a-zA-Z0-9_-]+")
        if not self.command:
            errors.append(f"target '{self.name}': missing 'command'")
        if self.mode not in ("json", "lines"):
            errors.append(
                f"target '{self.name}': mode must be 'json' or 'lines' "
                f"(got {self.mode!r})"
            )
        if self.mode == "json" and not self.id_field:
            errors.append(
                f"target '{self.name}': mode=json requires 'id_field'"
            )
        if self.poll_sec < 5:
            errors.append(
                f"target '{self.name}': poll_sec must be >= 5 "
                f"(got {self.poll_sec})"
            )
        if self.match is not None:
            try:
                re.compile(self.match)
            except re.error as e:
                errors.append(
                    f"target '{self.name}': invalid match regex: {e}"
                )
        return errors


def _state_path(state_dir: Path, name: str) -> Path:
    safe = _NAME_SAFE.sub("_", name)
    return state_dir / f"poller_{safe}.json"


def _load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(path: Path, seen: set[str], cap: int = 5000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    truncated = list(seen)[-cap:]
    path.write_text(json.dumps(truncated), encoding="utf-8")


def _resolve_dotted(d: Any, path: str) -> Any:
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


async def _run_command(cfg: TargetConfig) -> tuple[int, str, str]:
    if cfg.shell:
        proc = await asyncio.create_subprocess_shell(
            cfg.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        argv = shlex.split(cfg.command)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=cfg.timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"timeout after {cfg.timeout_sec}s"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _items_from_json(stdout: str, target_name: str) -> list[dict]:
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("poller %r: stdout is not valid JSON", target_name)
        return []
    if isinstance(data, list):
        return [x if isinstance(x, dict) else {"_raw": x} for x in data]
    if isinstance(data, dict):
        return [data]
    return []


def _items_from_lines(stdout: str) -> list[dict]:
    out: list[dict] = []
    for line in stdout.splitlines():
        s = line.rstrip()
        if not s:
            continue
        out.append({"line": s, "_raw": s})
    return out


async def run_target(api, cfg: TargetConfig, state_dir: Path) -> None:
    state_file = _state_path(state_dir, cfg.name)
    first_run = not state_file.exists()
    seen = _load_seen(state_file)
    pat = re.compile(cfg.match) if cfg.match else None

    log.info(
        "Poller %r started (mode=%s poll=%ds first_run=%s shell=%s)",
        cfg.name, cfg.mode, cfg.poll_sec, first_run, cfg.shell,
    )

    while True:
        try:
            rc, stdout, stderr = await _run_command(cfg)
        except Exception:
            log.exception("Poller %r: command failed to launch", cfg.name)
            await asyncio.sleep(cfg.poll_sec)
            continue

        if rc != 0:
            log.warning(
                "Poller %r: command exit=%d stderr=%s",
                cfg.name, rc, stderr.rstrip()[:400],
            )
            await asyncio.sleep(cfg.poll_sec)
            continue

        if cfg.mode == "json":
            items = _items_from_json(stdout, cfg.name)
        else:
            items = _items_from_lines(stdout)

        new_items: list[dict] = []
        for item in items:
            if pat is not None:
                blob = json.dumps(item, ensure_ascii=False)
                if not pat.search(blob):
                    continue
            if cfg.mode == "json":
                ident = _resolve_dotted(item, cfg.id_field)
                if ident is None:
                    continue
                key = json.dumps(ident, sort_keys=True, ensure_ascii=False) \
                    if not isinstance(ident, (str, int, float)) else str(ident)
            else:
                key = item.get("_raw", "")
            if not key or key in seen:
                continue
            seen.add(key)
            new_items.append(item)

        if first_run:
            first_run = False
            _save_seen(state_file, seen)
            log.info(
                "Poller %r first-run snapshot: %d items marked seen",
                cfg.name, len(seen),
            )
        elif new_items:
            for item in new_items:
                body = render_template(cfg.template, item)
                if len(body) > _BODY_MAX:
                    body = body[: _BODY_MAX - 1] + "…"
                metadata = {"target": cfg.name, "mode": cfg.mode}
                await api.emit(body=body, metadata=metadata)
            _save_seen(state_file, seen)
            log.info(
                "Poller %r emitted %d new item(s)",
                cfg.name, len(new_items),
            )

        await asyncio.sleep(cfg.poll_sec)


def parse_targets(raw: Any) -> tuple[list[TargetConfig], list[str]]:
    out: list[TargetConfig] = []
    errors: list[str] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, list):
        errors.append("'targets' must be a list")
        return out, errors
    seen_names: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"targets[{i}] must be an object")
            continue
        cfg = TargetConfig(
            name=str(entry.get("name", "")),
            command=str(entry.get("command", "")),
            shell=bool(entry.get("shell", False)),
            mode=str(entry.get("mode", "json")),
            id_field=str(entry.get("id_field", "")),
            poll_sec=int(entry.get("poll_sec", 300)),
            template=str(entry.get("template", "{_raw}")),
            match=entry.get("match"),
            timeout_sec=int(entry.get("timeout_sec", 60)),
        )
        entry_errors = cfg.validate()
        if cfg.name in seen_names:
            entry_errors.append(f"duplicate target name '{cfg.name}'")
        if entry_errors:
            errors.extend(entry_errors)
            continue
        seen_names.add(cfg.name)
        out.append(cfg)
    return out, errors


async def setup(api) -> None:
    state_dir = Path(__file__).parent.parent.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    targets, errors = parse_targets(api.config.get("targets"))
    for err in errors:
        log.error("poller config: %s", err)

    for cfg in targets:
        api.spawn(run_target(api, cfg, state_dir))

    if not targets:
        log.warning("poller plugin enabled but no valid targets configured")
    else:
        log.info(
            "Started %d poller target(s): %s",
            len(targets),
            [t.name for t in targets],
        )
