"""Configurable per-target GitHub watchers.

Each watcher polls one `gh` query (issues, runs, or pulls) on a configurable
interval and emits an event for every new item. Independent state file per
watcher, independent poll cadence, independent template.

Watcher config shape::

    {
      "name": "my-watcher",     # unique, used in state filename + log lines
      "kind": "issues",         # one of: issues | runs | pulls
      "repo": "owner/repo",     # required
      "filter": "--label foo",  # extra raw gh args, optional
      "poll_sec": 120,          # default 300
      "template": "..."         # optional, falls back to per-kind default
    }

The `template` is rendered with str.format_map; missing keys render as the
literal placeholder so a bad template never crashes the loop.
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

log = logging.getLogger("relay.plugin.github.watcher")

KINDS = ("issues", "runs", "pulls")

# JSON fields requested per kind. Kept compact — adding a field is cheap.
_FIELDS = {
    "issues": "number,title,body,labels,author,createdAt,updatedAt,url,state",
    "runs": (
        "databaseId,name,displayTitle,status,conclusion,headBranch,headSha,"
        "event,workflowName,url,createdAt,updatedAt"
    ),
    "pulls": (
        "number,title,body,labels,author,baseRefName,headRefName,state,url,"
        "createdAt,updatedAt,isDraft"
    ),
}

_ID_FIELD = {
    "issues": "number",
    "runs": "databaseId",
    "pulls": "number",
}

_DEFAULT_TEMPLATE = {
    "issues": (
        "Issue #{number} ({state}): {title}\n"
        "Repo: {repo}\n"
        "Author: {author}\n"
        "Labels: {labels}\n"
        "Url: {url}\n"
        "\n{body}"
    ),
    "runs": (
        "Run #{databaseId}: {workflowName} — {displayTitle}\n"
        "Repo: {repo}\n"
        "Branch: {headBranch}  Event: {event}\n"
        "Status: {status}  Conclusion: {conclusion}\n"
        "Url: {url}"
    ),
    "pulls": (
        "PR #{number} ({state}): {title}\n"
        "Repo: {repo}\n"
        "Author: {author}\n"
        "Branch: {headRefName} -> {baseRefName}  Draft: {isDraft}\n"
        "Labels: {labels}\n"
        "Url: {url}\n"
        "\n{body}"
    ),
}

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")
_BODY_MAX = 2000


@dataclass
class WatcherConfig:
    name: str
    kind: str
    repo: str
    filter: str = ""
    poll_sec: int = 300
    template: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("watcher missing 'name'")
        elif _NAME_SAFE.search(self.name):
            errors.append(
                f"watcher name '{self.name}' must match [a-zA-Z0-9_-]+"
            )
        if self.kind not in KINDS:
            errors.append(
                f"watcher '{self.name}': kind must be one of {KINDS} "
                f"(got {self.kind!r})"
            )
        if not self.repo:
            errors.append(f"watcher '{self.name}': missing 'repo'")
        elif "/" not in self.repo:
            errors.append(
                f"watcher '{self.name}': repo must be 'owner/name' "
                f"(got {self.repo!r})"
            )
        if self.poll_sec < 10:
            errors.append(
                f"watcher '{self.name}': poll_sec must be >= 10 "
                f"(got {self.poll_sec})"
            )
        return errors


class _SafeDict(dict):
    """Dict that returns the literal placeholder for missing keys.

    Lets a user-supplied template keep `{some_field}` literally if they
    typoed instead of crashing the watcher loop.
    """
    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def _flatten_labels(labels: Any) -> str:
    if not isinstance(labels, list):
        return ""
    out = []
    for entry in labels:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                out.append(str(name))
        elif isinstance(entry, str):
            out.append(entry)
    return ",".join(out)


def _flatten_author(author: Any) -> str:
    if isinstance(author, dict):
        return str(author.get("login") or author.get("name") or "")
    if isinstance(author, str):
        return author
    return ""


def _truncate(text: Any, limit: int = _BODY_MAX) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def render_fields(kind: str, repo: str, item: dict) -> dict[str, str]:
    """Map raw gh JSON for one item into flat string fields for templates."""
    flat: dict[str, str] = {"repo": repo}
    for k, v in item.items():
        if k == "labels":
            flat[k] = _flatten_labels(v)
        elif k == "author":
            flat[k] = _flatten_author(v)
        elif k == "body":
            flat[k] = _truncate(v)
        elif isinstance(v, (str, int, float, bool)):
            flat[k] = str(v)
        elif v is None:
            flat[k] = ""
        else:
            # Lists/dicts other than labels/author — render as JSON for visibility.
            try:
                flat[k] = json.dumps(v, ensure_ascii=False)
            except (TypeError, ValueError):
                flat[k] = str(v)
    return flat


def render_template(template: str, fields: dict[str, str]) -> str:
    return template.format_map(_SafeDict(fields))


_GH_SUBCOMMAND = {"issues": "issue", "runs": "run", "pulls": "pr"}


def build_gh_args(kind: str, repo: str, filter_str: str) -> list[str]:
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    base = [
        "gh", _GH_SUBCOMMAND[kind], "list",
        "--repo", repo,
        "--json", _FIELDS[kind],
    ]
    extra = shlex.split(filter_str) if filter_str else []
    return base + extra


async def _fetch(args: list[str]) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning("gh failed (%s): %s",
                    " ".join(args),
                    stderr.decode("utf-8", errors="replace").rstrip())
        return []
    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        log.warning("gh returned non-JSON for: %s", " ".join(args))
        return []
    if not isinstance(data, list):
        return []
    return data


def _state_path(state_dir: Path, name: str) -> Path:
    safe = _NAME_SAFE.sub("_", name)
    return state_dir / f"github_watcher_{safe}.json"


def _load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        log.warning("Could not parse %s — starting fresh", path)
        return set()


def _save_seen(path: Path, seen: set[str], cap: int = 2000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    truncated = list(seen)[-cap:]
    path.write_text(json.dumps(truncated), encoding="utf-8")


async def run_watcher(api, cfg: WatcherConfig, state_dir: Path) -> None:
    """One watcher's poll loop. Runs forever until cancelled."""
    state_file = _state_path(state_dir, cfg.name)
    first_run = not state_file.exists()
    seen = _load_seen(state_file)
    template = cfg.template or _DEFAULT_TEMPLATE[cfg.kind]
    args = build_gh_args(cfg.kind, cfg.repo, cfg.filter)

    log.info(
        "Watcher %r started (kind=%s repo=%s poll=%ds first_run=%s)",
        cfg.name, cfg.kind, cfg.repo, cfg.poll_sec, first_run,
    )

    while True:
        try:
            items = await _fetch(args)
        except Exception:
            log.exception("Watcher %r: fetch crashed", cfg.name)
            items = []

        new_items: list[dict] = []
        id_field = _ID_FIELD[cfg.kind]
        for item in items:
            ident = item.get(id_field)
            if ident is None:
                continue
            key = str(ident)
            if key in seen:
                continue
            seen.add(key)
            new_items.append(item)

        if first_run:
            first_run = False
            _save_seen(state_file, seen)
            log.info(
                "Watcher %r first-run snapshot: %d items marked seen",
                cfg.name, len(seen),
            )
        elif new_items:
            for item in new_items:
                fields = render_fields(cfg.kind, cfg.repo, item)
                body = render_template(template, fields)
                metadata = {
                    "watcher": cfg.name,
                    "kind": cfg.kind,
                    "repo": cfg.repo,
                    "id": fields.get(id_field, ""),
                }
                await api.emit(body=body, metadata=metadata)
            _save_seen(state_file, seen)
            log.info(
                "Watcher %r emitted %d new item(s)",
                cfg.name, len(new_items),
            )

        await asyncio.sleep(cfg.poll_sec)


def parse_watchers(raw: Any) -> tuple[list[WatcherConfig], list[str]]:
    """Parse the raw config.watchers list into validated WatcherConfig.

    Returns (configs, errors). Bad entries are skipped, errors are collected
    so the plugin can log them all at once.
    """
    out: list[WatcherConfig] = []
    errors: list[str] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, list):
        errors.append("'watchers' must be a list")
        return out, errors
    seen_names: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"watchers[{i}] must be an object")
            continue
        cfg = WatcherConfig(
            name=str(entry.get("name", "")),
            kind=str(entry.get("kind", "")),
            repo=str(entry.get("repo", "")),
            filter=str(entry.get("filter", "")),
            poll_sec=int(entry.get("poll_sec", 300)),
            template=entry.get("template"),
        )
        entry_errors = cfg.validate()
        if cfg.name in seen_names:
            entry_errors.append(f"duplicate watcher name '{cfg.name}'")
        if entry_errors:
            errors.extend(entry_errors)
            continue
        seen_names.add(cfg.name)
        out.append(cfg)
    return out, errors
