"""Outbound ``RineAdapter.send`` behaviour — what is relayed, and what type it carries.

Four rules are exercised here, all through the real ``RineAdapter.send`` (constructed via
``__new__`` to bypass the ``PlatformConfig`` wiring, which differs between the bare-host
stub and the Docker rig) with a recording fake client, so the assertions are identical
with or without Hermes installed:

- within a turn an inbound rine message woke, only the final routed answer leaves the
  adapter — the gateway marks it with ``metadata["notify"]`` and/or ``reply_to``, and the
  turn's interim narration carries neither;
- a send to a chat with no turn in flight is host-initiated (a cron delivery, the
  ``send_message`` tool, a home-channel notice) and is relayed — it has no answer to
  duplicate, and reporting success for a message that never left would be a lie;
- a routed reply mirrors the type of the message it answers;
- a turn the gateway could not complete produces a ``rine.v1.error`` instead of silence.

The host call shapes the relay rules are stated against are read out of Hermes 0.16.0:
``cron/scheduler.py`` sends ``adapter.send(chat_id, text, metadata=None)`` and
``tools/send_message_tool.py`` sends ``adapter.send(chat_id=…, content=…, metadata=None)``
— neither carries ``notify`` or ``reply_to``.

A Funnel ``rine.v1.webhook`` is relayed as the agent itself (``from == to``), so the
gateway's reply target for one is THIS agent; rine forbids self-sends (422), and the
adapter must skip that gracefully rather than surface a spurious error.
"""

from __future__ import annotations

import types
from typing import Any
from uuid import uuid4

import pytest

from hermes_rine._turns import InboundTurn, InboundTurns

from .conftest import OTHER_HANDLE, SELF_HANDLE

FINAL = {"notify": True}
GROUP = "#ops@myorg.rine.network"


class _SentMsg:
    def __init__(self) -> None:
        self.id = uuid4()


