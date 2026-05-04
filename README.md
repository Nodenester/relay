# relay

A lightweight two-way runner for Claude Code. Wraps one persistent
`claude --resume` session and plumbs external events into it as user
messages while routing Claude's tool calls back to plugin handlers — all
through a single local MCP server.

```
triggers (plugins) ──> event queue ──> persistent `claude --resume`
                                              │
                                              ▼
                                       tool calls back to
                                       runner's aggregating
                                       MCP (localhost HTTP)
                                              │
                                              ▼
                                       dispatched to plugin
                                       tool handlers
```

- **One persistent session.** Prompt cache stays warm, conversation
  continuity is native, no spawn-per-event.
- **Plugins are folders in `plugins/`.** Triggers and tools auto-discovered
  at startup. No runner edits when you add or delete one.
- **Generic plugins for almost anything.** `poller/` runs any shell command
  on a schedule; `webhook/` listens on any port — you rarely need to write
  Python for a new trigger source.
- **One MCP, all tools.** Claude Code connects to a single local MCP that
  aggregates every plugin's tools. Every call is observable and logged.
- **Autostarts on login** via Windows Task Scheduler. Restarts on crash.
- **Autonomous by default** (`--dangerously-skip-permissions`). Safety
  moves to `CLAUDE.md` rules.
- **No runtime dependencies on a backend.** Just `claude` CLI + Python +
  whatever plugins you enable.

## Install

```powershell
cd E:\Repos\relay
.\install.ps1
```

Edit `CLAUDE.md` for persona + rules. Edit `config.json` for plugin
settings. Delete any plugin folder you don't use.

## Event flow

When a plugin calls `api.emit(body, metadata)`, an `Event` enters the
queue. The dispatcher waits for the current Claude turn to finish
(`type:result` on stdout) and then drains everything queued in one go,
sending it as a single user message:

```
[FROM github | repo=your-org/your-repo | type=Issue]
Issue #42: orders page broken on mobile
…
```

If multiple events arrive together they're concatenated under a
`[BATCH: N events arrived together]` header so Claude sees them grouped
without fragmenting turns.

## Built-in plugins

| Plugin | Purpose |
|--------|---------|
| `discord/` | Bi-directional chat in a specific channel. |
| `outlook/` | Outlook Classic via the separately-installed `outlook-mcp-server`. |
| `github/` | Notifications poller + per-repo watchers (issues / runs / PRs). |
| `signal/` | Signal Note-to-Self via `signal-cli`. |
| `poller/` | **Generic** — runs any shell command on a schedule. |
| `webhook/` | **Generic** — HTTP listener with optional HMAC verification. |
| `_template/` | Skeleton for new plugins. |

## github watchers

The `github` plugin runs the notifications poller plus any number of
configurable per-repo watchers. Each watcher polls one `gh` query and
emits a separate event for every new item.

```json
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
    },
    {
      "name": "ci-failures",
      "kind": "runs",
      "repo": "your-org/your-repo",
      "filter": "--status failure --branch main --limit 20",
      "poll_sec": 300
    },
    {
      "name": "review-requests",
      "kind": "pulls",
      "repo": "your-org/your-repo",
      "filter": "--search review-requested:@me --state open"
    }
  ]
}
```

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `name` | yes | — | Unique per watcher; used in state filename. |
| `kind` | yes | — | `issues`, `runs`, or `pulls`. |
| `repo` | yes | — | `owner/name`. |
| `filter` | no | `""` | Raw extra `gh` args (`--label …`, `--state …`, `--search …`). |
| `poll_sec` | no | 300 | Min 10. |
| `template` | no | per-kind default | See per-kind fields below. |

Template fields available per kind:

- **issues**: `number`, `title`, `body`, `labels` (joined), `author`,
  `createdAt`, `updatedAt`, `url`, `state`, `repo`.
- **runs**: `databaseId`, `name`, `displayTitle`, `status`, `conclusion`,
  `headBranch`, `headSha`, `event`, `workflowName`, `url`, `createdAt`,
  `updatedAt`, `repo`.
- **pulls**: `number`, `title`, `body`, `labels`, `author`,
  `baseRefName`, `headRefName`, `state`, `url`, `createdAt`,
  `updatedAt`, `isDraft`, `repo`.

## Generic poller

For any source the github plugin doesn't natively cover. Runs an
arbitrary shell command on a schedule and emits each new item.

```json
"poller": {
  "enabled": true,
  "targets": [
    {
      "name": "k8s-failed-pods",
      "command": "kubectl get pods -A --field-selector=status.phase=Failed -o json | jq '.items'",
      "shell": true,
      "id_field": "metadata.uid",
      "poll_sec": 120,
      "template": "Failed pod: {metadata.namespace}/{metadata.name}\nReason: {status.reason}"
    }
  ]
}
```

| Field | Default | Notes |
|-------|---------|-------|
| `command` | — | Shell command. Pipes/redirects need `shell: true`. |
| `shell` | `false` | If true, runs via system shell. |
| `mode` | `"json"` | `json` parses stdout as a JSON array; `lines` treats each line as one item. |
| `id_field` | — | Required for `json`. Dotted path, e.g. `metadata.uid`. |
| `match` | `null` | Optional regex; only items whose JSON contains a match are emitted. |
| `poll_sec` | 300 | Min 5. |
| `timeout_sec` | 60 | Per-poll command timeout. |
| `template` | `"{_raw}"` | Dotted access, missing keys render literally. |

## Generic webhook

HTTP listener for true push triggers (no polling lag). Optional
GitHub-style HMAC-SHA256 verification per endpoint.

```json
"webhook": {
  "enabled": true,
  "host": "127.0.0.1",
  "port": 7700,
  "endpoints": [
    {
      "name": "github-push",
      "path": "/github",
      "secret_env": "RELAY_GITHUB_WEBHOOK_SECRET",
      "template": "GitHub event {header.x-github-event} on {payload.repository.full_name}\n{body}"
    },
    {
      "name": "generic",
      "path": "/in",
      "template": "{body}"
    }
  ]
}
```

Template fields: `{body}` (raw), `{header.<name>}` (lower-case),
`{payload.<dotted.path>}` (JSON-decoded body), `{query.<name>}`,
`{path}`, `{method}`. HMAC-mismatched requests get 401 and are not
emitted.

For GitHub webhooks, expose the port via cloudflared / Tailscale Funnel /
ngrok and register the URL on the repo:

```bash
gh api -X POST repos/OWNER/REPO/hooks \
  -f name=web -f active=true \
  -F 'events[]=issues' -F 'events[]=pull_request' \
  -F 'config[url]=https://YOUR-TUNNEL/github' \
  -F 'config[content_type]=json' \
  -F 'config[secret]=<same as RELAY_GITHUB_WEBHOOK_SECRET>'
```

## Writing a plugin

A plugin is a folder under `plugins/` with a `plugin.json` and a Python
module. Two supported modes:

- **inproc** — a Python module with `async def setup(api)` that
  registers tools and background tasks. Runs inside the runner's event
  loop.
- **proxy** — point `plugin.json` at a pre-built MCP server binary
  (e.g. outlook-mcp-server). The runner forwards tool calls and
  namespaces them under the plugin name.

See `plugins/_template/` for the skeleton. The plugin folder is treated
as a Python package, so multi-file plugins (like `github/` with its
separate `watcher.py`) work naturally with relative imports.

## Status

Alpha. Tested locally. License: MIT.
