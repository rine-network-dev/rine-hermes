"""Poll transport + the shared inbound ``_dispatch`` (exactly-once wake core).

``_dispatch(adapter, msg)`` is the single decision point both transports use (poll
here, SSE in ``_sse``) so the wake/dedupe/verify/ack policy lives in exactly one place.
For one decrypted inbound message it, in order:

1. **self-skip** — a message from the agent's own handle never wakes it, with one
   exception: a Funnel webhook (``rine.v1.webhook``) is a legitimate inbound event that
   the relay self-sends as the agent (``from == to``), so it IS dispatched.
2. **allowlist** — when ``RINE_ALLOWED_HANDLES`` is set, a disallowed sender is
   *quarantine-logged* (WARNING) and dropped — never silently swallowed.
2b. **terminal type** — an inbound type that closes an exchange rather than opening one
   (``rine.v1.error``, ``rine.v1.receipt``) is drained without waking a turn. Answering a
   peer's error notice with prose that the peer then answers is how two auto-replying
   agents talk to each other indefinitely. ``RINE_IGNORE_TYPES`` overrides the set; set
   it empty to wake on everything.
3. **refused sender** — a signature that does not cover the bytes, or one that covers
   them but belongs to an agent other than the row's sender, comes back with
   ``verification_status`` ``"invalid"`` / ``"sender-mismatch"``. Both are **logged at
   WARNING** (the security-relevant audit trail), then **drained (journal + ack)** so
   they never replay. They are **never** dispatched.
4. **decrypt_error** (any other) — genuinely unreadable (malformed / unsupported version):
   same drain (journal + ack), logged at INFO. Never dispatched: ciphertext / unreadable
   payload must not reach the agent. Draining (not "leave new") is deliberate — a
   real inbox with an unverifiable backlog would otherwise re-quarantine every poll.
5. **unverifiable** — dispatched with an ``[unverified sender]`` note, unless
   ``RINE_REQUIRE_VERIFIED=1`` blocks it.
6. **already consumed** — a redelivered, already-journaled id is **re-acked** (so it
   finally leaves the inbox) and **skipped** for dispatch (exactly-once across restart).
7. **in-flight** — an id whose turn is already running (registered a post-delivery
   callback from a prior poll) is **silently skipped** — no re-dispatch, no re-ack —
   because the poll interval can be shorter than a turn, so the server still shows it
   ``new`` until the turn finishes and acks it.
8. otherwise — build the source + ``MessageEvent(internal=True, auto_skill="rine:rine")``,
   register a **post-delivery callback** that journals (FIRST, durable) then acks
   (best-effort) **only after the agent turn completes**, open the turn (``adapter.turns``:
   the wake guard, the chat that outbound policy reads, the type the reply mirrors), then
   ``await handle_message`` to *schedule* the turn. The turn opens last of the four
   because only the callback and the dispatch ``except`` ever close one.
8x. **x402 payment frame** (a sub-case of step 8) — the three ``rine.v1.x402_*`` types wake
   the agent *payment-aware* (structured context + the ``rine_pay`` / ``rine_fulfill``
   tools) but NEVER stash a reply target: the prose auto-reply into the payment thread stays
   suppressed (the tools reply in-thread with the signed payload / receipt themselves; the
   turn's prose is dropped at ``adapter.send``). Opt-in gateway auto-pay
   (``RINE_X402_AUTO_PAY``, default OFF) may settle a sub-threshold quote here without a
   turn, then drain. An UNDECRYPTABLE x402 frame still drains at step 3/4 (no plaintext to
   act on), never waking the agent.

**Why a post-delivery callback?** ``BasePlatformAdapter.handle_message`` returns as soon
as the turn is *scheduled* (it spawns a background task — base.py:3839,3678), NOT after
the turn runs. Journaling/acking right after the await would record-and-ack before the
wake is durable → a crash mid-turn would lose the message (at-most-once). Hermes pops +
fires our callback only after ``_message_handler`` completes (base.py ~4510), so we defer
journal+ack to it: success → journaled+acked AFTER the turn (exactly-once); crash before
the callback → message stays ``new`` + un-journaled → re-dispatched next poll
(at-least-once, no loss); redelivery of a journaled id → re-acked, not re-dispatched.
It fires last, after every post-turn send the gateway makes, which is also what keeps
Hermes' human-facing crash text inside Hermes. ``RineAdapter.pop_post_delivery_callback``
is what makes the gateway hand it back at all — see the method for why.

**Known v0.1 edge case:** two inbound from the same sender in flight share a session key;
``register_post_delivery_callback`` *chains* callbacks for the same key and the pop is
one-shot, so both fire after whichever turn completes next. Acceptable for v0.1 (distinct
A2A conversations dominate); documented, not over-engineered.

A dispatch (scheduling) failure closes the turn again and leaves the id **neither
journaled nor acked**, so it stays eligible. The ciphertext is never read — only
``render_message_body`` (plaintext / decrypt_error).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from rine import MessageType, is_authenticity_failure
from rine.sender_key_words import SenderKeyRefusal

from hermes_rine._format import render_message_body
from hermes_rine._outbound import ERROR_TYPE
from hermes_rine._payer import maybe_auto_pay
from hermes_rine._turns import InboundTurn
from hermes_rine._x402 import is_x402, x402_wake_body

if TYPE_CHECKING:
    from rine import DecryptedMessage

    from hermes_rine.adapter import RineAdapter

logger = logging.getLogger("hermes_rine.poll")

# Inbound types that close an exchange rather than open one, so they never wake a turn.
# Matches the rine channel Eve ships (rine-eve/src/channel.ts): an error notice and a
# delivery receipt are for the record, and answering one — which the peer then answers —
# is how two auto-replying agents keep each other awake indefinitely. A drained message
# stays readable through ``rine_thread``; only the wake is suppressed.
#
# ``rine.v1.task_response`` is deliberately absent, so two hermes agents answering each
# other can still keep each other awake. A task_response is the answer to a question this
# agent asked with ``rine_send``; draining it by default would mean an agent is never woken
# by its own answer, which costs more than the loop it would prevent. An operator running
# two hermes agents that talk to each other adds the type via ``RINE_IGNORE_TYPES``.
_DEFAULT_IGNORE_TYPES = frozenset({ERROR_TYPE, MessageType.RECEIPT})


def ignored_inbound_types() -> frozenset[str]:
    """Types that never wake a turn — ``RINE_IGNORE_TYPES`` overrides the default set."""
    raw = os.getenv("RINE_IGNORE_TYPES")
    if raw is None:
        return _DEFAULT_IGNORE_TYPES
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _sender_handle(msg: DecryptedMessage) -> str:
    """The sender's handle (falling back to the agent uuid) for keying + self-skip."""
    return msg.sender_handle or (str(msg.from_agent_id) if msg.from_agent_id else "")


