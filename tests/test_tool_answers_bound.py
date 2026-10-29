"""The ledger of tool answers holds 512 entries; past that the oldest falls out first."""

from __future__ import annotations

from hermes_rine._tool_answers import ToolAnswers

from .tool_answer_rig import bound_session, session_of

CAPACITY = 512
SESSION = session_of("alice@lab")


def _answered(answers: ToolAnswers, message_id: str) -> bool:
    reason = answers.reason(
        SESSION, message_id=message_id, conversation_id="", group_handle=None, peer_handle=None
    )
    return reason is not None


def _note(answers: ToolAnswers, first: int, count: int) -> None:
    with bound_session(SESSION):
        for index in range(first, first + count):
            answers.note_reply(f"m{index}")


def test_tool_answers_when_filled_to_capacity_keep_every_entry() -> None:
    answers = ToolAnswers()
    _note(answers, 0, CAPACITY)

    assert _answered(answers, "m0")
    assert _answered(answers, f"m{CAPACITY - 1}")


def test_tool_answers_when_filled_past_capacity_drop_the_oldest_and_keep_the_newest() -> None:
    answers = ToolAnswers()
    _note(answers, 0, CAPACITY + 3)

    assert [_answered(answers, f"m{index}") for index in range(5)] == [
        False,
        False,
        False,
        True,
        True,
    ]
    assert _answered(answers, f"m{CAPACITY + 2}")


def test_tool_answers_when_another_session_is_forgotten_stay_bounded() -> None:
    answers = ToolAnswers()
    _note(answers, 0, CAPACITY)
    answers.forget(session_of("bob@lab"))
    _note(answers, CAPACITY, 1)

    assert not _answered(answers, "m0")
    assert _answered(answers, "m1")
    assert _answered(answers, f"m{CAPACITY}")
