"""Integration test — launches the runner, checks that Claude receives an
emitted event, calls the test tool, and responds with the expected marker.

Run: .venv\\Scripts\\python.exe tests\\run_integration_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
LOG_FILE = STATE_DIR / "logs" / "claude_stream.jsonl"
TIMEOUT_SEC = 120
MARKER = "RELAY_TEST_ACK"


def log(msg: str) -> None:
    print(f"[test] {msg}", flush=True)


def prepare_test_env() -> Path:
    # Write a test config that enables only the testplug plugin.
    config = {
        "claude": {"model": "haiku"},  # cheaper + faster for test
        "runner": {"mcp_port": 9199, "log_level": "INFO"},
        "plugins": {
            "discord": {"enabled": False, "token": "", "channel_id": 0},
            "outlook": {"enabled": False},
            "github": {"enabled": False},
            "signal": {"enabled": False},
            "testplug": {"enabled": True},
        },
    }
    test_config = REPO_ROOT / "config.test.json"
    test_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return test_config


def wipe_state() -> None:
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR, ignore_errors=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "logs").mkdir(parents=True, exist_ok=True)


def run() -> int:
    wipe_state()
    prepare_test_env()

    # Swap configs temporarily
    real_config = REPO_ROOT / "config.json"
    real_config_backup = REPO_ROOT / "config.json.backup"
    if real_config.exists():
        shutil.copy(real_config, real_config_backup)
    shutil.copy(REPO_ROOT / "config.test.json", real_config)

    python_exe = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    env = os.environ.copy()
    # Also redirect runner's own stderr/stdout to file so we can debug.
    runner_log = STATE_DIR / "logs" / "runner_test.log"

    log(f"Launching runner (timeout {TIMEOUT_SEC}s)...")
    proc = subprocess.Popen(
        [str(python_exe), "-m", "runner"],
        cwd=str(REPO_ROOT),
        stdout=open(runner_log, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        env=env,
    )

    deadline = time.time() + TIMEOUT_SEC
    success = False
    tool_called = False
    try:
        while time.time() < deadline:
            time.sleep(2)
            if not LOG_FILE.exists():
                continue
            content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            if MARKER in content and not success:
                log(f"PASS: marker '{MARKER}' seen in Claude output")
                success = True
            if '"testplug_double"' in content or "testplug_double" in content:
                if not tool_called:
                    log("PASS: testplug_double tool invocation seen")
                    tool_called = True
            if success and tool_called:
                break
    finally:
        log("Terminating runner...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Restore config
        if real_config_backup.exists():
            shutil.move(str(real_config_backup), str(real_config))
        else:
            real_config.unlink(missing_ok=True)
        (REPO_ROOT / "config.test.json").unlink(missing_ok=True)

    log(f"Results: marker={success}, tool_called={tool_called}")
    if success and tool_called:
        log("INTEGRATION TEST PASSED [OK]")
        return 0
    log("INTEGRATION TEST FAILED [FAIL]")
    log(f"  Runner log: {runner_log}")
    log(f"  Claude stream log: {LOG_FILE}")
    return 1


if __name__ == "__main__":
    sys.exit(run())
