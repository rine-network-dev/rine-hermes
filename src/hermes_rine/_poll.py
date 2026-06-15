"""Poll transport + the shared inbound ``_dispatch`` (exactly-once wake core).

``_dispatch(adapter, msg)`` is the single decision point both transports use (poll
here, SSE in ``_sse``) so the wake/dedupe/verify/ack policy lives in exactly one place.
For one decrypted inbound message it, in order:

1. **self-skip** — a message from the agent's own handle never wakes it, with one
   exception: a Funnel webhook (``rine.v1.webhook``) is a legitimate inbound event that
   the relay self-sends as the agent (``from == to``), so it IS dispatched.
2. **allowlist** — when ``RINE_ALLOWED_HANDLES`` is set, a disallowed sender is
   *quarantine-logged* (WARNING) and dropped — never silently swallowed (R8).
3. **invalid signature** — a tampered/forged sender, or a stale/rotated key, raises
   ``CryptoError`` inside the SDK, surfaced as a ``decrypt_error`` reading "Sender
   signature verification failed" (``verification_status`` stays ``unverifiable``). We
   special-case that text and **log it at WARNING** (the security-relevant audit trail),
   then **drain it (journal + ack)** so it never replays. It is **never** dispatched.
4. **decrypt_error** (any other) — genuinely unreadable (MLS / unsupported version):
   same drain (journal + ack), logged at INFO. Never dispatched: ciphertext / unreadable
   payload must not reach the agent (R6). Draining (not "leave new") is deliberate — a
   real inbox with an unverifiable backlog would otherwise re-quarantine every poll.
5. **unverifiable** — dispatched with an ``[unverified sender]`` note, unless
   ``RINE_REQUIRE_VERIFIED=1`` blocks it (R8).
6. **already consumed** — a redelivered, already-journaled id is **re-acked** (so it
   finally leaves the inbox) and **skipped** for dispatch (exactly-once across restart).
7. **in-flight** — an id whose turn is already running (registered a post-delivery
   callback from a prior poll) is **silently skipped** — no re-dispatch, no re-ack —
   because the poll interval can be shorter than a turn, so the server still shows it
   ``new`` until the turn finishes and acks it.
8. otherwise — build the source + ``MessageEvent(internal=True, auto_skill="rine:rine")``,
   register a **post-delivery callback** that journals (FIRST, durable) then acks
   (best-effort) **only after the agent turn completes**, mark the id in-flight, then
   ``await handle_message`` to *schedule* the turn.

**Why a post-delivery callback?** ``BasePlatformAdapter.handle_message`` returns as soon
as the turn is *scheduled* (it spawns a background task — base.py:3839,3678), NOT after
the turn runs. Journaling/acking right after the await would record-and-ack before the
wake is durable → a crash mid-turn would lose the message (at-most-once). Hermes pops +
fires our callback only after ``_message_handler`` completes (base.py ~4510), so we defer
journal+ack to it: success → journaled+acked AFTER the turn (exactly-once); crash before
the callback → message stays ``new`` + un-journaled → re-dispatched next poll
(at-least-once, no loss); redelivery of a journaled id → re-acked, not re-dispatched.

**Known v0.1 edge case:** two inbound from the same sender in flight share a session key;
``register_post_delivery_callback`` *chains* callbacks for the same key and the pop is
one-shot, so both fire after whichever turn completes next. Acceptable for v0.1 (distinct
A2A conversations dominate); documented, not over-engineered.

A dispatch (scheduling) failure removes the in-flight mark and leaves the id **neither
journaled nor acked**, so it stays eligible. The ciphertext is never read — only
``render_message_body`` (plaintext / decrypt_error).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from hermes_rine._format import render_message_body

if TYPE_CHECKING:
    from rine import DecryptedMessage

    from hermes_rine.adapter import RineAdapter

logger = logging.getLogger("hermes_rine.poll")

# The SDK raises ``CryptoError("Sender signature verification failed")`` on a signature
# mismatch (rine-sdk/src/rine/_crypto/message.py); inbox()/read() surface that text as
# ``decrypt_error`` (verification_status stays "unverifiable"). Match it case-insensitively
# so a signature failure is logged at WARNING (security-relevant) vs a benign unreadable
# (MLS / unsupported) at INFO — both are drained, neither is dispatched.
_SIG_FAILURE_MARKER = "signature verification failed"

# The Funnel relay self-sends this type (from == to); it is the one legitimate self-send.
# Compared as a literal so this wake exception never depends on the installed rine SDK
# version exporting a named constant for it.
_FUNNEL_WEBHOOK_TYPE = "rine.v1.webhook"


def _sender_handle(msg: DecryptedMessage) -> str:
    """The sender's handle (falling back to the agent uuid) for keying + self-skip."""
    return msg.sender_handle or (str(msg.from_agent_id) if msg.from_agent_id else "")


