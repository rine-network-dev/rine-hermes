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
import os
from typing import Any

import pytest

from hermes_rine.platform import HOME_CHANNEL_ENV, NO_HOME_CHANNEL

from .conftest import SELF_HANDLE


@pytest.fixture(autouse=True)
def _isolate_home_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """``connect`` seeds ``RINE_HOME_CHANNEL`` in the real process environment.

    That is the point of it — the variable is read by Hermes, not by us, so there is
    nowhere else to put it. But a real ``os.environ`` write outlives the test that made
    it, and every later assertion about the variable would then depend on which test ran
    connect first. ``monkeypatch`` restores the original value on teardown, which also
    lets a test below set one and have it cleaned up.
    """
    monkeypatch.delenv(HOME_CHANNEL_ENV, raising=False)


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
    adapter._sweep_task = None
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
    assert adapter._sweep_task is None
    # The handle is only known once whoami answers, so a failed connect must not have
    # seeded a home channel — writing "-" here would silently disable the notice for a
    # gateway that never connected.
    assert HOME_CHANNEL_ENV not in os.environ


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


async def test_connect_spawns_the_setaside_sweep_and_stop_cancels_it() -> None:
    # The retry runs ON A TICK, not on demand only. The tick is its
    # own task because ``_sse_loop`` sits inside ``_consume_stream`` for hours without
    # returning, so a sweep folded into the listener's loop body would never fire on the
    # transport that needs it most. Mutation guard: drop the ``create_task(_sweep_loop)``
    # from ``connect`` and the first assertion goes red; drop the sweeper from
    # ``_stop_listener`` and the last one does.
    adapter = _adapter(FakeConnectClient())
    try:
        assert await adapter.connect() is True
        sweeper = adapter._sweep_task
        assert sweeper is not None
        assert not sweeper.done()
        assert sweeper is not adapter._listen_task
    finally:
        await adapter._stop_listener()
    assert adapter._sweep_task is None
    assert sweeper.cancelled() or sweeper.done()


async def test_reconnecting_leaves_exactly_one_sweep_running() -> None:
    # Same hazard as the listener: two sweepers on one inbox re-read every parked id twice
    # per tick and race each other's ``forget``.
    adapter = _adapter(FakeConnectClient())
    try:
        await adapter.connect()
        first = adapter._sweep_task
        await adapter.connect(is_reconnect=True)
        second = adapter._sweep_task
        await asyncio.sleep(0)
        assert second is not first
        assert first.cancelled() or first.done()
        assert not second.done()
    finally:
        await adapter._stop_listener()


# --- the home channel is seeded at its SOURCE, not filtered at the boundary -------------


async def test_connect_seeds_the_home_channel_so_the_notice_is_never_generated() -> None:
    # Hermes emits "📬 No home channel is set for Rine. Type /sethome to make this chat
    # your home." on the first message of a session, behind an `if not os.getenv(env_var)`
    # gate keyed by the `cron_deliver_env_var` we register in platform.py. An A2A peer
    # cannot type /sethome, and the sentence fingerprints the host stack, so the operator
    # read it as the product leaking its harness. The outbound rule drops it while a turn
    # is in flight, but that is a boundary catch on a string; this makes it unreachable.
    adapter = _adapter(FakeConnectClient())
    try:
        assert await adapter.connect() is True
        assert os.environ[HOME_CHANNEL_ENV] == NO_HOME_CHANNEL
        # A sentinel, NOT this agent's handle: seeding a resolvable destination would make a
        # `deliver=rine` cron job with no home channel set hit the self-addressed skip and
        # report success for a message that never left. See platform.NO_HOME_CHANNEL.
        assert adapter.agent_handle != NO_HOME_CHANNEL
        assert "@" not in NO_HOME_CHANNEL
    finally:
        await adapter._stop_listener()


async def test_connect_keeps_a_home_channel_the_operator_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # setdefault, not assignment: an operator who pointed Hermes at a real home channel
    # configured a delivery target, and connecting must not redirect it to this agent.
    monkeypatch.setenv(HOME_CHANNEL_ENV, "ops@example.rine.network")
    adapter = _adapter(FakeConnectClient())
    try:
        await adapter.connect()
        assert os.environ[HOME_CHANNEL_ENV] == "ops@example.rine.network"
    finally:
        await adapter._stop_listener()
