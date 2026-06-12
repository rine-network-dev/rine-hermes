"""Wake-core behavior tests for ``_poll._dispatch`` (exactly-once + verify/ack policy).

Mocks only the Hermes ``MessageEvent`` seam (conftest stub) and the rine ack
(``RecordingClient.mark_delivered``); the journal is the real sqlite store and the
messages are real SDK ``DecryptedMessage`` models. Every acceptance criterion in
SPEC §5 "Adapter wake" has a test here.

**Asynchronous wake.** Real Hermes ``handle_message`` only *schedules* the turn and
returns; journal + ack happen later, inside a post-delivery callback fired after the
turn completes (base.py). The ``FakeAdapter`` mirrors that: ``_dispatch`` registers the
callback and schedules the turn, and a test simulates turn completion by calling
``adapter.fire_post_delivery()``. So these tests assert journal/ack happen ONLY after the
turn (not when ``handle_message`` returns), and that a turn that never completes leaves
the message un-journaled + un-acked (the real loss window, now covered).
"""

from __future__ import annotations

from hermes_rine._poll import _dispatch

# The exact SDK text inbox()/read() surface for a tampered 1:1 message
# (rine-sdk/src/rine/_crypto/message.py raises CryptoError with this message).
SIG_FAILURE = "Sender signature verification failed"


def _field(source: object, key: str) -> object:
    """Read a source field whether it's a real ``SessionSource`` (rig) or dict (off-Hermes)."""
    return source[key] if isinstance(source, dict) else getattr(source, key)


