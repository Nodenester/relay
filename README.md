# relay

A lightweight two-way runner for Claude Code. Wraps one persistent
**interactive** Claude Code session and plumbs external events into it
via the native channel mechanism, while routing tool calls back to
plugin handlers through a single local MCP server.

The same MCP server doubles as the **channel** (declares
`experimental.claude/channel` capability), so there's exactly one
process to run, one port to bind, no plugin to install. The session
also auto-enables Claude Code's **Remote Control**, so you can attach
from `claude.ai/code` on any device — phone, laptop, anywhere.

```
                  ┌────────────────────────────────────────────────┐
                  │ relay process (Python)                         │
plugins ─emit──>  │  event bus → dispatcher                        │
                  │                  │                             │
                  │                  │ notifications/claude/channel│
                  │                  ▼                             │
                  │   FastMCP (HTTP, port 9301) + channel cap      │
                  └────────────────────────────────────────────────┘
                                     │
                                     ▼ (HTTP, MCP-over-streamable)
                  ┌────────────────────────────────────────────────┐
                  │ claude (interactive, PTY-wrapped, hidden)      │
                  │  --dangerously-load-development-channels       │
                  │     server:relay                               │
                  │  --remote-control <name>                       │
                  │  --resume <UUID>                               │
                  └────────────────────────────────────────────────┘
                                     │
                                     ▼
                            claude.ai/code (web)
                            user attaches remotely
```

- **One persistent session.** Prompt cache stays warm. Lives forever in
  a hidden ConPTY (no visible window).
- **Native Claude Code Remote Control.** Pass `--remote-control <name>`
  in config and the agent shows up at `claude.ai/code`. No bot, no
  custom web UI, no auth boilerplate.
- **Native Channel mechanism.** Plugin events arrive as
  `<channel source="...">` user turns via MCP `notifications/claude/channel`.
  No extra plugin to write, no extra port — relay's existing MCP server
  doubles as the channel.
- **Plugins are folders in `plugins/`.** Triggers and tools auto-discovered.
  No runner edits when you add or delete one.
- **Generic plugins**: `poller/` (any shell command on a schedule),
  `webhook/` (HTTP listener), `inbox/` (file-tailed message injection),
  plus per-source plugins for `github`, `outlook`, `signal`, `discord`.
- **Autostarts on login** via Windows Task Scheduler.
- **Autonomous by default** (`--dangerously-skip-permissions`). Safety
  moves to `CLAUDE.md` rules.

## Requirements

- Windows (the supervisor uses ConPTY via `pywinpty`); macOS/Linux
  support possible by swapping the PTY backend.
- Python 3.12+
- `claude` CLI authenticated with a Claude subscription (Max recommended).
- `pip install -r requirements.txt`

## Install

```powershell
cd E:\Repos\relay
.\install.ps1
```

Edit `CLAUDE.md` for the agent's persona + rules. Edit `config.json` for
plugin settings.

## Config — minimum

```json
{
  "claude": {
    "model": "sonnet",
    "additional_args": []
  },
  "runner": {
    "mcp_port": 9301,
    "log_level": "INFO",
    "remote_control_name": "my-agent"
  },
  "plugins": {
    "inbox": {
      "enabled": true,
      "path": "state/inbox.jsonl",
      "poll_sec": 2,
      "default_source": "user"
    }
  }
}
```

If `remote_control_name` is omitted it defaults to `relay-<dir-name>`.

## Event flow

```
plugin → api.emit(body, metadata) → event_bus
                                        │
                                        ▼
                                  event_dispatcher
                                        │
                                        ▼ send_channel_event
                                MCP notifications/claude/channel
                                        │
                                        ▼
                            claude session sees:
                            <channel source="github_watcher"
                                     chat_id="..."
                                     message_id="...">
                              <event body>
                            </channel>
```

Multiple events arriving in quick succession are pushed individually as
separate channel notifications. Claude's session naturally serializes
them as user turns.

## Talking to a relay agent

External (other process or shell):

```bash
# append a JSON line to the inbox
echo '{"body": "what is on my plate?", "source": "user"}' >> state/inbox.jsonl
# the inbox plugin tails this file every 2 sec; the line arrives as a turn
```

Remotely (phone, laptop, anywhere): open `claude.ai/code` while logged
in to your Claude account, find the session named whatever you set as
`remote_control_name`. Type and reply as if you were at the keyboard.

## Built-in plugins

| Plugin | Purpose |
|--------|---------|
| `discord/` | Bidirectional chat in a specific channel. |
| `outlook/` | Outlook Classic via the separately-installed `outlook-mcp-server`. |
| `github/` | Notifications poller + per-repo watchers (issues / runs / PRs). |
| `signal/` | Signal Note-to-Self via `signal-cli`. |
| `inbox/` | File-tailed message injection (the `state/inbox.jsonl` channel). |
| `poller/` | Generic — runs any shell command on a schedule. |
| `webhook/` | Generic — HTTP listener with optional HMAC verification. |
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

Available kinds: `issues`, `runs`, `pulls`. Each item's gh-JSON fields
are exposed to the optional `template` for custom rendering.

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
    }
  ]
}
```

## Writing a plugin

A plugin is a folder under `plugins/` with a `plugin.json` and a Python
module. Two supported modes:

- **inproc** — Python module with `async def setup(api)` that registers
  tools and background tasks. Runs inside the runner's event loop.
- **proxy** — `plugin.json` points at a pre-built MCP server binary
  (e.g. `outlook-mcp-server`). The runner forwards tool calls and
  namespaces them under the plugin name.

See `plugins/_template/` for the skeleton. The plugin folder is treated
as a Python package, so multi-file plugins (like `github/` with its
separate `watcher.py`) work naturally with relative imports.

## Status

Beta. License: MIT.
