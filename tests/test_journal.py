"""Durability + idempotency tests for the consumed-id journal (D-WAKE exactly-once)."""

from __future__ import annotations

from hermes_rine._journal import ConsumedJournal


def test_mark_consumed_then_is_consumed_returns_true(tmp_path):
    journal = ConsumedJournal(str(tmp_path / "j.sqlite"))
    journal.mark_consumed("me", "msg-1")
    assert journal.is_consumed("me", "msg-1") is True


def test_is_consumed_for_unknown_id_returns_false(tmp_path):
    journal = ConsumedJournal(str(tmp_path / "j.sqlite"))
    assert journal.is_consumed("me", "never-seen") is False


def test_consumed_id_survives_a_fresh_instance_over_the_same_file(tmp_path):
    path = str(tmp_path / "j.sqlite")
    first = ConsumedJournal(path)
    first.mark_consumed("me", "msg-1")
    first.close()

    second = ConsumedJournal(path)
    assert second.is_consumed("me", "msg-1") is True


def test_mark_consumed_is_idempotent(tmp_path):
    journal = ConsumedJournal(str(tmp_path / "j.sqlite"))
    journal.mark_consumed("me", "msg-1")
    journal.mark_consumed("me", "msg-1")  # second mark must not raise
    assert journal.is_consumed("me", "msg-1") is True


def test_consumed_id_is_scoped_per_agent(tmp_path):
    journal = ConsumedJournal(str(tmp_path / "j.sqlite"))
    journal.mark_consumed("agent-a", "msg-1")
    assert journal.is_consumed("agent-a", "msg-1") is True
    assert journal.is_consumed("agent-b", "msg-1") is False
