"""Outbound ``RineAdapter.send`` behavior — the self-send graceful-skip.

A Funnel ``rine.v1.webhook`` is relayed as the agent itself (``from == to``). After the
wake turn the Hermes gateway auto-replies to the "sender", which for a webhook is THIS
agent — and rine forbids self-sends (422 "Cannot send a message to the same agent"). The
adapter must skip that self-addressed reply gracefully (a webhook is a one-way event with
no reply target) instead of surfacing a spurious error. This is the outbound counterpart
to the inbound self-skip exemption in ``_poll._dispatch`` (covered in test_adapter_wake).

These drive the REAL ``RineAdapter.send`` (constructed via ``__new__`` to bypass the
``PlatformConfig`` wiring, which differs between the bare-host stub and the Docker rig) with
a recording fake client, so the assertion is identical with or without Hermes installed.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .conftest import OTHER_HANDLE, SELF_HANDLE


class _SentMsg:
    def __init__(self) -> None:
        self.id = uuid4()


class FakeSendClient:
    """Records ``send``/``reply`` calls so a test can assert what hit the wire (if any)."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.replied: list[tuple[str, Any]] = []
        self.reply_types: list[str | None] = []

    async def send(self, target: str, payload: Any) -> _SentMsg:
        self.sent.append((target, payload))
        return _SentMsg()

    async def reply(
        self, reply_to: str, payload: Any, *, message_type: str | None = None
    ) -> _SentMsg:
        self.replied.append((reply_to, payload))
        self.reply_types.append(message_type)
        return _SentMsg()


def _adapter(agent_handle: str, client: FakeSendClient):
    """A bare ``RineAdapter`` with only the attrs ``send`` reads (no PlatformConfig)."""
    from hermes_rine._replystash import ReplyStash
    from hermes_rine.adapter import RineAdapter

    adapter = RineAdapter.__new__(RineAdapter)
    adapter._agent_handle = agent_handle
    adapter._client = client
    adapter._reply_stash = ReplyStash()
    return adapter


async def test_self_addressed_reply_is_skipped_gracefully() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    res = await adapter.send(SELF_HANDLE, "ack")
    assert res.success is True  # graceful no-op, not an error surfaced to the agent
    assert client.sent == []  # nothing hit the wire (no 422)
    assert client.replied == []


async def test_self_addressed_reply_skip_is_case_insensitive() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    res = await adapter.send(SELF_HANDLE.upper(), "ack")
    assert res.success is True
    assert client.sent == []


async def test_self_addressed_reply_to_via_reply_to_is_skipped() -> None:
    # The gateway sets chat_id to the (self) sender even when reply_to is present; the
    # guard must skip BEFORE either branch so client.reply is never attempted either.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    res = await adapter.send(SELF_HANDLE, "ack", reply_to=str(uuid4()))
    assert res.success is True
    assert client.replied == []
    assert client.sent == []


async def test_dm_with_no_stashed_id_falls_back_to_send() -> None:
    # No inbound id was stashed for this peer (a fresh, agent-initiated DM) → a plain
    # send that opens a new conversation, exactly as before.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    res = await adapter.send(OTHER_HANDLE, "hi peer")
    assert res.success is True
    assert client.sent == [(OTHER_HANDLE, {"text": "hi peer"})]
    assert client.replied == []


async def test_group_send_to_own_org_group_is_not_skipped() -> None:
    # A group chat_id (#-prefixed) is never a self-send even if it embeds the org; it must
    # route normally — the guard is DM-only.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    res = await adapter.send("#ops@myorg.rine.network", "team update")
    assert res.success is True
    assert client.sent == [("#ops@myorg.rine.network", {"text": "team update"})]


async def test_peer_send_uses_reply_when_reply_to_present() -> None:
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    rid = str(uuid4())
    res = await adapter.send(OTHER_HANDLE, "threaded", reply_to=rid)
    assert res.success is True
    assert client.replied == [(rid, {"text": "threaded"})]
    assert client.sent == []


# --- REQ-FORK-01: reply-in-place for a DM via the stashed inbound id -------------------


async def test_dm_reply_in_place_uses_stashed_inbound_id() -> None:
    # _dispatch stashed the inbound id for this DM peer; the agent's reply turn calls
    # send(chat_id) with NO explicit reply_to, and it must reply IN-PLACE (client.reply
    # to the inbound id, preserving the conversation) rather than fork via client.send.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    inbound_id = str(uuid4())
    adapter.record_reply_to(OTHER_HANDLE, inbound_id)

    res = await adapter.send(OTHER_HANDLE, "in-place answer")
    assert res.success is True
    assert client.replied == [(inbound_id, {"text": "in-place answer"})]
    assert client.sent == []  # NOT a forking send
    # The reply must keep the prior send default (TASK_REQUEST → wakes a peer) rather than
    # client.reply's TASK_RESPONSE default, which is loop-guard terminal (REQ-FORK-01).
    from rine import MessageType

    assert client.reply_types == [MessageType.TASK_REQUEST]


async def test_group_reply_ignores_the_stash_and_broadcasts() -> None:
    # A group chat_id (#-prefixed) is a broadcast even if an id was stashed under it.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    group = "#ops@myorg.rine.network"
    adapter.record_reply_to(group, str(uuid4()))

    res = await adapter.send(group, "team update")
    assert res.success is True
    assert client.sent == [(group, {"text": "team update"})]
    assert client.replied == []


async def test_explicit_reply_to_overrides_the_stash() -> None:
    # An explicit reply_to (e.g. a deliberate threaded reply) wins over the DM stash.
    client = FakeSendClient()
    adapter = _adapter(SELF_HANDLE, client)
    adapter.record_reply_to(OTHER_HANDLE, str(uuid4()))
    explicit = str(uuid4())

    res = await adapter.send(OTHER_HANDLE, "threaded", reply_to=explicit)
    assert res.success is True
    assert client.replied == [(explicit, {"text": "threaded"})]
