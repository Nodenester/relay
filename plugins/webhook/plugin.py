"""Generic HTTP webhook receiver plugin.

Listens on a configured host/port via aiohttp. Every POST to a configured
endpoint path emits one event. Supports GitHub-style HMAC-SHA256 signature
verification when a secret env var is set on the endpoint.

Config shape::

    "webhook": {
      "enabled": true,
      "host": "127.0.0.1",
      "port": 7700,
      "endpoints": [
        {
          "name": "github-push",
          "path": "/github",
          "secret_env": "RELAY_GITHUB_WEBHOOK_SECRET",
          "template": "GitHub event {header.x-github-event} on {payload.repository.full_name}\\n{body}"
        },
        {
          "name": "generic",
          "path": "/in",
          "template": "{body}"
        }
      ]
    }

Template fields:

- `{body}` — raw request body (string, truncated to 4000 chars)
- `{header.NAME}` — request header (NAME is lower-case)
- `{payload.PATH}` — dotted access into the JSON-decoded body, e.g.
  `{payload.repository.full_name}`. Resolves to the literal placeholder
  if the body isn't JSON or the path doesn't exist.
- `{query.NAME}` — query-string value
- `{path}` — the request path
- `{method}` — always POST for this plugin

Every request is logged with the endpoint name, source IP, and HTTP status.
HMAC-mismatched requests get HTTP 401 and are NOT emitted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from runner.template import render_template

log = logging.getLogger("relay.plugin.webhook")

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")
_BODY_MAX = 4000


@dataclass
class EndpointConfig:
    name: str
    path: str
    secret_env: str | None = None
    template: str = "{body}"

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name:
            errors.append("endpoint missing 'name'")
        elif _NAME_SAFE.search(self.name):
            errors.append(
                f"endpoint '{self.name}': name must match [a-zA-Z0-9_-]+"
            )
        if not self.path:
            errors.append(f"endpoint '{self.name}': missing 'path'")
        elif not self.path.startswith("/"):
            errors.append(
                f"endpoint '{self.name}': path must start with '/' "
                f"(got {self.path!r})"
            )
        return errors


def _verify_hmac(body_bytes: bytes, header_value: str, secret: str) -> bool:
    """GitHub-style: header is `sha256=<hex>`."""
    if not header_value or "=" not in header_value:
        return False
    algo, _, sent_hex = header_value.partition("=")
    if algo != "sha256":
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body_bytes, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sent_hex)


def _truncate(text: str, limit: int = _BODY_MAX) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _build_handler(api, cfg: EndpointConfig):
    async def handler(request: web.Request) -> web.Response:
        peer = request.remote or "?"
        body_bytes = await request.read()
        body_text = body_bytes.decode("utf-8", errors="replace")

        if cfg.secret_env:
            secret = os.environ.get(cfg.secret_env)
            if not secret:
                log.warning(
                    "endpoint %r: secret_env %r not set, refusing requests",
                    cfg.name, cfg.secret_env,
                )
                return web.Response(status=503, text="secret not configured")
            sig = (
                request.headers.get("X-Hub-Signature-256")
                or request.headers.get("x-hub-signature-256")
                or ""
            )
            if not _verify_hmac(body_bytes, sig, secret):
                log.warning(
                    "endpoint %r: HMAC mismatch from %s", cfg.name, peer,
                )
                return web.Response(status=401, text="bad signature")

        try:
            payload = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            payload = {}

        fields: dict[str, Any] = {
            "body": _truncate(body_text),
            "path": request.path,
            "method": request.method,
            "header": {k.lower(): v for k, v in request.headers.items()},
            "query": dict(request.query),
            "payload": payload if isinstance(payload, dict) else {"_": payload},
        }

        try:
            rendered = render_template(cfg.template, fields)
        except Exception:
            log.exception("endpoint %r: template render failed", cfg.name)
            rendered = body_text

        await api.emit(
            body=_truncate(rendered),
            metadata={
                "endpoint": cfg.name,
                "path": request.path,
                "remote": peer,
            },
        )
        log.info(
            "endpoint %r: 200 OK from %s (%d bytes)",
            cfg.name, peer, len(body_bytes),
        )
        return web.Response(status=200, text="ok")

    return handler


def parse_endpoints(raw: Any) -> tuple[list[EndpointConfig], list[str]]:
    out: list[EndpointConfig] = []
    errors: list[str] = []
    if raw is None:
        return out, errors
    if not isinstance(raw, list):
        errors.append("'endpoints' must be a list")
        return out, errors
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"endpoints[{i}] must be an object")
            continue
        cfg = EndpointConfig(
            name=str(entry.get("name", "")),
            path=str(entry.get("path", "")),
            secret_env=entry.get("secret_env") or None,
            template=str(entry.get("template", "{body}")),
        )
        entry_errors = cfg.validate()
        if cfg.name in seen_names:
            entry_errors.append(f"duplicate endpoint name '{cfg.name}'")
        if cfg.path in seen_paths:
            entry_errors.append(f"duplicate endpoint path '{cfg.path}'")
        if entry_errors:
            errors.extend(entry_errors)
            continue
        seen_names.add(cfg.name)
        seen_paths.add(cfg.path)
        out.append(cfg)
    return out, errors


async def _serve(api, host: str, port: int, endpoints: list[EndpointConfig]) -> None:
    app = web.Application()
    for cfg in endpoints:
        app.router.add_post(cfg.path, _build_handler(api, cfg))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    log.info(
        "webhook server listening on http://%s:%d (endpoints: %s)",
        host, port,
        ", ".join(f"{e.name}@{e.path}" for e in endpoints),
    )
    # Stay alive until cancelled by the runner shutting down.
    try:
        while True:
            import asyncio
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def setup(api) -> None:
    host = str(api.config.get("host", "127.0.0.1"))
    port = int(api.config.get("port", 7700))
    endpoints, errors = parse_endpoints(api.config.get("endpoints"))
    for err in errors:
        log.error("webhook config: %s", err)
    if not endpoints:
        log.warning("webhook plugin enabled but no valid endpoints configured")
        return
    api.spawn(_serve(api, host, port, endpoints))