def _is_funnel_webhook(msg: DecryptedMessage) -> bool:
    """True for a Funnel webhook (``rine.v1.webhook``), the one legitimate self-send.

    The Funnel relay encrypts as the agent and self-sends (``from == to``), so a webhook
    would otherwise be swallowed by the self-skip. Every OTHER self-sent type stays
    suppressed (it is the agent's own outbound echo).
    """
    return msg.type == MessageType.WEBHOOK


def _hook_name(msg: DecryptedMessage) -> str | None:
    """The cleartext ``rine.hook_name`` for a Funnel webhook, or None.

    The relay puts the hook name in cleartext ``metadata`` so the agent can tell which
    hook fired without decrypting the body. Returns None for non-webhook messages or a
    missing/non-string value.
    """
    name = msg.metadata.get("rine.hook_name")
    return name if isinstance(name, str) and name else None


async def _ack(adapter: RineAdapter, message_id: str) -> None:
    """Best-effort ``mark_delivered`` ack; a failure is logged, never fatal."""
    try:
        await adapter.client.mark_delivered([message_id])
    except Exception:  # noqa: BLE001 — ack is best-effort; redelivery re-acks via journal
        logger.warning("rine wake: mark_delivered failed for %s", message_id, exc_info=True)


def _session_key_for(adapter: RineAdapter, source: Any) -> str:
    """Compute the Hermes session key for ``source`` exactly as ``handle_message`` does.

    Isolated here so a ``build_session_key`` signature change touches one place. The two
    ``*_per_user`` toggles mirror base.py:3857-3861 (read off ``config.extra``).
    """
    from gateway.platforms.base import build_session_key  # type: ignore[import-not-found]

    extra = getattr(adapter.config, "extra", {}) or {}
    return build_session_key(
        source,
        group_sessions_per_user=extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
    )