def _is_signature_failure(decrypt_error: str) -> bool:
    """True when a ``decrypt_error`` indicates cryptographic sender-signature tampering."""
    return _SIG_FAILURE_MARKER in decrypt_error.lower()


def _is_funnel_webhook(msg: DecryptedMessage) -> bool:
    """True for a Funnel webhook (``rine.v1.webhook``), the one legitimate self-send.

    The Funnel relay encrypts as the agent and self-sends (``from == to``), so a webhook
    would otherwise be swallowed by the self-skip. Every OTHER self-sent type stays
    suppressed (it is the agent's own outbound echo).
    """
    return msg.type == _FUNNEL_WEBHOOK_TYPE


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

    if msg.decrypt_error:
        # 3/4. Never wake on an unreadable message (no trusted plaintext). DRAIN it
        #      (journal + ack) so it cannot wedge the poll loop. A *signature failure*
        #      (forged, or a stale/rotated sender key) is logged at WARNING — the
        #      security-relevant audit trail — while other unreadable causes (MLS /
        #      unsupported version) log at INFO. Ciphertext is NEVER surfaced either way.
        #      NB: an earlier design left signature failures ``new`` for an "operator
        #      sweep", but a real inbox with an unverifiable backlog re-quarantines them
        #      every poll forever (a log/IO storm); draining + a loud WARNING is the
        #      correct, contained policy. The message is never dispatched, so this in no
        #      way weakens the "never wake on an untrusted message" invariant.
        if _is_signature_failure(msg.decrypt_error):
            logger.warning(
                "rine wake: message %s failed sender-signature verification; "
                "not dispatching, draining (journal + ack)",
                message_id,
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
    if message_id in adapter.inflight:
        logger.debug("rine wake: message %s already in-flight; skipping", message_id)
        return

    # 8. Build the event and schedule the turn; journal + ack only AFTER it completes.
    is_group = bool(msg.group_handle or msg.group_id)
    chat_id = (msg.group_handle or str(msg.group_id)) if is_group else sender
    body = render_message_body(msg)
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
        WARNING) without aborting anything; the ack is already best-effort. The
        in-flight mark is always cleared. Never raises out of the callback.
        """
        try:
            _safe_journal(adapter, message_id)
            await _ack(adapter, message_id)
        finally:
            adapter.inflight.discard(message_id)

    session_key = _session_key_for(adapter, source)
    adapter.register_post_delivery_callback(session_key, on_complete)
    adapter.inflight.add(message_id)
    try:
        await adapter.handle_message(event)
    except Exception:  # noqa: BLE001 — scheduling failed: drop the mark, leave re-eligible
        adapter.inflight.discard(message_id)
        adapter.pop_post_delivery_callback(session_key)
        logger.error(
            "rine wake: handle_message failed for %s; leaving re-eligible",
            message_id,
            exc_info=True,
        )


async def drain_poison_pill(adapter: RineAdapter, message_id: str, reason: str) -> None:
    """Handle a message that ``read()`` could not decrypt (an SSE-path poison pill).

    On the poll path the SDK swallows decrypt failures into ``decrypt_error`` so they
    flow through ``_dispatch``; on the SSE path ``read()`` *raises* instead. This mirrors
    ``_dispatch``'s decrypt_error policy for the raising case so one bad message can never
    wedge the stream — **never surfacing ciphertext** (only the fixed error text is used).
    Either way the message is drained (journal + ack) so it can't replay forever; a
    signature failure logs at WARNING (security-relevant), other causes at INFO. It is
    never dispatched.
    """
    if _is_signature_failure(reason):
        logger.warning(
            "rine wake: message %s failed sender-signature verification; "
            "not dispatching, draining (journal + ack)",
            message_id,
        )
    else:
        logger.info(
            "rine wake: message %s unreadable (%s); not dispatching, draining",
            message_id,
            reason,
        )
    _safe_journal(adapter, message_id)
    await _ack(adapter, message_id)


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


__all__ = ["_dispatch", "_poll_loop", "drain_poison_pill"]