class FakeSendClient:
    """Records ``send``/``reply`` calls so a test can assert what hit the wire (if any)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.replied: list[tuple[str, Any]] = []
        self.reply_types: list[str | None] = []
        self.send_types: list[str | None] = []

    async def send(
        self, target: str, payload: Any, *, message_type: str | None = None
    ) -> _SentMsg:
        self.sent.append((target, payload))
        self.send_types.append(message_type)
        return _SentMsg()

    async def reply(
        self, reply_to: str, payload: Any, *, message_type: str | None = None
    ) -> _SentMsg:
        self.replied.append((reply_to, payload))
        self.reply_types.append(message_type)
        return _SentMsg()


def _adapter(agent_handle: str, client: FakeSendClient):
    """A bare ``RineAdapter`` with only the attrs the outbound path reads."""
    from hermes_rine.adapter import RineAdapter

    adapter = RineAdapter.__new__(RineAdapter)
    adapter._agent_handle = agent_handle
    adapter._client = client
    adapter._turns = InboundTurns()
    return adapter


def _woken_by(adapter, chat_id: str, message_type: str = "rine.v1.dm", **kwargs) -> str:
    """Open an in-flight turn for ``chat_id``, as ``_poll._dispatch`` does on a wake."""
    message_id = str(uuid4())
    adapter.turns.begin(
        message_id, InboundTurn(chat_id=chat_id, message_type=message_type, **kwargs)
    )
    return message_id


def _event(chat_id: str, message_id: str | None) -> Any:
    """A minimal stand-in for the Hermes ``MessageEvent`` the lifecycle hook receives."""
    return types.SimpleNamespace(
        source=types.SimpleNamespace(chat_id=chat_id), message_id=message_id
    )


# --- within a woken turn, only the final routed answer is relayed ---------------------


async def test_interim_narration_of_a_woken_turn_is_not_relayed() -> None:
    # Interim assistant messages reach the adapter through the same send() as the real
    # answer, but with neither final-reply marker. They are the agent's reasoning about
    # the peer, and a peer stores and pays for every message it receives.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    _woken_by(adapter, OTHER_HANDLE)

    res = await adapter.send(
        OTHER_HANDLE, "I'll check the inbox to get the message ID so I can reply in-thread."
    )

    assert res.success is True  # the gateway is told the send succeeded, not that it failed
    assert client.sent == []
    assert client.replied == []


async def test_woken_turn_produces_exactly_one_outbound_message() -> None:
    # The release's headline promise, end to end: one inbound message wakes a turn that
    # narrates twice and then answers, and exactly one message reaches the peer.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = _woken_by(adapter, OTHER_HANDLE, "rine.v1.task_request")

    await adapter.send(OTHER_HANDLE, "Let me look that up.")
    await adapter.send(OTHER_HANDLE, "Still working on it.")
    await adapter.send(OTHER_HANDLE, "the answer", reply_to=inbound, metadata=FINAL)

    assert client.sent == []
    assert client.replied == [(inbound, {"text": "the answer"})]
    assert client.reply_types == ["rine.v1.task_response"]


# --- a send the host originates on its own is relayed ---------------------------------


async def test_cron_delivery_reaches_the_peer() -> None:
    # cron/scheduler.py calls adapter.send(chat_id, text, metadata=None) — positional,
    # no reply_to, and metadata is None whenever there is no thread id, which for rine is
    # always. Dropping it would leave the scheduler logging "delivered via live adapter"
    # for a message that never left the box, and never falling back to standalone.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(OTHER_HANDLE, "nightly report", metadata=None)

    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "nightly report"})]


async def test_send_message_tool_reaches_the_peer() -> None:
    # tools/send_message_tool.py routes a plugin platform through
    # adapter.send(chat_id=…, content=…, metadata=None) and reports {"success": True} back
    # to the model on a truthy SendResult. A drop tells the agent it sent a message that
    # was never encrypted or POSTed.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(chat_id=OTHER_HANDLE, content="ping", metadata=None)

    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "ping"})]


async def test_home_channel_notice_reaches_the_peer_when_no_turn_is_running() -> None:
    # Hermes' own operator notices are written for a person, but they arrive on a chat
    # with no turn in flight and are indistinguishable at this boundary from a cron
    # delivery. Relaying them is the cost of never silently discarding real traffic.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(
        OTHER_HANDLE,
        "📬 No home channel is set for Rine. Type /sethome to make this chat your home.",
    )

    assert res.success is True
    assert len(client.sent) == 1


async def test_a_send_to_another_peer_during_a_woken_turn_is_relayed() -> None:
    # The suppression is scoped to the chat the turn is answering: an agent woken by one
    # peer can still have a cron job or its send_message tool deliver to another.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    _woken_by(adapter, "peer@other.rine.network")

    res = await adapter.send(OTHER_HANDLE, "unrelated delivery")

    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "unrelated delivery"})]


async def test_a_completed_turn_stops_suppressing_its_chat() -> None:
    # The turn closes in the post-delivery callback; after that the chat is ordinary
    # again, so a later cron delivery to the same peer is not swallowed.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = _woken_by(adapter, OTHER_HANDLE)
    adapter.turns.end(inbound)

    res = await adapter.send(OTHER_HANDLE, "nightly report")

    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "nightly report"})]


async def test_streaming_is_declined_so_a_partial_answer_is_never_previewed() -> None:
    # A rine message cannot be edited once sent. The gateway reads this flag to decide
    # whether to stream a partial answer it would later edit; with editing unavailable it
    # skips streaming (gateway/run.py) and sends the finished answer once, instead of
    # leaving the peer holding a truncated first frame.
    from hermes_rine.adapter import RineAdapter

    assert RineAdapter.SUPPORTS_MESSAGE_EDITING is False


async def test_final_reply_marked_by_reply_to_is_relayed() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = str(uuid4())

    res = await adapter.send(OTHER_HANDLE, "the answer", reply_to=inbound, metadata=FINAL)

    assert res.success is True
    assert client.replied == [(inbound, {"text": "the answer"})]
    assert client.sent == []


async def test_final_reply_marked_only_by_notify_is_relayed() -> None:
    # Either marker alone is sufficient, so a change to one of them upstream cannot
    # silence the channel.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(OTHER_HANDLE, "the answer", metadata=FINAL)

    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "the answer"})]


async def test_relay_interim_env_restores_relaying_everything(monkeypatch) -> None:
    monkeypatch.setenv("RINE_RELAY_INTERIM", "1")
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    await adapter.send(OTHER_HANDLE, "thinking out loud")

    assert client.sent == [(OTHER_HANDLE, {"text": "thinking out loud"})]


# --- the reply mirrors the type it answers -------------------------------------------


@pytest.mark.parametrize(
    ("inbound_type", "expected"),
    [
        ("rine.v1.dm", "rine.v1.dm"),
        ("rine.v1.task_request", "rine.v1.task_response"),
        ("rine.v1.negotiation", "rine.v1.negotiation"),
        ("com.acme.v1.invoice_dispute", "com.acme.v1.invoice_dispute"),
    ],
)
async def test_routed_reply_mirrors_the_inbound_type(inbound_type: str, expected: str) -> None:
    # PROTOCOL.md §4.1 types are semantic labels: a conversation stays a conversation, and
    # the one pairing the protocol defines is request → response.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = _woken_by(adapter, OTHER_HANDLE, inbound_type)

    await adapter.send(OTHER_HANDLE, "answer", reply_to=inbound, metadata=FINAL)

    assert client.reply_types == [expected]


async def test_reply_to_an_unknown_inbound_type_is_a_task_response() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = str(uuid4())  # no turn open — e.g. one that outlived a reconnect

    await adapter.send(OTHER_HANDLE, "answer", reply_to=inbound, metadata=FINAL)

    assert client.reply_types == ["rine.v1.task_response"]


async def test_a_closed_turns_type_does_not_leak_into_a_later_reply() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = _woken_by(adapter, OTHER_HANDLE, "rine.v1.dm")
    adapter.turns.end(inbound)

    await adapter.send(OTHER_HANDLE, "answer", reply_to=inbound, metadata=FINAL)

    assert client.reply_types == ["rine.v1.task_response"]


# --- a failed turn says so ------------------------------------------------------------


async def test_failed_turn_sends_a_rine_error_in_thread() -> None:
    # Hermes' own crash text is written for a person in a chat window and is sent while
    # the failed turn is still in flight, so it stays inside Hermes. Without this the peer
    # gets silence.
    from gateway.platforms.base import ProcessingOutcome

    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = str(uuid4())

    await adapter.on_processing_complete(_event(OTHER_HANDLE, inbound), ProcessingOutcome.FAILURE)

    assert client.reply_types == ["rine.v1.error"]
    [(replied_to, payload)] = client.replied
    assert replied_to == inbound
    assert "error" in payload["text"].lower()


async def test_completed_turn_sends_no_error() -> None:
    from gateway.platforms.base import ProcessingOutcome

    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    await adapter.on_processing_complete(
        _event(OTHER_HANDLE, str(uuid4())), ProcessingOutcome.SUCCESS
    )

    assert client.replied == []
    assert client.sent == []


async def test_failed_turn_in_a_payment_thread_sends_no_error() -> None:
    # Prose in an x402 thread corrupts the handshake; the pay/fulfill tools report their
    # own failures in a form the payer understands.
    from gateway.platforms.base import ProcessingOutcome

    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    _woken_by(adapter, OTHER_HANDLE, "rine.v1.x402_payment", x402=True)

    await adapter.on_processing_complete(
        _event(OTHER_HANDLE, str(uuid4())), ProcessingOutcome.FAILURE
    )

    assert client.replied == []
    assert client.sent == []


# --- guards that predate this change --------------------------------------------------


async def test_self_addressed_reply_is_skipped_gracefully() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(SELF_HANDLE, "ack", reply_to=str(uuid4()), metadata=FINAL)

    assert res.success is True  # graceful no-op, not an error surfaced to the agent
    assert client.sent == []  # nothing hit the wire (no 422)
    assert client.replied == []


async def test_self_addressed_reply_skip_is_case_insensitive() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(SELF_HANDLE.upper(), "ack", metadata=FINAL)

    assert res.success is True
    assert client.sent == []


async def test_x402_armed_chat_suppresses_the_final_prose_reply() -> None:
    # An x402 turn arms prose suppression for the chat_id: even the turn's FINAL answer is
    # dropped, because the pay/fulfill tools thread the signed payload/receipt themselves.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    _woken_by(adapter, OTHER_HANDLE, "rine.v1.x402_payment", x402=True)

    res = await adapter.send(
        OTHER_HANDLE, "I have paid your invoice.", reply_to=str(uuid4()), metadata=FINAL
    )

    assert res.success is True  # graceful no-op, not an error to the agent
    assert client.sent == []
    assert client.replied == []


async def test_x402_suppression_released_allows_a_later_reply() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = _woken_by(adapter, OTHER_HANDLE, "rine.v1.x402_payment", x402=True)
    adapter.turns.end(inbound)

    res = await adapter.send(OTHER_HANDLE, "hi peer", metadata=FINAL)

    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "hi peer"})]


async def test_group_answer_broadcasts_instead_of_replying_to_one_member() -> None:
    # MEASURED (session 22): a group post has no single recipient (to_agent_id IS NULL), so
    # POST /messages/{id}/reply refuses it outright — 404 "Destination agent not found" for
    # the poster's own org, 404 "Message not found" for every other member org, and ZERO
    # reply rows are created. Threading a group answer would not deliver it privately to one
    # member; it would fail the send and the group would hear nothing. A group answer is a
    # broadcast. (messages.py's reply_is_group_carve_out branch looks like it files such a
    # reply; it is unreachable, sitting after the recipient lookup that 404s.)
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound = _woken_by(adapter, GROUP, "rine.v1.dm")

    res = await adapter.send(GROUP, "team update", reply_to=inbound, metadata=FINAL)

    assert res.success is True
    assert client.replied == []
    assert client.sent == [(GROUP, {"text": "team update"})]
    assert client.send_types == ["rine.v1.dm"]  # the type still mirrors the inbound


async def test_group_send_to_own_org_group_is_not_skipped() -> None:
    # A group chat_id (#-prefixed) is never a self-send even if it embeds the org.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(GROUP, "team update", metadata=FINAL)

    assert res.success is True
    assert client.sent == [(GROUP, {"text": "team update"})]


async def test_send_failure_is_reported_not_raised() -> None:
    class _Failing(FakeSendClient):
        async def reply(self, *args: Any, **kwargs: Any) -> _SentMsg:
            raise RuntimeError("network down")

    client = _Failing()
    adapter = _adapter(SELF_HANDLE, client)

    res = await adapter.send(OTHER_HANDLE, "answer", reply_to=str(uuid4()), metadata=FINAL)

    assert res.success is False
    assert "network down" in (res.error or "")