async def _dispatch(adapter: RineAdapter, msg: DecryptedMessage) -> None:
    """Apply the full wake/dedupe/verify/ack policy to one inbound message."""
    message_id = str(msg.id)
    sender = _sender_handle(msg)

    # 1. Never wake on our own outbound copy — EXCEPT a Funnel webhook, which the relay
    #    legitimately self-sends as the agent (``from == to``) and must wake on.
    if sender and sender == adapter.agent_handle and not _is_funnel_webhook(msg):
        return

    # 2. Allowlist: quarantine-LOG a disallowed sender, never silently drop.
    if adapter.allowed_handles is not None and sender not in adapter.allowed_handles:
        logger.warning(
            "rine wake: quarantined message %s from disallowed sender %r", message_id, sender
        )
        return

    # 2b. Terminal type: drain it (journal + ack) without waking a turn, so an exchange
    #     that has already closed cannot restart itself.
    if msg.type in ignored_inbound_types():
        logger.info(
            "rine wake: message %s is a %s; draining without waking a turn",
            message_id,
            msg.type,
        )
        _safe_journal(adapter, message_id)
        await _ack(adapter, message_id)
        return

    if msg.decrypt_error:
        # 3/4. Never wake on an unreadable message (no trusted plaintext). DRAIN it
        #      (journal + ack) so it cannot wedge the poll loop. A *refused sender*
        #      (forged signature, or one belonging to another agent) is logged at
        #      WARNING — the security-relevant audit trail — while other unreadable
        #      causes (malformed / unsupported version) log at INFO. Ciphertext is NEVER
        #      surfaced either way. The two are told apart by the SDK's own
        #      ``is_authenticity_failure`` rather than by the error prose, so the audit
        #      trail stays honest when the SDK rewords a message and a verdict added to
        #      ``VerificationStatus`` is classified the day it ships.
        #      NB: an earlier design left refused senders ``new`` for an "operator
        #      sweep", but a real inbox with an unverifiable backlog re-quarantines them
        #      every poll forever (a log/IO storm); draining + a loud WARNING is the
        #      correct, contained policy. The message is never dispatched, so this in no
        #      way weakens the "never wake on an untrusted message" invariant.
        if is_authenticity_failure(msg.verification_status):
            logger.warning(
                "rine wake: message %s refused (%s); not dispatching, draining (journal + ack)",
                message_id,
                msg.verification_status,
            )
        else:
            logger.info(
                "rine wake: message %s unreadable (%s); not dispatching, draining",
                message_id,
                msg.decrypt_error,
            )
        _safe_journal(adapter, message_id)
        await _ack(adapter, message_id)
        return

    # 5. Unverifiable: blocked under strict mode, else dispatched with a marker.
    if msg.verification_status == "unverifiable" and adapter.require_verified:
        logger.warning(
            "rine wake: message %s unverifiable and RINE_REQUIRE_VERIFIED=1; skipping", message_id
        )
        return

    # 6. Exactly-once: a redelivered, already-recorded id is re-acked, not re-dispatched.
    if adapter.journal.is_consumed(adapter.agent_handle, message_id):
        logger.debug("rine wake: message %s already consumed; re-acking only", message_id)
        await _ack(adapter, message_id)
        return

    # 7. In-flight: a prior poll already scheduled this id's turn (the poll interval can be
    #    shorter than a turn, so the server still shows it ``new``). Skip — no double-wake,
    #    no re-ack: the in-flight turn's post-delivery callback will journal + ack it.
    if message_id in adapter.turns:
        logger.debug("rine wake: message %s already in-flight; skipping", message_id)
        return

    # 8. Build the event and schedule the turn; journal + ack only AFTER it completes.
    is_group = bool(msg.group_handle or msg.group_id)
    chat_id = (msg.group_handle or str(msg.group_id)) if is_group else sender
    frame_is_x402 = is_x402(msg)

    # 8x. Opt-in gateway auto-pay (RINE_X402_AUTO_PAY, default OFF): settle a sub-threshold
    #     payment_required here WITHOUT a turn, then drain like a consumed frame. Any
    #     refusal (above threshold, no policy/wallet, caps, send failure) returns False and
    #     falls through to a normal payment-aware wake so a human/LLM decides.
    if frame_is_x402 and await maybe_auto_pay(adapter.client, msg):
        logger.info("rine wake: message %s auto-paid (x402); draining", message_id)
        _safe_journal(adapter, message_id)
        await _ack(adapter, message_id)
        return

    # One wake carries exactly one message, so this is a single-message render: a
    # fresh refusal explains a missing sender key in full rather than marking it.
    body = x402_wake_body(msg) if frame_is_x402 else render_message_body(msg, SenderKeyRefusal())
    if msg.verification_status == "unverifiable":
        body = f"[unverified sender] {body}"
    hook_name = _hook_name(msg)
    if hook_name is not None:
        body = f"[webhook: {hook_name}] {body}"

    event, source = _build_event(adapter, chat_id, is_group, body, msg, sender)

    async def on_complete() -> None:
        """Post-delivery: record (journal) BEFORE ack, each step failure-isolated.

        Runs only after the agent turn completes durably (Hermes fires it post-turn).
        A journal write failure degrades that single message to at-least-once (logged
        WARNING) without aborting anything; the ack is already best-effort. The turn is
        always closed, releasing the wake guard, the remembered inbound type and any x402
        prose suppression (so a later unrelated send to this chat is never swallowed).
        Never raises out.
        """
        try:
            _safe_journal(adapter, message_id)
            await _ack(adapter, message_id)
        finally:
            adapter.turns.end(message_id)

    session_key = _session_key_for(adapter, source)
    adapter.register_post_delivery_callback(session_key, on_complete)

    # Open the turn: its chat marks this chat as already answering (so the gateway's
    # interim narration stays inside Hermes while a cron delivery to another peer still
    # goes out), its type lets the routed reply mirror the message, and an x402 frame
    # additionally suppresses the turn's prose entirely — the pay/fulfill tools thread
    # the signed payload / receipt in-place themselves. Closed post-turn.
    # It opens HERE, immediately before the schedule, because ``end`` is only reachable
    # from the post-delivery callback and from the except below: a raise anywhere between
    # ``begin`` and the try would leave an entry that nothing ever removes, and an entry
    # that outlives its turn permanently blocks that id's wake guard and drops the chat's
    # unmarked outbound sends for the life of the process.
    adapter.turns.begin(
        message_id,
        InboundTurn(chat_id=chat_id, message_type=msg.type, x402=frame_is_x402),
    )
    try:
        await adapter.handle_message(event)
    except Exception:  # noqa: BLE001 — scheduling failed: close the turn, leave re-eligible
        adapter.turns.end(message_id)
        adapter.pop_post_delivery_callback(session_key)
        logger.error(
            "rine wake: handle_message failed for %s; leaving re-eligible",
            message_id,
            exc_info=True,
        )


