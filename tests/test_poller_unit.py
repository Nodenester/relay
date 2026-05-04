"""Unit tests for plugins/poller/plugin.py — no live commands.

Run: .venv\\Scripts\\python.exe tests\\test_poller_unit.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # so `from runner.template import ...` works

_spec = importlib.util.spec_from_file_location(
    "_relay_test_poller",
    REPO_ROOT / "plugins" / "poller" / "plugin.py",
)
assert _spec and _spec.loader
poller = importlib.util.module_from_spec(_spec)
sys.modules["_relay_test_poller"] = poller
_spec.loader.exec_module(poller)


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


def test_render_template_dotted() -> None:
    print("test_render_template_dotted")
    data = {"a": {"b": {"c": "deep"}}, "n": 7, "missing": None}
    assert_eq(poller.render_template("{a.b.c}", data), "deep", "nested access")
    assert_eq(poller.render_template("{n}", data), "7", "int coerced")
    assert_eq(poller.render_template("{missing}", data), "", "None -> empty string")
    assert_eq(poller.render_template("{a.b.nope}", data), "{a.b.nope}", "missing renders literal")
    assert_eq(poller.render_template("{nope}", data), "{nope}", "top-level missing renders literal")


def test_resolve_dotted() -> None:
    print("test_resolve_dotted")
    assert_eq(
        poller._resolve_dotted({"a": {"b": 1}}, "a.b"), 1, "two-level",
    )
    assert_eq(
        poller._resolve_dotted({"a": 1}, "a"), 1, "single-level",
    )
    assert_eq(
        poller._resolve_dotted({"a": 1}, "a.b"), None,
        "non-dict intermediate -> None",
    )


def test_items_from_json() -> None:
    print("test_items_from_json")
    items = poller._items_from_json('[{"x": 1}, {"y": 2}]', "t")
    assert_eq(len(items), 2, "two-element array")
    assert_eq(items[0], {"x": 1}, "first dict")

    items = poller._items_from_json('{"single": true}', "t")
    assert_eq(len(items), 1, "single object treated as 1 item")

    items = poller._items_from_json("not json", "t")
    assert_eq(items, [], "non-JSON returns empty")

    items = poller._items_from_json("", "t")
    assert_eq(items, [], "empty stdout returns empty")


def test_items_from_lines() -> None:
    print("test_items_from_lines")
    items = poller._items_from_lines("a\nb\n\nc\n")
    assert_eq([i["line"] for i in items], ["a", "b", "c"], "blank lines skipped")


def test_template_render() -> None:
    print("test_template_render")
    item = {"metadata": {"namespace": "default", "name": "pod-1"},
            "status": {"reason": "OOMKilled"}}
    body = poller.render_template(
        "Failed pod: {metadata.namespace}/{metadata.name}\nReason: {status.reason}",
        item,
    )
    assert_eq(body, "Failed pod: default/pod-1\nReason: OOMKilled", "dotted template")


def test_parse_targets() -> None:
    print("test_parse_targets")
    cfgs, errs = poller.parse_targets([
        {"name": "ok", "command": "echo hi", "id_field": "x", "poll_sec": 10},
        {"name": "bad-mode", "command": "echo", "mode": "wat"},
        {"name": "no-cmd", "id_field": "x"},
        {"name": "json-no-id", "command": "echo", "mode": "json"},
        {"name": "lines-mode", "command": "echo", "mode": "lines"},  # ok, no id_field needed
        {"name": "low-poll", "command": "echo", "id_field": "x", "poll_sec": 1},
        {"name": "bad-regex", "command": "echo", "id_field": "x", "match": "[unclosed"},
    ])
    names = {c.name for c in cfgs}
    assert_eq(names, {"ok", "lines-mode"}, "valid targets kept")
    assert_eq(any("mode" in e for e in errs), True, "bad mode reported")
    assert_eq(any("'command'" in e for e in errs), True, "missing command reported")
    assert_eq(any("id_field" in e for e in errs), True, "missing id_field reported")
    assert_eq(any("poll_sec" in e for e in errs), True, "low poll reported")
    assert_eq(any("regex" in e for e in errs), True, "bad regex reported")


def test_validate_target() -> None:
    print("test_validate_target")
    cfg = poller.TargetConfig(
        name="bad space", command="x", id_field="y",
    )
    errs = cfg.validate()
    assert_eq(any("[a-zA-Z0-9_-]+" in e for e in errs), True, "name regex enforced")


def main() -> int:
    test_render_template_dotted()
    test_resolve_dotted()
    test_items_from_json()
    test_items_from_lines()
    test_template_render()
    test_parse_targets()
    test_validate_target()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
