"""Unit tests for plugins/webhook/plugin.py — pure logic, no live HTTP.

Run: .venv\\Scripts\\python.exe tests\\test_webhook_unit.py
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `from runner.template import ...` works

_spec = importlib.util.spec_from_file_location(
    "_relay_test_webhook",
    REPO_ROOT / "plugins" / "webhook" / "plugin.py",
)
assert _spec and _spec.loader
hook = importlib.util.module_from_spec(_spec)
sys.modules["_relay_test_webhook"] = hook
_spec.loader.exec_module(hook)


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


def test_verify_hmac() -> None:
    print("test_verify_hmac")
    body = b'{"x":1}'
    secret = "topsecret"
    sig = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    assert_eq(hook._verify_hmac(body, sig, secret), True, "valid sig accepted")
    assert_eq(hook._verify_hmac(body, sig, "wrong"), False, "wrong secret rejected")
    assert_eq(hook._verify_hmac(b"tampered", sig, secret), False, "tampered body rejected")
    assert_eq(hook._verify_hmac(body, "sha1=abc", secret), False, "wrong algo rejected")
    assert_eq(hook._verify_hmac(body, "", secret), False, "empty header rejected")
    assert_eq(hook._verify_hmac(body, "garbage", secret), False, "bad format rejected")


def test_render_template_dotted() -> None:
    print("test_render_template_dotted")
    data = {
        "payload": {"repository": {"full_name": "x/y"}},
        "header": {"x-github-event": "issues"},
        "body": "{}",
    }
    assert_eq(
        hook.render_template("{payload.repository.full_name}", data),
        "x/y", "dotted payload",
    )
    assert_eq(
        hook.render_template("{header.x-github-event}", data),
        "issues", "header lookup",
    )
    assert_eq(
        hook.render_template("{payload.missing.deep}", data),
        "{payload.missing.deep}", "missing literal",
    )


def test_template_render() -> None:
    print("test_template_render")
    fields = {
        "body": "{}",
        "header": {"x-github-event": "issues"},
        "payload": {"repository": {"full_name": "x/y"}},
        "path": "/github",
        "method": "POST",
        "query": {},
    }
    out = hook.render_template(
        "Event {header.x-github-event} on {payload.repository.full_name}",
        fields,
    )
    assert_eq(out, "Event issues on x/y", "github-style template")


def test_parse_endpoints() -> None:
    print("test_parse_endpoints")
    cfgs, errs = hook.parse_endpoints([
        {"name": "ok", "path": "/in"},
        {"name": "bad path", "path": "no-leading-slash"},
        {"name": "ok", "path": "/dup-name"},  # duplicate name
        {"name": "ok2", "path": "/in"},  # duplicate path
        {"path": "/anon"},  # missing name
        {"name": "with-secret", "path": "/sec", "secret_env": "FOO"},
    ])
    names = {c.name for c in cfgs}
    assert_eq("ok" in names, True, "first ok kept")
    assert_eq("with-secret" in names, True, "endpoint with secret kept")
    assert_eq(any("must start with" in e for e in errs), True, "bad path reported")
    assert_eq(any("duplicate" in e for e in errs), True, "duplicate reported")
    assert_eq(any("missing 'name'" in e for e in errs), True, "missing name reported")

    bad_name_cfgs, bad_name_errs = hook.parse_endpoints([
        {"name": "bad name!", "path": "/x"},
    ])
    assert_eq(len(bad_name_cfgs), 0, "bad name rejected")
    assert_eq(any("[a-zA-Z0-9_-]+" in e for e in bad_name_errs), True, "name regex enforced")


def test_truncate() -> None:
    print("test_truncate")
    assert_eq(hook._truncate("abc", 100), "abc", "short unchanged")
    assert_eq(hook._truncate("a" * 5000).endswith("…"), True, "long truncated")
    assert_eq(len(hook._truncate("a" * 5000)), 4000, "respects default cap")


def main() -> int:
    test_verify_hmac()
    test_render_template_dotted()
    test_template_render()
    test_parse_endpoints()
    test_truncate()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
