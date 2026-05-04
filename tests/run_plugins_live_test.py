"""Live plugin integration test — enables outlook + github, verifies they
start polling without errors. Does not require new email or new notifications
(first-run snapshot mode emits nothing).

Run: .venv\\Scripts\\python.exe tests\\run_plugins_live_test.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"
RUNNER_LOG = STATE_DIR / "logs" / "runner_plugins_test.log"
TIMEOUT_SEC = 25


def log(msg: str) -> None:
    print(f"[test] {msg}", flush=True)


def run() -> int:
    # Wipe state so first-run snapshot logic triggers cleanly.
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR, ignore_errors=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "logs").mkdir(parents=True, exist_ok=True)

    # Test config: outlook + github ON, discord + signal OFF, use haiku.
    test_config = {
        "claude": {"model": "haiku"},
        "runner": {"mcp_port": 9210, "log_level": "INFO"},
        "plugins": {
            "discord": {"enabled": False, "token": "", "channel_id": 0},
            "outlook": {"enabled": True, "poll_sec": 3600},
            "github": {"enabled": True, "poll_sec": 3600},
            "signal": {"enabled": False},
        },
    }
    real_config = REPO_ROOT / "config.json"
    backup = REPO_ROOT / "config.json.backup2"
    if real_config.exists():
        shutil.copy(real_config, backup)
    real_config.write_text(json.dumps(test_config, indent=2), encoding="utf-8")

    python_exe = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    log(f"Launching runner (timeout {TIMEOUT_SEC}s)...")
    proc = subprocess.Popen(
        [str(python_exe), "-m", "runner"],
        cwd=str(REPO_ROOT),
        stdout=open(RUNNER_LOG, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )

    try:
        time.sleep(TIMEOUT_SEC)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if backup.exists():
            shutil.move(str(backup), str(real_config))

    logtext = RUNNER_LOG.read_text(encoding="utf-8", errors="replace")

    checks = {
        "outlook plugin loaded": "Plugin outlook loaded" in logtext,
        "github plugin loaded": "Plugin github loaded" in logtext,
        "outlook poll loop started": "Outlook poll loop started" in logtext,
        "github poll loop started": "GitHub poll loop started" in logtext,
        "outlook first-run snapshot": "Outlook first-run snapshot" in logtext,
        "github first-run snapshot": "GitHub first-run snapshot" in logtext,
        "mcp served on 9210": "http://127.0.0.1:9210" in logtext,
        "no poll exceptions": "Outlook poll failed" not in logtext and "GitHub poll failed" not in logtext,
        "no unhandled exception": "Traceback (most recent" not in logtext,
    }

    passed = 0
    failed = 0
    for label, ok in checks.items():
        status = "PASS" if ok else "FAIL"
        log(f"  [{status}] {label}")
        if ok:
            passed += 1
        else:
            failed += 1

    # Also extract interesting runtime info
    seen_outlook = (STATE_DIR / "outlook_seen.json").exists()
    seen_github = (STATE_DIR / "github_seen.json").exists()
    log(f"  state/outlook_seen.json created: {seen_outlook}")
    log(f"  state/github_seen.json created: {seen_github}")
    if seen_outlook:
        log(f"    outlook seen count: {len(json.loads((STATE_DIR / 'outlook_seen.json').read_text()))}")
    if seen_github:
        log(f"    github seen count: {len(json.loads((STATE_DIR / 'github_seen.json').read_text()))}")

    log(f"Result: {passed} passed, {failed} failed")
    log(f"Log file: {RUNNER_LOG}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