async def test_single_inbound_wakes_once_and_acks_after(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message()
    await _dispatch(adapter, msg)

    # Turn scheduled + a callback registered, but NOT yet journaled/acked.
    assert len(adapter.events) == 1
    event = adapter.events[0]
    assert event.internal is True
    assert event.auto_skill == "rine:rine"
    assert _field(event.source, "chat_id") == "alice"
    assert adapter.registered_callbacks  # a post-delivery callback is pending
    assert adapter.journal.is_consumed("me", str(msg.id)) is False  # not yet
    assert adapter.client.acked == []  # not yet

    # Turn completes → journal (first) then ack, and the in-flight mark clears.
    await adapter.fire_post_delivery()
    assert adapter.journal.is_consumed("me", str(msg.id)) is True
    assert adapter.client.acked == [str(msg.id)]
    assert str(msg.id) not in adapter.inflight


async def test_no_journal_or_ack_until_turn_completes(make_adapter, make_message):
    # The fire-and-forget loss window: a message whose turn never completes (crash
    # mid-turn) must NOT be journaled or acked, so it is re-dispatched next poll.
    adapter = make_adapter()
    msg = make_message()
    await _dispatch(adapter, msg)
    # Deliberately do NOT fire the callback (simulate a crash before the turn finishes).
    assert adapter.journal.is_consumed("me", str(msg.id)) is False
    assert adapter.client.acked == []
    assert str(msg.id) in adapter.inflight  # still in flight


async def test_wake_body_never_contains_ciphertext(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(plaintext='{"text":"the plaintext"}')
    await _dispatch(adapter, msg)
    assert adapter.events[0].text == "the plaintext"
    assert "OPAQUE-CIPHERTEXT" not in adapter.events[0].text


async def test_self_message_is_skipped(make_adapter, make_message):
    adapter = make_adapter(agent_handle="me")
    await _dispatch(adapter, make_message(sender_handle="me"))
    assert adapter.events == []
    assert adapter.client.acked == []


async def test_in_flight_inbound_not_re_dispatched_before_turn_completes(
    make_adapter, make_message
):
    # A poll interval shorter than a turn re-surfaces the same still-``new`` message.
    # While its turn is in flight (callback unfired) it must NOT re-dispatch or re-ack.
    adapter = make_adapter()
    msg = make_message()
    await _dispatch(adapter, msg)
    await _dispatch(adapter, msg)  # redelivery while the first turn is still running
    assert len(adapter.events) == 1  # no double-wake
    assert adapter.client.acked == []  # not re-acked mid-flight

    # Once the turn completes, the id is journaled + acked exactly once.
    await adapter.fire_post_delivery()
    assert adapter.client.acked == [str(msg.id)]


async def test_duplicate_inbound_after_turn_dispatches_once(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message()
    await _dispatch(adapter, msg)
    await adapter.fire_post_delivery()  # first turn completes → journaled + acked
    await _dispatch(adapter, msg)  # redelivery (e.g. across a reconnect)
    assert len(adapter.events) == 1  # exactly-once
    # Redelivery of a journaled id is re-acked (so it finally leaves the inbox).
    assert adapter.client.acked == [str(msg.id), str(msg.id)]


async def test_decrypt_error_never_dispatched_but_journaled_acked(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(plaintext=None, decrypt_error="MLS unsupported")
    await _dispatch(adapter, msg)
    assert adapter.events == []  # ciphertext never surfaced
    # Genuinely unreadable: journaled + acked immediately (no turn to wait on).
    assert adapter.journal.is_consumed("me", str(msg.id)) is True
    assert adapter.client.acked == [str(msg.id)]


async def test_signature_failure_never_dispatched_but_drained(make_adapter, make_message):
    # A REAL signature-failure message: the SDK surfaces decrypt_error with the
    # signature-failure text, plaintext None, verification_status "unverifiable" (it can
    # NEVER be "invalid"). Security invariant: it is NEVER dispatched (the agent must not
    # wake on an untrusted/unreadable message). Operationally it is DRAINED (journaled +
    # acked) with a WARNING — leaving it ``new`` would re-quarantine an unverifiable
    # backlog every poll (a real storm observed in the live gateway E2E).
    adapter = make_adapter()
    msg = make_message(
        plaintext=None,
        decrypt_error=SIG_FAILURE,
        verification_status="unverifiable",
        verified=False,
    )
    await _dispatch(adapter, msg)
    assert adapter.events == []  # never dispatched (security invariant)
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # drained: journaled
    assert adapter.client.acked == [str(msg.id)]  # drained: acked


async def test_unverifiable_dispatched_with_marker_by_default(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(verification_status="unverifiable", verified=False)
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1
    assert adapter.events[0].text.startswith("[unverified sender]")


async def test_unverifiable_blocked_when_require_verified(make_adapter, make_message):
    adapter = make_adapter(require_verified=True)
    msg = make_message(verification_status="unverifiable", verified=False)
    await _dispatch(adapter, msg)
    assert adapter.events == []
    assert adapter.client.acked == []


async def test_disallowed_sender_quarantined_not_dispatched(make_adapter, make_message):
    adapter = make_adapter(allowed_handles={"bob"})
    await _dispatch(adapter, make_message(sender_handle="alice"))
    assert adapter.events == []  # quarantined (logged), not dispatched
    assert adapter.client.acked == []


async def test_allowed_sender_dispatched(make_adapter, make_message):
    adapter = make_adapter(allowed_handles={"alice"})
    await _dispatch(adapter, make_message(sender_handle="alice"))
    assert len(adapter.events) == 1


async def test_handle_message_failure_leaves_id_re_eligible(make_adapter, make_message):
    adapter = make_adapter(raise_on_handle=True)
    msg = make_message()
    await _dispatch(adapter, msg)
    # Scheduling itself raised → neither journaled nor acked, in-flight mark dropped,
    # callback forgotten → a later poll re-attempts the wake.
    assert adapter.journal.is_consumed("me", str(msg.id)) is False
    assert adapter.client.acked == []
    assert str(msg.id) not in adapter.inflight
    assert not adapter.registered_callbacks


async def test_group_message_builds_group_source(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(group_handle="#research")
    await _dispatch(adapter, msg)
    event = adapter.events[0]
    assert _field(event.source, "chat_type") == "group"
    assert _field(event.source, "chat_id") == "#research"


async def test_failed_ack_then_redelivery_re_acks_not_re_dispatches(make_adapter, make_message):
    # First dispatch: turn completes, journal records, but the ack FAILS.
    adapter = make_adapter(fail_ack=True)
    msg = make_message()
    await _dispatch(adapter, msg)
    await adapter.fire_post_delivery()
    assert len(adapter.events) == 1
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # recorded
    assert adapter.client.acked == []  # ack failed

    # Redelivery of the same id: recognised as consumed → re-acked, NOT re-dispatched.
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1  # still exactly one wake
    assert adapter.client.acked == [str(msg.id)]  # finally acked


async def test_journal_failure_in_callback_does_not_raise(make_adapter, make_message):
    # A journal write failure inside the post-delivery callback degrades to at-least-once
    # (logged) and must NOT raise out of the callback or block the ack.
    adapter = make_adapter()
    msg = make_message()
    await _dispatch(adapter, msg)

    def _boom(*_a, **_k):
        raise RuntimeError("sqlite locked")

    adapter.journal.mark_consumed = _boom  # type: ignore[method-assign]
    await adapter.fire_post_delivery()  # must not raise
    assert adapter.client.acked == [str(msg.id)]  # ack still happened
    assert str(msg.id) not in adapter.inflight  # in-flight mark still cleared
