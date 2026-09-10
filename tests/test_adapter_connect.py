"""``RineAdapter.connect`` — the call the gateway makes to open the wake channel.

The gateway owns this call site, and hosts change it. A host that passes
``is_reconnect=True`` must not get a ``TypeError`` (that kills the wake channel silently,
with the gateway retrying forever), and a host that reuses one adapter instance across
reconnects must not end up with two listeners on one inbox — two listeners double every
wake and race each other's acks.

Driven through the real ``RineAdapter`` with a fake client, so the assertion holds with or
without Hermes installed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from .conftest import SELF_HANDLE


class _Agent:
    def __init__(self, handle: str) -> None:
        self.handle = handle
        self.revoked_at = None


class _WhoAmI:
    def __init__(self, handle: str) -> None:
        self.agents = [_Agent(handle)]


class FakeConnectClient:
    """A client whose ``inbox`` never returns, so the listener task stays alive."""

    def __init__(self) -> None:
        self.whoami_calls = 0

    async def whoami(self) -> _WhoAmI:
        self.whoami_calls += 1
        return _WhoAmI(SELF_HANDLE)

    async def inbox(self, **_: Any) -> list[Any]:
        await asyncio.sleep(3600)
        return []


def _adapter(client: FakeConnectClient):
    """A bare ``RineAdapter`` carrying only what ``connect`` and the poll loop read."""
    from hermes_rine.adapter import RineAdapter

    adapter = RineAdapter.__new__(RineAdapter)
    adapter._client = client
    adapter._transport = "poll"
    adapter._poll_interval = 3600
    adapter._agent_handle = ""
    adapter._listen_task = None
    adapter._running = False
    # ``connect`` announces the gateway-control posture, so these three are part of what
    # it reads. This is what ``__init__`` produces from an unset environment: control off,
    # nobody named, and the WAKE allowlist allow-all — ``None`` here meaning the opposite
    # of what ``None`` means on the line above it (see ``parse_control_handles``).
    adapter._gateway_control_on = False
    adapter._gateway_control_handles = frozenset()
    adapter._allowed_handles = None
    return adapter


async def test_connect_accepts_the_is_reconnect_keyword() -> None:
    adapter = _adapter(FakeConnectClient())
    try:
        assert await adapter.connect(is_reconnect=True) is True
        assert adapter.agent_handle == SELF_HANDLE
    finally:
        await adapter._stop_listener()


async def test_connect_still_binds_without_arguments() -> None:
    adapter = _adapter(FakeConnectClient())
    try:
        assert await adapter.connect() is True
    finally:
        await adapter._stop_listener()


async def test_reconnecting_leaves_exactly_one_listener_running() -> None:
    # A host that reuses the adapter instance calls connect() twice. Without cancelling
    # the first listener, both poll the same inbox — every message wakes the agent twice.
    adapter = _adapter(FakeConnectClient())
    try:
        await adapter.connect()
        first = adapter._listen_task

        await adapter.connect(is_reconnect=True)
        second = adapter._listen_task

        await asyncio.sleep(0)  # let the cancellation land
        assert second is not first
        assert first.cancelled() or first.done()
        assert not second.done()
    finally:
        await adapter._stop_listener()


async def test_failed_auth_reports_failure_without_leaving_a_listener() -> None:
    class _Rejecting(FakeConnectClient):
        async def whoami(self) -> _WhoAmI:
            raise RuntimeError("no credentials")

    adapter = _adapter(_Rejecting())

    assert await adapter.connect() is False
    assert adapter._listen_task is None


@pytest.mark.parametrize("transport", ["poll", "sse"])
async def test_connect_is_idempotent_for_either_transport(transport: str) -> None:
    adapter = _adapter(FakeConnectClient())
    adapter._transport = transport
    try:
        await adapter.connect()
        first = adapter._listen_task
        await adapter.connect(is_reconnect=True)
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
    finally:
        await adapter._stop_listener()
