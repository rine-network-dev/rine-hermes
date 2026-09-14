"""The durable set-aside list and the tick that retries it.

Three things are load-bearing here and each has a guard that a mutation turns red:

- **The bound actually bounds.** ``SETASIDE_MAX_ATTEMPTS`` retries, then the row is
  listed and never re-read again. The rejected design — leave an unreadable message
  ``new`` for an "operator sweep" — was rejected for re-quarantining a backlog every
  poll forever, so a retry with no bound would reintroduce exactly that.
- **The sweep fires on a tick, with nobody asking.** The on-demand verb is the
  convenience; the timer is the feature.
- **An authenticity failure takes the other path**: journaled, recorded already
  exhausted, and never re-read. A signature that does not cover the bytes cannot start
  to verify, and a scheduled re-presentation of an attacker-controlled message is a log
  amplifier whose cadence the attacker chooses.

The store is exercised as itself (real sqlite on ``tmp_path``, never a double — it IS
the thing under test), and the wake paths through the real ``_dispatch`` / ``_sweep_once``
/ ``_sweep_loop`` with the suite's recording client.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools

import pytest

from hermes_rine import _setaside
from hermes_rine._poll import DispatchOutcome, _dispatch
from hermes_rine._setaside import (
    SETASIDE_MAX_ATTEMPTS,
    SETASIDE_MAX_ROWS,
    SWEEP_INTERVAL_ENV,
    SetAsideReason,
    SetAsideStore,
    sweep_interval_seconds,
)
from hermes_rine._sweep import _sweep_loop, _sweep_once

UNREADABLE = "no sender key for this group"


@pytest.fixture
def store(tmp_path):
    """A real ``SetAsideStore`` on its own sqlite file."""
    s = SetAsideStore(str(tmp_path / "setaside.sqlite"))
    yield s
    s.close()


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true or ``timeout`` elapses (then assert)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the sweep")
        await asyncio.sleep(0.02)


# =====================================================================
# The store: bounds, idempotence, eviction
# =====================================================================


def test_record_counts_the_first_sighting_as_an_attempt(store):
    row = store.record("me", "m1", SetAsideReason.UNREADABLE)
    assert row.attempts == 1
    assert row.exhausted is False
    assert store.get("me", "m1") == row


def test_record_is_idempotent_and_keeps_the_original_first_seen(store):
    first = store.record("me", "m1", SetAsideReason.UNREADABLE)
    second = store.record("me", "m1", SetAsideReason.UNREADABLE)
    assert second.attempts == 2
    assert second.first_seen == first.first_seen
    assert len(store.entries("me")) == 1


def test_record_exhausts_the_row_at_the_attempt_bound(store):
    # The bound is the difference between this design and the one that was rejected.
    for attempt in range(1, SETASIDE_MAX_ATTEMPTS + 1):
        row = store.record("me", "m1", SetAsideReason.UNREADABLE)
        assert row.exhausted is (attempt >= SETASIDE_MAX_ATTEMPTS)
    assert store.due("me") == []
    assert len(store.entries("me")) == 1  # listed, just never retried


def test_record_can_exhaust_a_row_immediately(store):
    row = store.record("me", "m1", SetAsideReason.AUTHENTICITY, exhausted=True)
    assert row.attempts == 1
    assert row.exhausted is True
    assert store.due("me") == []


def test_rows_are_scoped_to_the_agent_that_parked_them(store):
    store.record("me", "m1", SetAsideReason.UNREADABLE)
    store.record("sibling", "m2", SetAsideReason.UNREADABLE)
    assert [r.message_id for r in store.entries("me")] == ["m1"]
    assert [r.message_id for r in store.entries("sibling")] == ["m2"]


def test_forget_drops_one_row_and_tolerates_an_absent_one(store):
    store.record("me", "m1", SetAsideReason.UNREADABLE)
    store.forget("me", "m1")
    store.forget("me", "m1")
    assert store.entries("me") == []


def test_due_is_oldest_first_so_a_full_list_is_covered_across_ticks(store):
    for i in range(3):
        store.record("me", f"m{i}", SetAsideReason.UNREADABLE)
    assert [r.message_id for r in store.due("me", limit=2)] == ["m0", "m1"]


def test_the_table_never_exceeds_the_row_bound(store):
    # The real constant, not a patched one: a list that grows without bound is the other
    # half of what the rejected design got wrong.
    for i in range(SETASIDE_MAX_ROWS + 5):
        store.record("me", f"m{i:04d}", SetAsideReason.UNREADABLE)
    assert len(store.entries("me")) == SETASIDE_MAX_ROWS


def test_overflow_evicts_the_oldest_exhausted_row_before_any_live_one(monkeypatch, store):
    # An exhausted row will never be retried, so it is the cheapest thing to lose. The
    # bound itself is proved above against the real 500; this pins the ORDER, which needs
    # a list small enough to state by hand.
    #
    # 🔴 The clock is stubbed because the assertion is about ORDER and ``first_seen`` is
    # the only thing that carries it. Four ``record()`` calls in one test run land inside
    # a single tick of a coarse ``time.time()``, and ``_SQL_EVICT``'s
    # ``ORDER BY exhausted DESC, first_seen ASC`` then tie-breaks by rowid — which
    # happens to be insertion order here, so the test would pass by accident on this box
    # and stop proving the order it names. Monotonic integers make the order the code's.
    ticks = itertools.count(1_000.0)
    monkeypatch.setattr(_setaside, "_now", lambda: next(ticks))
    monkeypatch.setattr(_setaside, "SETASIDE_MAX_ROWS", 3)
    store.record("me", "live-old", SetAsideReason.UNREADABLE)
    store.record("me", "spent", SetAsideReason.AUTHENTICITY, exhausted=True)
    store.record("me", "live-mid", SetAsideReason.UNREADABLE)
    store.record("me", "live-new", SetAsideReason.UNREADABLE)
    assert {r.message_id for r in store.entries("me")} == {"live-old", "live-mid", "live-new"}
    store.record("me", "live-newest", SetAsideReason.UNREADABLE)
    assert {r.message_id for r in store.entries("me")} == {
        "live-mid",
        "live-new",
        "live-newest",
    }


def test_a_reopened_store_still_sees_the_rows(tmp_path):
    # Durable is the point: the retry has to survive the gateway restart that a crashed
    # decrypt often accompanies.
    path = str(tmp_path / "setaside.sqlite")
    first = SetAsideStore(path)
    first.record("me", "m1", SetAsideReason.UNREADABLE)
    first.close()
    second = SetAsideStore(path)
    try:
        assert [r.message_id for r in second.entries("me")] == ["m1"]
    finally:
        second.close()


def test_the_store_shares_the_journal_file_without_touching_its_table(tmp_path):
    # One file, two tables. Widening ``consumed_message`` instead would mean "handled",
    # which is the opposite of what a parked message is.
    from hermes_rine._journal import ConsumedJournal

    path = str(tmp_path / "shared.sqlite")
    journal = ConsumedJournal(path)
    store = SetAsideStore(path)
    try:
        journal.mark_consumed("me", "handled")
        store.record("me", "parked", SetAsideReason.UNREADABLE)
        assert journal.is_consumed("me", "handled") is True
        assert journal.is_consumed("me", "parked") is False
        assert [r.message_id for r in store.entries("me")] == ["parked"]
    finally:
        store.close()
        journal.close()


# =====================================================================
# The tick interval
# =====================================================================


def test_sweep_interval_defaults_to_five_minutes(monkeypatch):
    monkeypatch.delenv(SWEEP_INTERVAL_ENV, raising=False)
    assert sweep_interval_seconds() == 300


def test_sweep_interval_honours_the_env_var(monkeypatch):
    monkeypatch.setenv(SWEEP_INTERVAL_ENV, "45")
    assert sweep_interval_seconds() == 45


@pytest.mark.parametrize("bad", ["0", "-5", "soon", ""])
def test_sweep_interval_refuses_a_value_that_would_disable_the_retry(monkeypatch, caplog, bad):
    # A silently disabled retry reads exactly like the behaviour this replaces, so an
    # unusable value falls back loudly rather than turning the feature off.
    monkeypatch.setenv(SWEEP_INTERVAL_ENV, bad)
    with caplog.at_level("WARNING", logger="hermes_rine.setaside"):
        assert sweep_interval_seconds() == 300
    assert bool(caplog.records) is (bad != "")


# =====================================================================
# The wake channel: parking on dispatch, retrying on the tick
# =====================================================================


async def test_an_unreadable_message_is_parked_not_journaled(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    assert adapter.journal.is_consumed("me", str(msg.id)) is False
    row = adapter.setaside.get("me", str(msg.id))
    assert row is not None
    assert row.reason is SetAsideReason.UNREADABLE


async def test_the_sweep_rereads_with_peek_so_the_retry_cannot_consume_the_message(
    make_adapter, make_message
):
    # 🔴 Mutation guard for ``peek=True``. Without the peek header the server marks the
    # row delivered on every tick, so the retry would consume the message it exists to
    # recover — a silent inversion of the whole feature, invisible to every other
    # assertion here.
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    adapter.client.reads[str(msg.id)] = msg
    await _sweep_once(adapter)
    assert adapter.client.read_calls == [(str(msg.id), True)]


async def test_the_sweep_dispatches_a_message_that_became_readable(make_adapter, make_message):
    # The payoff: a group message sealed under a sender key that had not arrived yet is
    # delivered late but intact, instead of being lost at its first poll.
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    assert adapter.events == []
    adapter.client.reads[str(msg.id)] = make_message(
        message_id=msg.id, plaintext='{"text":"late but intact"}'
    )
    await _sweep_once(adapter)
    assert len(adapter.events) == 1
    assert adapter.events[0].text == "late but intact"
    assert adapter.setaside.get("me", str(msg.id)) is None  # off the list once delivered
    # 🔴 The sweep itself acks NOTHING on the way through — the one ack on file is the
    # park-time drain, and the healed row's second one rides ``_dispatch``'s ordinary
    # post-delivery callback (recorded, not yet fired here, because the turn has not
    # completed). Pinned because both mutations are invisible to every assertion above:
    # a sweep that acked the row itself would ack BEFORE the turn ran — turning a crash
    # mid-turn into a message nothing will ever offer again, which is the loss the list
    # exists to prevent — and one that skipped the callback registration would leave the
    # late delivery un-drained forever.
    assert adapter.client.acked == [str(msg.id)]


async def test_the_sweep_bumps_a_still_unreadable_message_once_per_pass(
    make_adapter, make_message
):
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    adapter.client.reads[str(msg.id)] = msg
    await _sweep_once(adapter)
    row = adapter.setaside.get("me", str(msg.id))
    assert row is not None
    assert row.attempts == 2
    assert adapter.events == []


async def test_the_attempt_bound_stops_the_sweep_rereading_forever(make_adapter, make_message):
    # 🔴 The bound actually bounding, end to end. Raise ``SETASIDE_MAX_ATTEMPTS`` past the
    # loop count or drop the exhaustion write and the final assertion goes red.
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    adapter.client.reads[str(msg.id)] = msg
    await _dispatch(adapter, msg)  # attempt 1
    for _ in range(SETASIDE_MAX_ATTEMPTS - 1):
        await _sweep_once(adapter)  # attempts 2..MAX
    row = adapter.setaside.get("me", str(msg.id))
    assert row is not None
    assert row.attempts == SETASIDE_MAX_ATTEMPTS
    assert row.exhausted is True

    reads_so_far = len(adapter.client.read_calls)
    await _sweep_once(adapter)
    assert len(adapter.client.read_calls) == reads_so_far  # nothing re-read
    assert adapter.setaside.get("me", str(msg.id)).attempts == SETASIDE_MAX_ATTEMPTS


async def test_an_authenticity_failure_is_journaled_and_never_retried(make_adapter, make_message):
    # 🔴 The path the SPEC assigns to a refused sender, told apart by the SDK's own
    # ``is_authenticity_failure`` and not by the error's prose. Drop ``exhausted=True``
    # and the sweep starts re-reading an attacker-controlled message on a timer.
    adapter = make_adapter()
    msg = make_message(
        plaintext=None,
        decrypt_error="signature verification failed",
        verification_status="invalid",
        verified=False,
    )
    adapter.client.reads[str(msg.id)] = msg
    await _dispatch(adapter, msg)
    row = adapter.setaside.get("me", str(msg.id))
    assert row is not None
    assert row.reason is SetAsideReason.AUTHENTICITY
    assert row.exhausted is True
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # a refusal IS handling

    await _sweep_once(adapter)
    assert adapter.client.read_calls == []
    assert adapter.events == []


async def test_a_still_sealed_row_is_not_reacked_or_redispatched_on_every_tick(
    make_adapter, make_message, caplog
):
    # 🔴 Mutation guard for the sweep counting the attempt ITSELF instead of handing a
    # still-sealed message back to ``_dispatch``. ``_dispatch``'s unreadable arm acks and
    # logs at INFO, so re-entering it would re-ack an already-acked row and re-announce it
    # on EVERY tick for the life of the install — the log/IO storm the never-ack design
    # was rejected for, rebuilt inside its replacement. Every counter assertion in this
    # file passes either way: the ack list and the log are the only witnesses.
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    assert adapter.client.acked == [str(msg.id)]
    adapter.client.reads[str(msg.id)] = msg

    with caplog.at_level("INFO", logger="hermes_rine.poll"):
        await _sweep_once(adapter)
        await _sweep_once(adapter)

    assert adapter.client.acked == [str(msg.id)], "the retry re-acked an already-acked row"
    assert adapter.events == []
    assert [r.message for r in caplog.records if "set aside for retry" in r.message] == []


async def test_a_row_that_opens_but_is_declined_by_policy_stays_listed_and_bounded(
    make_adapter, make_message
):
    # 🔴 Two mutations at once. The sweep used to ``forget`` a row the instant it opened
    # and only THEN dispatch it — so a message ``_dispatch`` declines on an early return
    # (strict-mode ``unverifiable`` here, an in-flight turn in production) was dropped
    # from the list while never being handled: silently unrecoverable, the exact loss the
    # list exists to prevent. And because nothing on that path records anything, a refusal
    # that re-decides identically forever has to spend an attempt, or it occupies the
    # oldest-first batch permanently and starves every newer row behind it.
    adapter = make_adapter(require_verified=True)
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    assert adapter.setaside.get("me", str(msg.id)).attempts == 1
    adapter.client.reads[str(msg.id)] = make_message(
        message_id=msg.id,
        plaintext='{"text":"opened but unverifiable"}',
        verification_status="unverifiable",
        verified=False,
    )

    await _sweep_once(adapter)

    row = adapter.setaside.get("me", str(msg.id))
    assert row is not None, "a declined message must not be dropped from the list"
    assert row.attempts == 2, "a policy refusal must spend an attempt or it retries forever"
    assert adapter.events == []


async def test_a_row_that_comes_back_forged_stops_being_retried(make_adapter, make_message):
    # A row parked as merely unreadable that re-reads as a REFUSED SENDER must inherit the
    # authenticity verdict, not keep its old one: the sweep re-classifies on every read.
    # Otherwise an attacker-controlled message whose first sighting looked transient gets
    # re-presented on a timer whose cadence the attacker picks.
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    adapter.client.reads[str(msg.id)] = make_message(
        message_id=msg.id,
        plaintext=None,
        decrypt_error="signature verification failed",
        verification_status="invalid",
        verified=False,
    )

    await _sweep_once(adapter)
    row = adapter.setaside.get("me", str(msg.id))
    assert row.reason is SetAsideReason.AUTHENTICITY
    assert row.exhausted is True

    reads_so_far = len(adapter.client.read_calls)
    await _sweep_once(adapter)
    assert len(adapter.client.read_calls) == reads_so_far


async def test_dispatch_reports_which_of_the_three_things_it_did(make_adapter, make_message):
    # The sweep's whole accounting keys off this return value, and each member drives a
    # different write: HANDLED forgets the row, PARKED leaves the attempt ``_dispatch``
    # already spent, DEFERRED spends one. Collapsing any two of them silently breaks the
    # bound in one direction or the other.
    adapter = make_adapter(allowed_handles={"someone-else"})
    declined = await _dispatch(adapter, make_message(plaintext='{"text":"hi"}'))
    assert declined is DispatchOutcome.DEFERRED

    adapter = make_adapter()
    parked = await _dispatch(adapter, make_message(plaintext=None, decrypt_error=UNREADABLE))
    assert parked is DispatchOutcome.PARKED

    handled = await _dispatch(adapter, make_message(plaintext='{"text":"hi"}'))
    assert handled is DispatchOutcome.HANDLED


async def test_the_sweep_forgets_a_message_the_server_no_longer_has(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    await _sweep_once(adapter)  # the recording client 404s an id it has no answer for
    assert adapter.setaside.get("me", str(msg.id)) is None


async def test_one_failed_reread_does_not_end_the_sweep(make_adapter, make_message):
    # A transient network error on one row must not strand every row behind it.
    adapter = make_adapter()
    first = make_message(plaintext=None, decrypt_error=UNREADABLE)
    second = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, first)
    await _dispatch(adapter, second)
    adapter.client.reads[str(first.id)] = RuntimeError("connection reset")
    adapter.client.reads[str(second.id)] = make_message(
        message_id=second.id, plaintext='{"text":"through"}'
    )
    await _sweep_once(adapter)
    assert len(adapter.events) == 1
    assert adapter.setaside.get("me", str(first.id)) is not None  # kept for the next tick


async def test_the_sweep_fires_on_a_tick_with_nobody_asking(
    monkeypatch, make_adapter, make_message
):
    # 🔴 The retry rides a TIMER, not only the on-demand verb.
    # Nothing in this test calls the sweep — it starts the loop and waits.
    monkeypatch.setenv(SWEEP_INTERVAL_ENV, "1")
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    adapter.client.reads[str(msg.id)] = make_message(
        message_id=msg.id, plaintext='{"text":"on the tick"}'
    )
    task = asyncio.create_task(_sweep_loop(adapter))
    try:
        await _wait_for(lambda: bool(adapter.events))
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert adapter.events[0].text == "on the tick"


async def test_the_sweep_sleeps_before_its_first_pass(monkeypatch, make_adapter, make_message):
    # It must not race the listener's own first pass, which is about to park these rows.
    monkeypatch.setenv(SWEEP_INTERVAL_ENV, "300")
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    await _dispatch(adapter, msg)
    task = asyncio.create_task(_sweep_loop(adapter))
    try:
        await asyncio.sleep(0.05)
        assert adapter.client.read_calls == []
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_store_failure_still_drains_the_message(make_adapter, make_message, caplog):
    # The retry is an improvement on forgetting, never a new way to wedge the poll loop:
    # if the store cannot be written the message is still acked and still not dispatched.
    adapter = make_adapter()
    adapter.setaside.close()
    adapter.setaside._path = "/nonexistent-dir/setaside.sqlite"  # noqa: SLF001
    msg = make_message(plaintext=None, decrypt_error=UNREADABLE)
    with caplog.at_level("WARNING", logger="hermes_rine.poll"):
        await _dispatch(adapter, msg)
    assert adapter.client.acked == [str(msg.id)]
    assert adapter.events == []
    assert any("set-aside write failed" in r.message for r in caplog.records)
