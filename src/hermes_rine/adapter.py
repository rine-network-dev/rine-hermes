"""``RineAdapter`` — the Hermes gateway platform adapter (inbound wake channel).

Modelled on the in-tree ``EmailAdapter``: ``connect()`` authenticates then spawns a
listener task; the listener (``_poll_loop`` or the SSE ``_sse_loop``) turns each new
rine message into a ``MessageEvent`` and ``await``s ``self.handle_message`` to wake the
gateway agent; the agent's reply routes back out through ``send()`` as a rine send.
Exactly-once across restarts via the durable ``ConsumedJournal`` (record-before-ack).

Every uncertain Hermes signature is isolated here. The ``Platform`` arg is
``Platform("rine")`` — a string pseudo-member the enum's ``_missing_`` mints for a
runtime-registered plugin platform (``platform_registry.is_registered`` is True by the
time the gateway constructs the adapter, because ``register_platform`` ran first). The
async ``RineClient`` is built **lazily** on first connect (never at import / ``register``)
and closed on ``disconnect``; the journal opens lazily likewise.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any

from gateway.config import Platform, PlatformConfig  # type: ignore[import-not-found]
from gateway.platforms.base import (  # type: ignore[import-not-found]
    BasePlatformAdapter,
    ProcessingOutcome,
    SendResult,
)

from hermes_rine._client import _format_error, build_client, primary_handle
from hermes_rine._gateway_control import (
    ALLOW_ENV,
    HANDLES_ENV,
    grants_control,
    log_configuration,
    parse_control_handles,
    switch_on,
)
from hermes_rine._journal import ConsumedJournal
from hermes_rine._outbound import notify_failure, route_reply
from hermes_rine._setaside import SetAsideStore
from hermes_rine._turns import InboundTurns

if TYPE_CHECKING:
    from collections.abc import Callable

    from rine import RineClient

logger = logging.getLogger("hermes_rine.adapter")

_DEFAULT_POLL_INTERVAL = 30

# Hermes reads an allowlist the same way but additionally honours a bare ``*`` and, for a
# handle containing ``@``, also admits the local part before it. This parser honours ``*``
# too, so ``RINE_ALLOWED_HANDLES=*`` means the same thing at both layers; it deliberately
# does NOT split on ``@``, because every rine handle contains one and admitting the local
# part alone would let ``peer@other-org`` through an allowlist naming ``peer@my-org``.
# The gateway layer is therefore the looser of the two and this one is the tighter — that
# asymmetry is intended, not a bug to reconcile.
_ALLOW_ALL_HANDLES = "*"


def _parse_allowed_handles(raw: str) -> set[str] | None:
    """Parse ``RINE_ALLOWED_HANDLES`` into a set, or None for allow-all (unset or ``*``)."""
    handles = {h.strip() for h in raw.split(",") if h.strip()}
    if not handles or _ALLOW_ALL_HANDLES in handles:
        return None
    return handles


async def _cancel(task: asyncio.Task[None] | None) -> None:
    """Cancel and await one background task, if there is one."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class RineAdapter(BasePlatformAdapter):
    """Gateway adapter that wakes the agent on inbound rine mail and replies back out."""

    # A rine message cannot be edited once sent: it is sealed to the recipient and
    # POSTed, and the protocol has no update verb. Declaring that keeps the gateway's
    # streaming consumer from previewing a partial answer it would then try to edit —
    # with editing unavailable the gateway skips streaming and sends the finished answer
    # once. The consumer that reads it is ``_build_stream_consumer_config``, via its own
    # ``_adapter_supports_edit = getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)``
    # (named as bare symbols: the module they live in was moved by an upstream
    # decomposition release, and a path here would rot again). Every other non-editing
    # platform in the host declares the same flag (gateway/platforms/signal.py,
    # gateway/platforms/weixin.py, plugins/platforms/wecom/adapter.py).
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig) -> None:
        """Read wake env vars; defer the client + journal to lazy first use."""
        super().__init__(config, Platform("rine"))
        self._transport = os.getenv("RINE_TRANSPORT", "poll").strip().lower() or "poll"
        self._poll_interval = int(os.getenv("RINE_POLL_INTERVAL", str(_DEFAULT_POLL_INTERVAL)))
        self._allowed_handles = _parse_allowed_handles(os.getenv("RINE_ALLOWED_HANDLES", ""))
        self._require_verified = os.getenv("RINE_REQUIRE_VERIFIED") == "1"
        # Gateway control: read once here, decided per message by
        # ``allows_gateway_control``. Both default to "nobody" — see
        # :mod:`hermes_rine._gateway_control` for the full truth table.
        self._gateway_control_on = switch_on(os.getenv(ALLOW_ENV))
        self._gateway_control_handles = parse_control_handles(os.getenv(HANDLES_ENV, ""))
        self._client: RineClient | None = None
        self._journal = ConsumedJournal()
        # The unreadable messages this agent has parked. Shares the journal's
        # file on its own connection; a separate TABLE, because "could not read it" is
        # the opposite of the journal's "handled it" — see :mod:`hermes_rine._setaside`.
        self._setaside = SetAsideStore()
        self._agent_handle = ""
        self._listen_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        # Turns a rine message woke that are scheduled but not finished: the wake guard
        # (no double-dispatch while a turn runs) and the context every outbound decision
        # reads. Opened by ``_poll._dispatch``, closed by the post-delivery callback.
        self._turns = InboundTurns()
        logger.info("[rine] adapter initialised (transport=%s)", self._transport)

    # -- properties the transports read (one stable seam) ------------------------------

    @property
    def client(self) -> RineClient:
        """The lazily-built async ``RineClient`` (built on first ``connect``)."""
        if self._client is None:
            self._client = build_client()
        return self._client

    @property
    def journal(self) -> ConsumedJournal:
        """The durable consumed-id journal."""
        return self._journal

    @property
    def setaside(self) -> SetAsideStore:
        """The durable set-aside list of messages this agent could not read."""
        return self._setaside

    @property
    def agent_handle(self) -> str:
        """This agent's own handle (resolved in ``connect`` via ``whoami``)."""
        return self._agent_handle

    @property
    def allowed_handles(self) -> set[str] | None:
        """Sender allowlist (``RINE_ALLOWED_HANDLES``) or None for allow-all."""
        return self._allowed_handles

    @property
    def require_verified(self) -> bool:
        """Whether ``RINE_REQUIRE_VERIFIED=1`` blocks unverifiable senders."""
        return self._require_verified

    @property
    def poll_interval(self) -> int:
        """Seconds between inbox polls (``RINE_POLL_INTERVAL``)."""
        return self._poll_interval

    @property
    def turns(self) -> InboundTurns:
        """The inbound turns currently in flight (wake guard + outbound context)."""
        return self._turns

    @property
    def running(self) -> bool:
        """True while the listener should keep running (mirrors base ``_running``)."""
        return self._running

    def allows_gateway_control(self, sender: str) -> bool:
        """Whether ``sender``'s message may resolve gateway commands and control prompts.

        Read-only and I/O-free: both settings were parsed once in ``__init__``, so the
        per-message decision costs a set lookup. Off for every sender unless the channel
        switch is on AND the sender is named. The wake allowlist has already run by the
        time this is asked, so the answer is a subset of the admitted senders — but that
        allowlist is allow-all when ``RINE_ALLOWED_HANDLES`` is unset, so it is a bound
        only when the operator set it. ``connect`` says which of the two it is.
        """
        return grants_control(
            enabled=self._gateway_control_on,
            handles=self._gateway_control_handles,
            sender=sender,
        )

    # -- lifecycle ---------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Authenticate (cheap ``whoami``) then spawn the transport listener.

        ``is_reconnect`` is accepted for call-site compatibility and deliberately unused:
        rine holds no server-side ephemeral queue that a cold boot drops. The durable
        consumed-id journal already provides exactly-once across a reconnect, and both
        transports are cursor-free, so a reconnect needs no distinct code path. It is
        keyword-only with a default, so a host that calls a bare ``connect()`` still binds.

        Connecting is idempotent: a host that reuses one adapter instance across
        reconnects would otherwise leave the previous listener running, and two listeners
        on one inbox double every wake.
        """
        from hermes_rine._poll import _poll_loop
        from hermes_rine._sse import _sse_loop
        from hermes_rine._sweep import _sweep_loop

        await self._stop_listener()
        try:
            me = await self.client.whoami()
        except Exception as err:  # noqa: BLE001 — surface the reason, fail the connect
            logger.error("[rine] connect failed: %s", _format_error(err))
            return False

        self._agent_handle = primary_handle(me)
        self._running = True
        # Say the gateway-control posture out loud here rather than per message: a knob
        # that is half-configured is off, and a user who set one of its two halves and
        # heard nothing would reasonably read the silence as "it worked".
        log_configuration(
            enabled=self._gateway_control_on,
            handles=self._gateway_control_handles,
            wake_allowlist_named=self._allowed_handles is not None,
        )
        loop = _sse_loop if self._transport == "sse" else _poll_loop
        self._listen_task = asyncio.create_task(loop(self))
        # The set-aside retry runs on its own tick, NOT inside the listener: ``_sse_loop``
        # sits in ``_consume_stream`` for hours without returning, and SSE is the
        # transport that needs the retry most (a yielded frame is already a delivery
        # server-side). One task per connect, torn down with the listener.
        self._sweep_task = asyncio.create_task(_sweep_loop(self))
        logger.info(
            "[rine] connected as %s (transport=%s)", self._agent_handle or "?", self._transport
        )
        return True

    async def _stop_listener(self) -> None:
        """Cancel and await the background tasks (transport listener + set-aside sweep).

        Both are cancelled together because both are spawned together by ``connect``,
        and a sweep left running against a disconnected adapter would keep re-reading
        parked ids on a timer with nothing to dispatch them to.
        """
        await _cancel(self._listen_task)
        self._listen_task = None
        await _cancel(self._sweep_task)
        self._sweep_task = None

    async def disconnect(self) -> None:
        """Stop the background tasks, close the client + journal + set-aside store."""
        self._running = False
        await self._stop_listener()
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._journal.close()
        self._setaside.close()
        logger.info("[rine] disconnected")

    def pop_post_delivery_callback(
        self, session_key: str, *, generation: int | None = None
    ) -> Callable[[], Any] | None:
        """Pop the callback that ends a turn, ignoring generation for our own entry.

        Journal + ack (and the release of the in-flight turn) happen inside a
        post-delivery callback the gateway fires once the turn is over, so a callback
        the gateway declines to pop is a message that stays ``new`` forever.

        The gateway pops with the run generation it bound to the turn and returns None
        for any entry registered without one (``gateway/platforms/base.py``
        ``pop_post_delivery_callback``). An adapter cannot register with a matching
        generation: the token is minted inside the message handler, i.e. *after*
        ``_dispatch`` has already registered and handed the turn over. So a
        generation-free entry — which is only ever ours — is popped generation-free,
        while an entry that carries a generation keeps the gateway's ownership check
        that stops a stale run from clearing a fresher run's callback.
        """
        entry = getattr(self, "_post_delivery_callbacks", {}).get(session_key)
        if callable(entry):
            generation = None
        return super().pop_post_delivery_callback(session_key, generation=generation)

    # -- outbound --------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Relay the turn's final answer to rine, in the conversation it answers.

        Every gateway send lands here — the final routed response, interim narration, and
        Hermes' own operator notices alike — so the relay/no-relay decision and the reply's
        message type live in ``_outbound``, next to the markers they read.
        """
        return await route_reply(self, chat_id, content, reply_to, metadata)

    async def on_processing_complete(self, event: Any, outcome: ProcessingOutcome) -> None:
        """Tell the peer when a turn failed, instead of leaving it on silence.

        Hermes writes its own crash text for a person in a chat window and sends it with
        neither final-reply marker, so it is not relayed. A rine peer gets a
        ``rine.v1.error`` in the same thread instead.
        """
        if outcome is not ProcessingOutcome.FAILURE:
            return
        chat_id = str(getattr(getattr(event, "source", None), "chat_id", "") or "")
        if not chat_id:
            return
        await notify_failure(self, chat_id, getattr(event, "message_id", None))

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Minimal chat descriptor — rine chat_ids are handles (``#`` prefixed for groups)."""
        is_group = chat_id.startswith("#")
        return {"name": chat_id, "type": "group" if is_group else "dm", "chat_id": chat_id}


__all__ = ["RineAdapter"]
