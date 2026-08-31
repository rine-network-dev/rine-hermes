"""Outbound gateway policy — what leaves the adapter, and what type it carries.

``RineAdapter.send`` is the boundary between Hermes-internal chatter and an encrypted
message to an external agent, so four rules live here.

**A woken turn produces one outbound message.** The gateway marks the turn's final
routed response with ``metadata["notify"] = True`` **and** ``reply_to = <inbound message
id>`` (``gateway/platforms/base.py``); its interim "thinking" narration and Hermes' own
operator notices — the home-channel prompt among them — carry neither. An A2A peer stores
and is billed for every message it receives, and interim narration is this agent's
reasoning *about* that peer, so within a turn an inbound rine message woke, only the
final answer leaves. Either marker on its own is enough, so a change to one of them
upstream cannot silence the channel. ``RINE_RELAY_INTERIM=1`` relays every gateway send
for anyone who wants the narration too.

**Everything the host originates on its own is relayed.** A cron job delivering to
``RINE_HOME_CHANNEL`` and the agent's ``send_message`` tool both reach ``send`` with
neither marker (``cron/scheduler.py``, ``tools/send_message_tool.py``), and neither
belongs to a turn: there is no answer for them to duplicate, and dropping them would
report a delivery that never happened. So the question is not "is this marked final" but
"is this the chatter of a turn already answering this chat" — which is what the adapter's
in-flight turns record (``_turns``).

**A reply mirrors the type it answers.** rine message types are semantic labels
(PROTOCOL.md §4.1). A reply to a ``rine.v1.dm`` is a ``rine.v1.dm``; a reply to a
``rine.v1.task_request`` is a ``rine.v1.task_response``, the one pairing the protocol
defines. Every other type, including a third-party ``com.acme.v1.*``, is mirrored
verbatim. An absent or unrecognised inbound type replies ``rine.v1.task_response``.

**A failed turn says so.** Hermes' own crash text is written for a person in a chat
window and is sent while the failed turn is still in flight, so it stays inside Hermes by
the first rule; the adapter sends a ``rine.v1.error`` in its place and the peer learns the
request failed instead of waiting on silence.

``rine_send`` and ``rine_reply`` never pass through here — they build their own client and
address the peer directly, which is what makes them the way an agent sends a message it
decided to send.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from rine import MessageType

from hermes_rine._client import _format_error

if TYPE_CHECKING:
    from gateway.platforms.base import (  # type: ignore[import-not-found]
        SendResult,
    )

    from hermes_rine.adapter import RineAdapter

logger = logging.getLogger("hermes_rine.outbound")

# PROTOCOL.md §4.1 "Error notification". Also what rine-eve emits on a terminal session
# failure (rine-eve/src/channel-outbound.ts), and one of the types a rine channel treats
# as terminal rather than as something to answer.
ERROR_TYPE = MessageType.ERROR

_FAILURE_TEXT = "The agent hit an internal error handling this message and sent no reply."


def relay_interim_enabled() -> bool:
    """True when ``RINE_RELAY_INTERIM=1`` relays interim narration and notices as well."""
    return os.getenv("RINE_RELAY_INTERIM") == "1"


def is_final_gateway_reply(reply_to: str | None, metadata: dict[str, Any] | None) -> bool:
    """True when a gateway ``send`` carries a turn's final routed answer."""
    return bool((metadata or {}).get("notify")) or reply_to is not None


def should_relay(
    adapter: RineAdapter,
    chat_id: str,
    reply_to: str | None,
    metadata: dict[str, Any] | None,
) -> bool:
    """True when a gateway ``send`` should leave the adapter as a rine message.

    Suppression is scoped to the one thing it exists to prevent — a second outbound
    message on a turn an inbound rine message already woke. A send to a chat with no
    turn in flight is host-initiated (a cron delivery, the ``send_message`` tool, a
    home-channel notice) and goes out.
    """
    if relay_interim_enabled() or is_final_gateway_reply(reply_to, metadata):
        return True
    return not adapter.turns.in_chat(chat_id)


def reply_type_for(inbound_type: str | None) -> str:
    """The message type a reply to ``inbound_type`` carries (mirror, one pairing apart)."""
    if inbound_type == MessageType.TASK_REQUEST:
        return MessageType.TASK_RESPONSE
    return inbound_type or MessageType.TASK_RESPONSE


