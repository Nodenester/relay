"""Integration test plugin — proves event-flow and tool-dispatch end-to-end.

When enabled, emits a marker event on startup. Exposes one tool. Used by
tests/run_integration_test.py.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("relay.plugin.testplug")

STARTUP_MARKER = "RELAY_TEST_ACK"


async def setup(api) -> None:
    @api.tool("Test tool — return the input multiplied by 2.")
    async def double(n: int) -> int:
        """Return n*2."""
        return n * 2

    async def emit_marker():
        # small delay so Claude is ready
        await asyncio.sleep(3)
        await api.emit(
            body=(
                "This is an integration test. Reply with exactly "
                f"'{STARTUP_MARKER}' and nothing else, then call the "
                "relay tool 'testplug_double' with n=21."
            ),
            metadata={"role": "integration-test"},
        )
        log.info("Test event emitted")

    api.spawn(emit_marker())