def _safe_journal(adapter: RineAdapter, message_id: str) -> None:
    """Record a consumed id; a journal write failure degrades to at-least-once, never raises."""
    try:
        adapter.journal.mark_consumed(adapter.agent_handle, message_id)
    except Exception:  # noqa: BLE001 — journal write failed: log, abandon (at-least-once)
        logger.warning(
            "rine wake: journal write failed for %s; degrading to at-least-once",
            message_id,
            exc_info=True,
        )


def _build_event(
    adapter: RineAdapter,
    chat_id: str,
    is_group: bool,
    body: str,
    msg: DecryptedMessage,
    sender: str,
) -> tuple[Any, Any]:
    """Build a Hermes ``MessageEvent`` + its ``source`` (imports stay lazy-of-register)."""
    from gateway.platforms.base import MessageEvent, MessageType

    source = adapter.build_source(
        chat_id=chat_id,
        chat_name=chat_id,
        chat_type="group" if is_group else "dm",
        user_id=sender or None,
        user_name=sender or None,
        message_id=str(msg.id),
    )
    event = MessageEvent(
        text=body or "(empty message)",
        message_type=MessageType.TEXT,
        source=source,
        message_id=str(msg.id),
        internal=True,
        auto_skill="rine:rine",
    )
    return event, source


async def _poll_loop(adapter: RineAdapter) -> None:
    """Poll the agent's inbox for ``new`` messages every ``RINE_POLL_INTERVAL`` seconds."""
    while adapter.running:
        try:
            page = await adapter.client.inbox(status="new", limit=50)
            for msg in page:
                await _dispatch(adapter, msg)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — a transient poll error must not kill the loop
            logger.error("rine wake: poll error", exc_info=True)
        await asyncio.sleep(adapter.poll_interval)


__all__ = ["_dispatch", "_poll_loop", "ignored_inbound_types"]