async def route_reply(
    adapter: RineAdapter,
    chat_id: str,
    content: str,
    reply_to: str | None,
    metadata: dict[str, Any] | None,
) -> SendResult:
    """Relay a gateway send to rine, dropping the interim chatter of a woken turn."""
    from gateway.platforms.base import SendResult

    if not should_relay(adapter, chat_id, reply_to, metadata):
        logger.debug("[rine] not relaying interim chatter of a woken turn to %s", chat_id)
        return SendResult(success=True)
    message_type = reply_type_for(adapter.turns.message_type(reply_to))
    return await _deliver(adapter, chat_id, content, reply_to, message_type)


async def notify_failure(adapter: RineAdapter, chat_id: str, reply_to: str | None) -> None:
    """Send a ``rine.v1.error`` after a turn the gateway could not complete.

    Replaces Hermes' human-facing crash text, which the gateway sends unmarked and while
    the failed turn is still in flight, so ``should_relay`` drops it (the turn closes in
    the post-delivery callback, which the gateway fires after that send). The payload is
    one plain sentence: the adapter
    knows only that the turn failed, and inventing detail it does not have would be worse
    than saying so. An error is terminal — a rine channel does not answer one — so this
    cannot start an exchange of its own.
    """
    await _deliver(adapter, chat_id, _FAILURE_TEXT, reply_to, ERROR_TYPE)


async def _deliver(
    adapter: RineAdapter,
    chat_id: str,
    content: str,
    reply_to: str | None,
    message_type: str,
) -> SendResult:
    """Encrypt and send one message, in-thread for a DM and as a broadcast for a group.

    A group answer is a **broadcast**, never a threaded reply. MEASURED (session 22; pinned
    by ``tests/test_group-one-conversation_group0.py`` in the server repo): a group post has
    no single recipient (``to_agent_id IS NULL``), so ``POST /messages/{id}/reply`` refuses
    it outright — 404 *Destination agent not found* for the poster's own org, 404 *Message
    not found* for every other member org. ZERO reply rows are created. So threading a group
    answer would not deliver it privately to one member; the call would raise, the ``except``
    below would turn it into ``SendResult(success=False)``, and the group would hear nothing.

    🔴 ``messages.py``'s ``reply_is_group_carve_out`` branch looks like it files exactly such
    a reply into its own conversation. It is UNREACHABLE for a real group post — it sits
    after the recipient lookup that 404s. Do not read it as the server handling this case and
    drop the ``_is_group`` guard below.

    A Funnel ``rine.v1.webhook`` is relayed as the agent itself (``from == to``), so the
    gateway's reply target for one is THIS agent. rine forbids self-sends (422) and a
    webhook is a one-way event with no reply target, so that send is skipped gracefully
    rather than surfacing a spurious error — the outbound counterpart to the inbound
    self-skip exemption in ``_poll._dispatch``. A turn woken by an x402 payment frame is
    skipped likewise: the pay/fulfill tools thread the signed payload or receipt in place
    themselves, and prose in a payment thread corrupts the handshake.
    """
    from gateway.platforms.base import SendResult

    if adapter.turns.x402_in_chat(chat_id):
        logger.info("[rine] suppressing prose reply into x402 payment thread %s", chat_id)
        return SendResult(success=True)
    if _is_self_addressed(adapter, chat_id):
        logger.info(
            "[rine] skipping self-addressed reply (a webhook self-send has no reply target)"
        )
        return SendResult(success=True)
    thread_on = None if _is_group(chat_id) else reply_to
    logger.debug("[rine] outbound to %s (reply_to=%s, type=%s)", chat_id, thread_on, message_type)
    try:
        if thread_on:
            msg = await adapter.client.reply(
                thread_on, {"text": content}, message_type=message_type
            )
        else:
            msg = await adapter.client.send(chat_id, {"text": content}, message_type=message_type)
    except Exception as err:  # noqa: BLE001 — never raise into the gateway send path
        logger.error("[rine] send to %s failed: %s", chat_id, err)
        return SendResult(success=False, error=_format_error(err))
    return SendResult(success=True, message_id=str(msg.id))


def _is_group(chat_id: str) -> bool:
    """True for a group chat_id — rine group handles are ``#``-prefixed."""
    return chat_id.startswith("#")


def _is_self_addressed(adapter: RineAdapter, chat_id: str) -> bool:
    """True for a DM addressed to this agent's own handle (a webhook's echo target)."""
    if _is_group(chat_id) or not adapter.agent_handle:
        return False
    return chat_id.strip().lower() == adapter.agent_handle.strip().lower()


__all__ = [
    "ERROR_TYPE",
    "is_final_gateway_reply",
    "notify_failure",
    "relay_interim_enabled",
    "reply_type_for",
    "route_reply",
    "should_relay",
]
