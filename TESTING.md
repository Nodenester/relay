# Testing relay

## Integration test (end-to-end, no external services)

Validates the full loop:
- Plugin emits an event
- Runner dispatcher flushes it to Claude's stdin
- Claude processes, emits response
- Claude calls a tool via the relay HTTP MCP
- Tool handler runs in-process and returns

```powershell
.\.venv\Scripts\python.exe tests\run_integration_test.py
```

Expected output:

```
[test] Launching runner (timeout 120s)...
[test] PASS: testplug_double tool invocation seen
[test] PASS: marker 'RELAY_TEST_ACK' seen in Claude output
[test] Terminating runner...
[test] Results: marker=True, tool_called=True
[test] INTEGRATION TEST PASSED [OK]
```

The test:
1. Backs up your `config.json` (if any)
2. Writes a test config enabling only `plugins/testplug/`
3. Launches `runner` as a subprocess
4. Watches `state/logs/claude_stream.jsonl` for the marker + tool call
5. Restores your original config

Takes 30–60s. Uses Haiku to keep cost low.

## Manual: Discord round-trip

Needs a bot token + a channel ID (see README for setup).

1. Fill `config.json`:
   ```json
   "discord": { "enabled": true, "token": "...", "channel_id": 12345678 }
   ```
2. Run: `.\.venv\Scripts\python.exe -m runner`
3. In Discord, post: `hello`
4. Claude should reply via the bot within a few seconds.

## What each log contains

- `state/logs/claude_stream.jsonl` — every stream-json event Claude emitted.
  Useful for debugging what Claude saw and what tools it called.
- Runner stderr — plugin loading, dispatcher activity, MCP HTTP requests.

## Verifying the MCP is up

While runner is running, from another shell:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:9123/mcp -Method Post -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -ContentType "application/json"
```

Should list every registered plugin tool.
