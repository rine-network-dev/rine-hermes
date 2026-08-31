"""What an inbox page prints when the sender key for a group row is missing.

The rule, and it is the same on every surface: a render carrying N rows
explains the missing-sender-key cause in full on the FIRST row that has it, and
marks every later one with the SDK's bounded preview. A render carrying ONE row
explains in full, always. The tier is chosen by how many messages the caller
asked for — never by which package renders, and never by matching words in
``decrypt_error``: this surface branches on the SDK's typed
``decrypt_error_code`` alone.

Every assertion here is about text a model reads, through the renderers the
tools call. The two-state machine itself is the SDK's
(``rine.sender_key_words.SenderKeyRefusal``) and is tested there.
"""

from __future__ import annotations

from rine import SENDER_KEY_UNAVAILABLE_CODE, SENDER_KEY_UNAVAILABLE_PREVIEW
from rine.sender_key_words import no_sender_key_state_detail

from hermes_rine._format import render_inbox, render_single_message

SENDER_KEY_ID = "11111111-2222-3333-4444-555555555555"
#: The sentence the SDK composes and hands over on the row; never retyped here.
FULL_SENTENCE = no_sender_key_state_detail(SENDER_KEY_ID)
OTHER_CAUSE = "Unsupported encryption_version 'mls-v1'"


def _no_key_row(make_message):
    """A group row this agent holds no sender key state for."""
    return make_message(
        plaintext=None,
        group_handle="#ops@acme.rine.network",
        decrypt_error=FULL_SENTENCE,
        decrypt_error_code=SENDER_KEY_UNAVAILABLE_CODE,
    )


def _other_cause_row(make_message):
    """A row that failed for a cause this rule does not touch."""
    return make_message(plaintext=None, decrypt_error=OTHER_CAUSE)


def test_inbox_when_eight_rows_lack_the_sender_key_then_it_explains_once_and_marks_the_rest(
    make_message,
):
    """The first row carrying the cause explains it; every later one is marked."""
    out = render_inbox([_no_key_row(make_message) for _ in range(8)], status="new")

    assert out.count(FULL_SENTENCE) == 1
    assert out.count(SENDER_KEY_UNAVAILABLE_PREVIEW) == 7
    # The explanation is on the FIRST row that carries the cause, not a later one.
    assert out.index(FULL_SENTENCE) < out.index(SENDER_KEY_UNAVAILABLE_PREVIEW)


def test_inbox_when_eight_rows_lack_the_sender_key_then_the_page_costs_one_explanation(
    make_message,
):
    """The point of the rule: the page is bounded by one paragraph, not eight."""
    out = render_inbox([_no_key_row(make_message) for _ in range(8)], status="new")
    # The same page under the old behaviour: the paragraph restored to every row.
    paragraph_per_row = out.replace(SENDER_KEY_UNAVAILABLE_PREVIEW, FULL_SENTENCE)

    assert len(out) < len(paragraph_per_row) / 3


def test_inbox_when_rendered_a_second_time_then_it_explains_again(make_message):
    """Per-render state. A module-level flag would make the second page mute."""
    rows = [_no_key_row(make_message) for _ in range(3)]

    first = render_inbox(rows, status="new")
    second = render_inbox(rows, status="new")

    assert first.count(FULL_SENTENCE) == 1
    assert second.count(FULL_SENTENCE) == 1


def test_inbox_when_another_cause_arrives_first_then_it_does_not_spend_the_explanation(
    make_message,
):
    """ "Explained" is pooled per cause, never per render-with-any-failure."""
    rows = [_other_cause_row(make_message), _no_key_row(make_message)]

    out = render_inbox(rows, status="new")

    assert out.count(FULL_SENTENCE) == 1
    assert SENDER_KEY_UNAVAILABLE_PREVIEW not in out


def test_inbox_when_a_row_failed_for_another_reason_then_its_error_is_still_verbatim(
    make_message,
):
    """Only the no-sender-key cause changed; every other refusal reads as before."""
    out = render_inbox([_other_cause_row(make_message) for _ in range(3)], status="new")

    assert out.count(f"[unreadable] {OTHER_CAUSE}") == 3
    assert SENDER_KEY_UNAVAILABLE_PREVIEW not in out


def test_single_message_when_the_sender_key_is_missing_then_the_whole_sentence_is_printed(
    make_message,
):
    """One message asked for → the full four-clause sentence, never the marker."""
    out = render_single_message(_no_key_row(make_message))

    assert FULL_SENTENCE in out
    assert SENDER_KEY_UNAVAILABLE_PREVIEW not in out


def test_single_message_when_read_twice_then_it_explains_both_times(make_message):
    """Two reads are two renders — the second must not be marked by the first."""
    msg = _no_key_row(make_message)

    assert FULL_SENTENCE in render_single_message(msg)
    assert FULL_SENTENCE in render_single_message(msg)


def test_inbox_when_the_key_is_missing_then_the_marker_names_no_key_or_group(make_message):
    """The repeated marker is cause-free — a reader cannot count keys off a page."""
    out = render_inbox([_no_key_row(make_message) for _ in range(4)], status="new")
    marked = out.split(FULL_SENTENCE, 1)[1]

    assert SENDER_KEY_ID not in marked
