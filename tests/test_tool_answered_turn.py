"""A turn the model already answered through a rine tool is not answered a second time.

Measured on a live run of the sibling eve channel: the model called ``rine_reply``, then
the channel sent the turn's final text as well, 0.75 s later, so the peer received two
answers to one message. This adapter relays a turn's final text the same way, so the same
rule holds here: when ``rine_reply`` answered the message that woke the turn, or
``rine_send`` / ``rine_send_and_wait`` delivered to the peer who wrote it, the final text
is not sent. A direct send always opens a conversation of its own, so a 1:1 send answers by
its recipient. Group inbound and overlapping turns are in ``test_tool_answered_group.py``.

Every case drives the real tool handlers and the real ``RineAdapter.send`` against one
recording client. Only a call that returned counts: a failed tool call suppresses nothing.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from hermes_rine.tools import messaging

from .conftest import OTHER_HANDLE
from .test_adapter_send import _woken_by
from .tool_answer_rig import final, rig, tool


async def test_final_text_is_not_sent_after_rine_reply_answered_the_message(
    monkeypatch, caplog
) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, "rine.v1.task_request")
    await tool(OTHER_HANDLE, messaging._handle_reply, {"message_id": inbound, "body": "pong"})

    with caplog.at_level(logging.INFO, logger="hermes_rine"):
        await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"reply:{inbound}"]
    assert [
        r.message for r in caplog.records if inbound in r.message and "no auto-reply" in r.message
    ]


async def test_final_text_is_sent_when_the_tool_replied_to_a_different_message(
    monkeypatch,
) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, "rine.v1.task_request")
    other = str(uuid4())
    await tool(OTHER_HANDLE, messaging._handle_reply, {"message_id": other, "body": "pong"})

    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"reply:{other}", f"reply:{inbound}"]


async def test_final_text_is_sent_when_the_rine_reply_call_failed(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, "rine.v1.task_request")
    client.failing = True
    await tool(OTHER_HANDLE, messaging._handle_reply, {"message_id": inbound, "body": "pong"})
    client.failing = False

    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"reply:{inbound}", f"reply:{inbound}"]


async def test_final_text_is_not_sent_after_rine_send_to_the_sender(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, conversation_id=str(uuid4()))
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "pong"})

    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"send:{OTHER_HANDLE}"]


async def test_final_text_is_not_sent_after_rine_send_and_wait_to_the_sender(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, conversation_id=str(uuid4()))
    send_and_wait = messaging._handle_send_and_wait
    await tool(OTHER_HANDLE, send_and_wait, {"to": OTHER_HANDLE, "body": "q", "wait_seconds": 1})

    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"send:{OTHER_HANDLE}"]


async def test_final_text_is_sent_when_rine_send_went_to_another_peer(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, conversation_id=str(uuid4()))
    await tool(
        OTHER_HANDLE, messaging._handle_send, {"to": "carol@acme.rine.network", "body": "psst"}
    )

    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == ["send:carol@acme.rine.network", f"reply:{inbound}"]


async def test_final_text_is_sent_when_the_rine_send_call_failed(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    inbound = _woken_by(adapter, OTHER_HANDLE, conversation_id=str(uuid4()))
    client.failing = True
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "pong"})
    client.failing = False

    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"send:{OTHER_HANDLE}", f"reply:{inbound}"]
