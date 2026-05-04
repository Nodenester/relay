"""Unit tests for plugins/github/watcher.py — no live gh / no asyncio loop.

Run: .venv\\Scripts\\python.exe tests\\test_watcher_unit.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load watcher.py directly without going through the plugin loader.
_spec = importlib.util.spec_from_file_location(
    "_relay_test_watcher",
    REPO_ROOT / "plugins" / "github" / "watcher.py",
)
assert _spec and _spec.loader
watcher = importlib.util.module_from_spec(_spec)
sys.modules["_relay_test_watcher"] = watcher
_spec.loader.exec_module(watcher)


PASSED = 0
FAILED = 0


def assert_eq(actual, expected, msg: str) -> None:
    global PASSED, FAILED
    if actual == expected:
        PASSED += 1
        print(f"  PASS {msg}")
    else:
        FAILED += 1
        print(f"  FAIL {msg}\n    expected: {expected!r}\n    actual:   {actual!r}")


def test_build_gh_args() -> None:
    print("test_build_gh_args")
    args = watcher.build_gh_args("issues", "owner/repo", "")
    assert_eq(args[:5], ["gh", "issue", "list", "--repo", "owner/repo"], "issues base")
    assert_eq("--json" in args, True, "issues has --json")

    args = watcher.build_gh_args("runs", "a/b", "--status failure --branch main")
    assert_eq(args[:5], ["gh", "run", "list", "--repo", "a/b"], "runs base")
    assert_eq(args[-4:], ["--status", "failure", "--branch", "main"], "runs filter appended")

    args = watcher.build_gh_args("pulls", "a/b", '--search "review-requested:@me"')
    assert_eq(args[1], "pr", "pulls -> gh pr")
    # shlex preserves the quoted arg as a single token.
    assert_eq("review-requested:@me" in args, True, "pulls quoted filter")

    try:
        watcher.build_gh_args("nonsense", "a/b", "")
    except ValueError:
        assert_eq(True, True, "rejects unknown kind")
    else:
        assert_eq(False, True, "rejects unknown kind")


def test_render_fields_issues() -> None:
    print("test_render_fields_issues")
    item = {
        "number": 7,
        "title": "Orders page broken on mobile",
        "body": "x" * 5000,  # bigger than _BODY_MAX
        "labels": [{"name": "ai:queued"}, {"name": "frontend"}],
        "author": {"login": "octocat"},
        "state": "OPEN",
        "url": "https://github.com/x/y/issues/7",
    }
    fields = watcher.render_fields("issues", "x/y", item)
    assert_eq(fields["number"], "7", "number coerced to str")
    assert_eq(fields["labels"], "ai:queued,frontend", "labels joined")
    assert_eq(fields["author"], "octocat", "author flattened")
    assert_eq(fields["repo"], "x/y", "repo injected")
    assert_eq(len(fields["body"]) <= 2000, True, "body truncated")
    assert_eq(fields["body"].endswith("…"), True, "body has ellipsis")


def test_render_template_safedict() -> None:
    print("test_render_template_safedict")
    out = watcher.render_template(
        "Issue #{number}: {title}",
        {"number": "1", "title": "hi"},
    )
    assert_eq(out, "Issue #1: hi", "basic substitution")

    out = watcher.render_template(
        "{number} {missing}",
        {"number": "1"},
    )
    assert_eq(out, "1 {missing}", "missing keys render literally")


def test_parse_watchers() -> None:
    print("test_parse_watchers")
    cfgs, errs = watcher.parse_watchers([
        {"name": "ok", "kind": "issues", "repo": "a/b", "poll_sec": 60},
        {"name": "bad-kind", "kind": "wat", "repo": "a/b"},
        {"name": "no-repo", "kind": "issues"},
        {"name": "ok", "kind": "issues", "repo": "x/y"},  # duplicate name
        {"name": "low-poll", "kind": "issues", "repo": "x/y", "poll_sec": 1},
    ])
    assert_eq(len(cfgs), 1, "only one valid watcher kept")
    assert_eq(cfgs[0].name, "ok", "valid watcher is the first 'ok'")
    assert_eq(any("kind" in e for e in errs), True, "kind error reported")
    assert_eq(any("missing 'repo'" in e for e in errs), True, "missing repo reported")
    assert_eq(any("duplicate" in e for e in errs), True, "duplicate name reported")
    assert_eq(any("poll_sec" in e for e in errs), True, "low poll reported")

    cfgs, errs = watcher.parse_watchers(None)
    assert_eq(cfgs, [], "None gives empty")
    assert_eq(errs, [], "None gives no errors")

    cfgs, errs = watcher.parse_watchers("not a list")
    assert_eq(cfgs, [], "non-list rejected")
    assert_eq(len(errs), 1, "non-list error reported")


def test_seen_set_persistence() -> None:
    print("test_seen_set_persistence")
    with tempfile.TemporaryDirectory() as td:
        sd = Path(td)
        path = watcher._state_path(sd, "my watcher!")
        assert_eq(path.name.startswith("github_watcher_my_watcher"), True, "name sanitized")

        seen = watcher._load_seen(path)
        assert_eq(seen, set(), "empty when missing")

        watcher._save_seen(path, {"1", "2", "3"})
        loaded = watcher._load_seen(path)
        assert_eq(loaded, {"1", "2", "3"}, "round-trip")

        # Truncation cap honored.
        watcher._save_seen(path, set(str(i) for i in range(50)), cap=10)
        loaded = watcher._load_seen(path)
        assert_eq(len(loaded), 10, "cap honored")


def main() -> int:
    test_build_gh_args()
    test_render_fields_issues()
    test_render_template_safedict()
    test_parse_watchers()
    test_seen_set_persistence()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
