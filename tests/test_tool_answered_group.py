"""Group inbound and overlapping turns: each turn is answered by its own tools only.

A group send lands in the group's conversation, so a send to the inbound's group answers it.
Two turns in flight together (another chat's, or a queued second message from the same chat)
each see only what their own tools delivered: a concurrent turn's send neither silences
another turn's answer nor is erased when another turn opens or ends.
"""

from __future__ import annotations

from uuid import uuid4

from hermes_rine.tools import messaging

from .conftest import OTHER_HANDLE
from .test_adapter_send import GROUP, _woken_by
from .tool_answer_rig import final, rig, tool

OTHER_GROUP = "#other@myorg.rine.network"
THIRD_HANDLE = "dave@acme.rine.network"


def _groups(conversation: str) -> dict[str, tuple[str, str]]:
    return {"ops": (GROUP, conversation), "other": (OTHER_GROUP, str(uuid4()))}


async def test_group_final_text_is_not_sent_after_rine_send_to_that_group(monkeypatch) -> None:
    conversation = str(uuid4())
    client, adapter = rig(monkeypatch, _groups(conversation))
    inbound = _woken_by(adapter, GROUP, conversation_id=conversation, group_handle=GROUP)
    await tool(GROUP, messaging._handle_send, {"to": "ops", "body": "hello all"})

    await final(adapter, GROUP, inbound)

    assert client.log == ["send:ops"]


async def test_group_final_text_is_sent_when_rine_send_went_to_another_group(monkeypatch) -> None:
    client, adapter = rig(monkeypatch, _groups(str(uuid4())))
    inbound = _woken_by(adapter, GROUP, conversation_id=str(uuid4()), group_handle=GROUP)
    await tool(GROUP, messaging._handle_send, {"to": "other", "body": "hi"})

    await final(adapter, GROUP, inbound)

    assert client.log == ["send:other", f"send:{GROUP}"]


async def test_a_later_turn_in_the_same_chat_is_answered(monkeypatch) -> None:
    # A send made during an earlier turn answers nothing in a turn that opens afterwards.
    client, adapter = rig(monkeypatch)
    first = _woken_by(adapter, OTHER_HANDLE)
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "earlier"})
    await final(adapter, OTHER_HANDLE, first)
    adapter.turns.end(first)

    second = _woken_by(adapter, OTHER_HANDLE)
    await final(adapter, OTHER_HANDLE, second)

    assert client.log == [f"send:{OTHER_HANDLE}", f"reply:{second}"]


async def test_a_concurrent_turns_send_to_a_group_does_not_silence_the_groups_turn(
    monkeypatch,
) -> None:
    conversation = str(uuid4())
    client, adapter = rig(monkeypatch, _groups(conversation))
    group_turn = _woken_by(adapter, GROUP, conversation_id=conversation, group_handle=GROUP)
    dm_turn = _woken_by(adapter, OTHER_HANDLE)
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": "ops", "body": "fyi"})

    await final(adapter, GROUP, group_turn)
    await final(adapter, OTHER_HANDLE, dm_turn)

    assert client.log == ["send:ops", f"send:{GROUP}", f"reply:{dm_turn}"]


async def test_a_concurrent_turns_send_to_a_peer_does_not_silence_that_peers_turn(
    monkeypatch,
) -> None:
    client, adapter = rig(monkeypatch)
    peer_turn = _woken_by(adapter, OTHER_HANDLE)
    other_turn = _woken_by(adapter, THIRD_HANDLE)
    await tool(THIRD_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "fyi"})

    await final(adapter, OTHER_HANDLE, peer_turn)
    await final(adapter, THIRD_HANDLE, other_turn)

    assert client.log == [f"send:{OTHER_HANDLE}", f"reply:{peer_turn}", f"reply:{other_turn}"]


async def test_another_turn_opening_and_ending_keeps_a_running_turns_answer(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    running = _woken_by(adapter, OTHER_HANDLE)
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "pong"})
    passer = _woken_by(adapter, THIRD_HANDLE)
    await final(adapter, THIRD_HANDLE, passer)
    adapter.turns.end(passer)

    await final(adapter, OTHER_HANDLE, running)

    assert client.log == [f"send:{OTHER_HANDLE}", f"reply:{passer}"]


async def test_a_queued_second_message_from_the_chat_is_answered_after_the_first_turn(
    monkeypatch,
) -> None:
    # The second inbound opens while the first turn still runs; the first turn's send, made
    # after that, answers the first message only.
    client, adapter = rig(monkeypatch)
    first = _woken_by(adapter, OTHER_HANDLE)
    second = _woken_by(adapter, OTHER_HANDLE)
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "pong"})
    await final(adapter, OTHER_HANDLE, first)
    adapter.turns.end(first)

    await final(adapter, OTHER_HANDLE, second)

    assert client.log == [f"send:{OTHER_HANDLE}", f"reply:{second}"]


async def test_a_send_made_while_no_turn_ran_answers_nothing_in_the_next_turn(monkeypatch) -> None:
    client, adapter = rig(monkeypatch)
    await tool(OTHER_HANDLE, messaging._handle_send, {"to": OTHER_HANDLE, "body": "unprompted"})

    inbound = _woken_by(adapter, OTHER_HANDLE)
    await final(adapter, OTHER_HANDLE, inbound)

    assert client.log == [f"send:{OTHER_HANDLE}", f"reply:{inbound}"]
