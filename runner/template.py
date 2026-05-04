"""Shared dotted-template Formatter for plugins.

Renders templates like `{a.b.c}` by walking nested dicts, instead of
Python's default attribute / item access. Missing keys return the literal
placeholder so a bad template never crashes the caller.

Used by `plugins/poller/` and `plugins/webhook/`. Plugins import via:

    from runner.template import render_template

(plugin folders are registered as packages, so the import works from the
plugin module's setup function.)
"""
from __future__ import annotations

import json
import string
from typing import Any


class _DotFormatter(string.Formatter):
    def get_field(self, field_name: str, args, kwargs):
        data = args[0] if args else kwargs
        if not isinstance(data, dict):
            return "{" + field_name + "}", field_name
        cur: Any = data
        for part in field_name.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return "{" + field_name + "}", field_name
        if isinstance(cur, (str, int, float, bool)):
            return str(cur), field_name
        if cur is None:
            return "", field_name
        try:
            return json.dumps(cur, ensure_ascii=False), field_name
        except (TypeError, ValueError):
            return str(cur), field_name


_FORMATTER = _DotFormatter()


def render_template(template: str, data: dict) -> str:
    """Render a template against a dict, supporting dotted-path keys."""
    return _FORMATTER.vformat(template, (data,), {})
