"""``RineAdapter`` — the Hermes gateway platform adapter (inbound wake channel).

Modelled on the in-tree ``EmailAdapter``: ``connect()`` authenticates then spawns a
listener task; the listener (``_poll_loop`` or the SSE ``_sse_loop``) turns each new
rine message into a ``MessageEvent`` and ``await``s ``self.handle_message`` to wake the
gateway agent; the agent's reply routes back out through ``send()`` as a rine send.
Exactly-once across restarts via the durable ``ConsumedJournal`` (record-before-ack).

Every uncertain Hermes signature is isolated here (R1/R11). The ``Platform`` arg is
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
    SendResult,
)
from rine import MessageType

from hermes_rine._client import _format_error, build_client
from hermes_rine._journal import ConsumedJournal
from hermes_rine._replystash import ReplyStash

if TYPE_CHECKING:
    from rine import RineClient

logger = logging.getLogger("hermes_rine.adapter")

_DEFAULT_POLL_INTERVAL = 30


def _parse_allowed_handles(raw: str) -> set[str] | None:
    """Parse ``RINE_ALLOWED_HANDLES`` into a set, or None when unset (allow all)."""
    handles = {h.strip() for h in raw.split(",") if h.strip()}
    return handles or None


class RineAdapter(BasePlatformAdapter):
    """Gateway adapter that wakes the agent on inbound rine mail and replies back out."""

    def __init__(self, config: PlatformConfig) -> None:
        """Read wake env vars; defer the client + journal to lazy first use."""
        super().__init__(config, Platform("rine"))
        self._transport = os.getenv("RINE_TRANSPORT", "poll").strip().lower() or "poll"
        self._poll_interval = int(os.getenv("RINE_POLL_INTERVAL", str(_DEFAULT_POLL_INTERVAL)))
        self._allowed_handles = _parse_allowed_handles(os.getenv("RINE_ALLOWED_HANDLES", ""))
        self._require_verified = os.getenv("RINE_REQUIRE_VERIFIED") == "1"
        self._client: RineClient | None = None
        self._journal = ConsumedJournal()
        self._agent_handle = ""
        self._listen_task: asyncio.Task[None] | None = None
        # In-flight ids: a turn scheduled by a prior poll but not yet completed
        # (post-delivery callback unfired). Guards against a poll interval shorter
        # than a turn re-dispatching the same still-``new`` message (double-wake).
        self._inflight: set[str] = set()
        # Latest inbound message id per DM chat_id, so the agent's reply turn replies
        # IN-PLACE (same conversation) instead of forking via send(). Keyed by chat_id
        # (= the DM sender handle, what `send` receives), so `send` looks it up without
        # recomputing the session key. REQ-FORK-01.
        self._reply_stash = ReplyStash()
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
    def inflight(self) -> set[str]:
        """Ids whose turn is scheduled but not yet completed (in-flight wake guard)."""
        return self._inflight

    def record_reply_to(self, chat_id: str, message_id: str) -> None:
        """Stash the latest inbound message id for a DM ``chat_id`` (REQ-FORK-01).

        ``send`` reads this so a reply to a DM lands in the inbound conversation
        (reply-in-place) instead of forking. Bounded; see :class:`ReplyStash`.
        """
        self._reply_stash.record(chat_id, message_id)

    @property
    def running(self) -> bool:
        """True while the listener should keep running (mirrors base ``_running``)."""
        return self._running

    # -- lifecycle ---------------------------------------------------------------------

    async def connect(self) -> bool:
        """Authenticate (cheap ``whoami``) then spawn the transport listener."""
        from hermes_rine._poll import _poll_loop
        from hermes_rine._sse import _sse_loop

        try:
            me = await self.client.whoami()
        except Exception as err:  # noqa: BLE001 — surface the reason, fail the connect
            logger.error("[rine] connect failed: %s", _format_error(err))
            return False

        live = [a.handle for a in me.agents if a.revoked_at is None]
        self._agent_handle = live[0] if live else ""
        self._running = True
        loop = _sse_loop if self._transport == "sse" else _poll_loop
        self._listen_task = asyncio.create_task(loop(self))
        logger.info(
            "[rine] connected as %s (transport=%s)", self._agent_handle or "?", self._transport
        )
        return True

    async def disconnect(self) -> None:
        """Stop the listener, close the client + journal."""
        self._running = False
        if self._listen_task is not None:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task
            self._listen_task = None
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._journal.close()
        logger.info("[rine] disconnected")

    # -- outbound --------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Route the agent's reply back to rine as a 1:1 / group send (or in-thread reply).

        REQ-FORK-01 — a DM with a stashed inbound id and no explicit ``reply_to`` replies
        IN-PLACE via ``client.reply`` (same conversation) instead of ``client.send`` (which
        forks a NEW conversation). The change is conversation-routing only: we pass an
        explicit ``message_type=TASK_REQUEST`` so the reply keeps the prior ``send`` default
        (``client.reply``'s SDK default is ``TASK_RESPONSE``, which is loop-guard terminal and
        would silently stop a multi-turn peer exchange). Group sends and explicit-``reply_to``
        callers are unchanged; ``_poll._dispatch`` records the stash per inbound DM.

        A Funnel ``rine.v1.webhook`` is a self-send event (``from == to``): after the turn the
        gateway auto-replies to the "sender", which for a webhook is THIS agent. rine forbids
        self-sends (422 "Cannot send a message to the same agent"), and a webhook is a one-way
        event with no one to reply to — so a self-addressed reply is skipped gracefully rather
        than surfacing a spurious error to the agent. This is the outbound counterpart to the
        inbound self-skip exemption in ``_poll._dispatch`` (REQ-FUNNEL self-send symmetry). The
        agent's real actions — sends to peers or groups — are unaffected.
        """
        is_group = chat_id.startswith("#")
        if (
            not is_group
            and self._agent_handle
            and chat_id.strip().lower() == self._agent_handle.strip().lower()
        ):
            logger.info(
                "[rine] skipping self-addressed reply (a webhook self-send has no reply target)"
            )
            return SendResult(success=True)
        # Reply-in-place for a DM with a known inbound id (no explicit reply_to, not a group).
        stashed_reply_to = reply_to or (None if is_group else self._reply_stash.get(chat_id))
        logger.debug("[rine] outbound to %s (reply_to=%s)", chat_id, stashed_reply_to)
        try:
            if stashed_reply_to:
                msg = await self.client.reply(
                    stashed_reply_to,
                    {"text": content},
                    message_type=MessageType.TASK_REQUEST,
                )
            else:
                msg = await self.client.send(chat_id, {"text": content})
        except Exception as err:  # noqa: BLE001 — never raise into the gateway send path
            logger.error("[rine] send to %s failed: %s", chat_id, err)
            return SendResult(success=False, error=_format_error(err))
        return SendResult(success=True, message_id=str(msg.id))

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Minimal chat descriptor — rine chat_ids are handles (``#`` prefixed for groups)."""
        is_group = chat_id.startswith("#")
        return {"name": chat_id, "type": "group" if is_group else "dm", "chat_id": chat_id}


__all__ = ["RineAdapter"]
