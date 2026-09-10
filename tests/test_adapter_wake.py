"""Wake-core behavior tests for ``_poll._dispatch`` (exactly-once + verify/ack policy).

Mocks only the Hermes ``MessageEvent`` seam (conftest stub) and the rine ack
(``RecordingClient.mark_delivered``); the journal is the real sqlite store and the
messages are real SDK ``DecryptedMessage`` models. Every acceptance criterion for
the adapter's wake path has a test here.

**Asynchronous wake.** Real Hermes ``handle_message`` only *schedules* the turn and
returns; journal + ack happen later, inside a post-delivery callback fired after the
turn completes (base.py). The ``FakeAdapter`` mirrors that: ``_dispatch`` registers the
callback and schedules the turn, and a test simulates turn completion by calling
``adapter.fire_post_delivery()``. So these tests assert journal/ack happen ONLY after the
turn (not when ``handle_message`` returns), and that a turn that never completes leaves
the message un-journaled + un-acked (the real loss window, now covered).
"""

from __future__ import annotations

import pytest

from hermes_rine._outbound import should_relay
from hermes_rine._poll import _dispatch

from .conftest import RecordingClient

# The three first-party x402 payment frames Hermes must never auto-reply to.
X402_TYPES = [
    "rine.v1.x402_payment_required",
    "rine.v1.x402_payment",
    "rine.v1.x402_receipt",
]

# The two authenticity refusals the SDK reports on a row it would not open. The text is
# incidental — the verdict lives in ``verification_status``
# (rine-sdk/src/rine/_base_client.py ``_apply_decrypt_failure``) — and these strings are
# here only so the fixtures look like the rows the SDK actually hands over.
SIG_FAILURE = "Sender signature verification failed"
SENDER_MISMATCH = "Envelope sender does not match the message sender"


def _field(source: object, key: str) -> object:
    """Read a source field whether it's a real ``SessionSource`` (rig) or dict (off-Hermes)."""
    return source[key] if isinstance(source, dict) else getattr(source, key)


def _real_adapter(tmp_path, client):
    """A real ``RineAdapter`` over the real base class, wired for a dispatch-only test.

    Built with ``__new__`` (as the outbound tests do) so no ``PlatformConfig`` wiring is
    needed, and with ``_message_handler = None`` so the base's ``handle_message`` returns
    without a gateway behind it. What this buys over ``FakeAdapter``: the post-delivery
    callback store and its generation gate are the *base class's*, so a callback the
    gateway would decline to hand back is a failing test rather than a green one.
    """
    from gateway.config import Platform  # type: ignore[import-not-found]

    from hermes_rine._journal import ConsumedJournal
    from hermes_rine._turns import InboundTurns
    from hermes_rine.adapter import RineAdapter

    adapter = RineAdapter.__new__(RineAdapter)
    # Set for the same reason every other attribute here is: ``__new__`` skips
    # ``__init__``, which is where the real adapter gets it (``super().__init__(config,
    # Platform("rine"))``). The base class reads ``self.platform`` when it builds a
    # ``SessionSource``, so a post-delivery callback that reaches that path dies with a
    # bare AttributeError — a gap in this harness's hand-wired attribute list, not in the
    # adapter. Keep it in step with ``RineAdapter.__init__``.
    adapter.platform = Platform("rine")
    adapter._agent_handle = "me"
    adapter._client = client
    adapter._journal = ConsumedJournal(str(tmp_path / "journal.sqlite"))
    adapter._turns = InboundTurns()
    adapter._require_verified = False
    adapter._allowed_handles = None
    # Gateway control off, which is also what ``__init__`` reads from an unset
    # environment: the two attributes are hand-wired here for the same ``__new__``
    # reason as everything above.
    adapter._gateway_control_on = False
    adapter._gateway_control_handles = frozenset()
    adapter._running = True
    adapter._message_handler = None
    adapter._post_delivery_callbacks = {}
    adapter.config = type("Cfg", (), {"extra": {}})()
    return adapter


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
    assert str(msg.id) not in adapter.turns


async def test_no_journal_or_ack_until_turn_completes(make_adapter, make_message):
    # The fire-and-forget loss window: a message whose turn never completes (crash
    # mid-turn) must NOT be journaled or acked, so it is re-dispatched next poll.
    adapter = make_adapter()
    msg = make_message()
    await _dispatch(adapter, msg)
    # Deliberately do NOT fire the callback (simulate a crash before the turn finishes).
    assert adapter.journal.is_consumed("me", str(msg.id)) is False
    assert adapter.client.acked == []
    assert str(msg.id) in adapter.turns  # still in flight


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


async def test_self_sent_funnel_webhook_is_dispatched(make_adapter, make_message):
    # A Funnel webhook is relayed as the agent itself (from == to), so it trips the
    # self-skip's sender match — but it is a legitimate inbound event and MUST wake.
    adapter = make_adapter(agent_handle="me")
    msg = make_message(sender_handle="me", msg_type="rine.v1.webhook")
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1  # the narrow self-skip exception fired


async def test_self_sent_non_webhook_is_still_suppressed(make_adapter, make_message):
    # Every OTHER self-sent type is the agent's own outbound echo and stays suppressed —
    # the exception is webhook-only, not a blanket "always dispatch self-sends".
    adapter = make_adapter(agent_handle="me")
    await _dispatch(adapter, make_message(sender_handle="me", msg_type="rine.v1.text"))
    assert adapter.events == []
    assert adapter.client.acked == []


async def test_funnel_webhook_journaled_exactly_once_after_turn(make_adapter, make_message):
    # A delivered Funnel webhook must engage exactly-once journaling. The
    # bug was that ``on_complete`` was never awaited, so the webhook woke the agent but
    # the consumed-id journal stayed EMPTY (degrading exactly-once → at-least-once) and
    # the inbox ack was incidental. Assert the public journaling behavior for the funnel
    # path: after the turn completes, the journal holds exactly one row for the webhook id
    # and it is acked; a redelivery is then suppressed (re-acked, not re-dispatched).
    adapter = make_adapter(agent_handle="me")
    msg = make_message(sender_handle="me", msg_type="rine.v1.webhook")
    await _dispatch(adapter, msg)

    # Woken, but journal/ack are deferred to the post-delivery callback (not fire-and-forget).
    assert len(adapter.events) == 1
    assert adapter.journal.is_consumed("me", str(msg.id)) is False
    assert adapter.client.acked == []

    # Turn completes → the awaited callback journals (exactly one row) then acks.
    await adapter.fire_post_delivery()
    assert adapter.journal.is_consumed("me", str(msg.id)) is True
    assert adapter.client.acked == [str(msg.id)]
    assert str(msg.id) not in adapter.turns

    # Redelivery of the journaled webhook is re-acked, never re-dispatched (exactly-once).
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1
    assert adapter.client.acked == [str(msg.id), str(msg.id)]


async def test_self_sent_funnel_webhook_dispatched_under_require_verified(
    make_adapter, make_message
):
    # A self-signed webhook verifies cleanly, so the strict-mode gate must not block it.
    adapter = make_adapter(agent_handle="me", require_verified=True)
    msg = make_message(
        sender_handle="me",
        msg_type="rine.v1.webhook",
        verification_status="verified",
        verified=True,
    )
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1


async def test_funnel_webhook_surfaces_hook_name(make_adapter, make_message):
    # The cleartext rine.hook_name metadata is surfaced in the wake body so the agent
    # can tell which hook fired without inspecting raw metadata.
    adapter = make_adapter(agent_handle="me")
    msg = make_message(
        sender_handle="me",
        msg_type="rine.v1.webhook",
        plaintext='{"text":"payload"}',
        metadata={"rine.hook_name": "github-ci"},
    )
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1
    assert adapter.events[0].text == "[webhook: github-ci] payload"


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


@pytest.mark.parametrize(
    ("status", "decrypt_error"),
    [
        ("invalid", SIG_FAILURE),
        ("sender-mismatch", SENDER_MISMATCH),
    ],
)
async def test_refused_sender_never_dispatched_and_logged_as_a_warning(
    make_adapter, make_message, caplog, status, decrypt_error
):
    # The SDK refuses a row whose signature does not cover the bytes ("invalid") and one
    # whose signature belongs to an agent other than the sender the server names
    # ("sender-mismatch"). Both are security events, so both must reach the WARNING audit
    # trail — not the INFO line that also carries malformed payloads. Security invariant:
    # neither is ever dispatched. Operationally both are DRAINED (journaled + acked);
    # leaving them ``new`` would re-quarantine an unreadable backlog every poll.
    adapter = make_adapter()
    msg = make_message(
        plaintext=None,
        decrypt_error=decrypt_error,
        verification_status=status,
        verified=False,
    )
    with caplog.at_level("INFO", logger="hermes_rine.poll"):
        await _dispatch(adapter, msg)

    assert adapter.events == []  # never dispatched (security invariant)
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # drained: journaled
    assert adapter.client.acked == [str(msg.id)]  # drained: acked
    [record] = [r for r in caplog.records if r.name == "hermes_rine.poll"]
    assert record.levelname == "WARNING"
    assert status in record.getMessage()


async def test_unreadable_message_is_drained_at_info_not_as_a_security_warning(
    make_adapter, make_message, caplog
):
    # A malformed or unsupported-version payload is a compatibility problem, not an
    # attack. Logging it at WARNING alongside refused senders is what made the audit
    # trail unreadable in the first place — and matching on error prose is what put it
    # there, so a row with no authenticity verdict stays INFO whatever its text says.
    adapter = make_adapter()
    msg = make_message(
        plaintext=None,
        decrypt_error="Unsupported encryption_version 'mls-v1'",
        verification_status="unverifiable",
        verified=False,
    )
    with caplog.at_level("INFO", logger="hermes_rine.poll"):
        await _dispatch(adapter, msg)

    assert adapter.events == []
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # still drained
    [record] = [r for r in caplog.records if r.name == "hermes_rine.poll"]
    assert record.levelname == "INFO"


@pytest.mark.parametrize("x402_type", X402_TYPES)
async def test_x402_frame_wakes_payment_aware_not_drained(make_adapter, make_message, x402_type):
    # An x402 frame now WAKES the agent payment-aware (structured context + the
    # pay/fulfill tools) rather than draining silently. The wake body carries the frame
    # type + tool guidance; journal + ack are DEFERRED to the post-delivery callback (same
    # exactly-once machinery as a normal message), NOT done up front.
    adapter = make_adapter()
    msg = make_message(msg_type=x402_type, plaintext='{"x402Version":2,"accepts":[]}')
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1  # payment-aware turn scheduled
    assert "x402" in adapter.events[0].text  # structured payment context, not raw JSON
    assert adapter.journal.is_consumed("me", str(msg.id)) is False  # deferred, not drained
    assert adapter.client.acked == []
    # Turn completes → journaled + acked exactly once (like any woken message).
    await adapter.fire_post_delivery()
    assert adapter.journal.is_consumed("me", str(msg.id)) is True
    assert adapter.client.acked == [str(msg.id)]


async def test_x402_frame_arms_prose_suppression(make_adapter, make_message):
    # An x402 DM wake arms prose suppression at ``send`` for the sender chat_id, so the
    # turn's prose never lands in the payment thread. The pay/fulfill tools do the
    # in-thread reply themselves.
    adapter = make_adapter()
    msg = make_message(sender_handle="alice", msg_type="rine.v1.x402_payment_required")
    await _dispatch(adapter, msg)
    assert adapter.turns.x402_in_chat("alice")  # prose suppression armed
    # After the turn completes the suppression is released (no lingering swallow).
    await adapter.fire_post_delivery()
    assert adapter.turns.x402_in_chat("alice") is False


async def test_x402_receipt_wake_is_informational(make_adapter, make_message):
    # A receipt is informational (no reply expected): it still wakes so the agent knows the
    # payment settled.
    adapter = make_adapter()
    msg = make_message(sender_handle="alice", msg_type="rine.v1.x402_receipt")
    await _dispatch(adapter, msg)
    assert len(adapter.events) == 1
    assert "receipt" in adapter.events[0].text.lower()


async def test_x402_frame_redelivery_after_turn_re_acked_not_re_dispatched(
    make_adapter, make_message
):
    # A woken x402 frame is journaled after its turn, so a redelivery is recognised as
    # consumed and re-acked — never dispatched twice (exactly-once, same as any message).
    adapter = make_adapter()
    msg = make_message(msg_type="rine.v1.x402_payment", plaintext='{"x402Version":2}')
    await _dispatch(adapter, msg)
    await adapter.fire_post_delivery()
    await _dispatch(adapter, msg)  # redelivery after the turn completed
    assert len(adapter.events) == 1  # exactly-once wake
    assert adapter.client.acked == [str(msg.id), str(msg.id)]  # re-acked, not re-dispatched


async def test_x402_scheduling_failure_releases_suppression(make_adapter, make_message):
    # If scheduling the x402 turn raises, the armed prose suppression must be released so a
    # later unrelated send to that chat is not silently swallowed (no lingering suppress).
    adapter = make_adapter(raise_on_handle=True)
    msg = make_message(sender_handle="alice", msg_type="rine.v1.x402_payment_required")
    await _dispatch(adapter, msg)
    assert adapter.turns.x402_in_chat("alice") is False


async def test_undecryptable_x402_frame_still_drained_not_woken(make_adapter, make_message):
    # An x402 frame the SDK could not decrypt has no trusted plaintext to act on: it must
    # still DRAIN at the decrypt_error step (journal + ack), never wake the agent.
    adapter = make_adapter()
    msg = make_message(
        msg_type="rine.v1.x402_payment_required", plaintext=None, decrypt_error="MLS unsupported"
    )
    await _dispatch(adapter, msg)
    assert adapter.events == []  # never woken on ciphertext
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # drained
    assert adapter.client.acked == [str(msg.id)]


async def test_non_x402_message_still_dispatched(make_adapter, make_message):
    # The guard is type-scoped: an ordinary task_request is unaffected (regression guard so
    # the x402 skip can never over-match and swallow normal traffic).
    adapter = make_adapter()
    await _dispatch(adapter, make_message(msg_type="rine.v1.task_request"))
    assert len(adapter.events) == 1


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
    assert str(msg.id) not in adapter.turns
    assert not adapter.registered_callbacks


async def test_failure_before_the_schedule_leaves_no_turn_behind(make_adapter, make_message):
    # Only the post-delivery callback and ``_dispatch``'s own except ever close a turn, and
    # ``_poll_loop`` swallows whatever escapes a dispatch. So a raise between the wake
    # decision and the schedule must not have opened one: an entry nothing removes blocks
    # that id's wake guard forever AND makes ``should_relay`` drop every unmarked send to
    # the chat for the life of the process.
    adapter = make_adapter()
    msg = make_message(sender_handle="alice")

    def _boom(**kwargs: object) -> object:
        raise RuntimeError("building the gateway source failed")

    adapter.build_source = _boom

    with pytest.raises(RuntimeError):
        await _dispatch(adapter, msg)

    assert str(msg.id) not in adapter.turns  # wake guard released → a later poll retries
    assert should_relay(adapter, "alice", None, None) is True  # unmarked sends still go out
    assert adapter.journal.is_consumed("me", str(msg.id)) is False
    assert adapter.client.acked == []


async def test_group_message_builds_group_source(make_adapter, make_message):
    adapter = make_adapter()
    msg = make_message(group_handle="#research")
    await _dispatch(adapter, msg)
    event = adapter.events[0]
    assert _field(event.source, "chat_type") == "group"
    assert _field(event.source, "chat_id") == "#research"


@pytest.mark.parametrize("group_handle", [None, "#research"])
async def test_dispatch_records_the_inbound_type_so_the_reply_can_mirror_it(
    make_adapter, make_message, group_handle
):
    # The gateway threads the inbound message id through send()'s reply_to but not its
    # type, so the adapter has to remember it for the duration of the turn.
    adapter = make_adapter()
    msg = make_message(sender_handle="alice", msg_type="rine.v1.dm", group_handle=group_handle)
    await _dispatch(adapter, msg)
    assert adapter.turns.message_type(str(msg.id)) == "rine.v1.dm"


async def test_completed_turn_forgets_the_inbound_type(make_adapter, make_message):
    # The memory lasts one turn. Left behind, it would grow without bound on a long-lived
    # gateway and type a later reply after a message it no longer answers.
    adapter = make_adapter()
    msg = make_message(sender_handle="alice", msg_type="rine.v1.dm")
    await _dispatch(adapter, msg)
    await adapter.fire_post_delivery()
    assert adapter.turns.message_type(str(msg.id)) is None


async def test_scheduling_failure_forgets_the_inbound_type(make_adapter, make_message):
    adapter = make_adapter(raise_on_handle=True)
    msg = make_message(sender_handle="alice", msg_type="rine.v1.dm")
    await _dispatch(adapter, msg)
    assert adapter.turns.message_type(str(msg.id)) is None


@pytest.mark.parametrize("terminal_type", ["rine.v1.error", "rine.v1.receipt"])
async def test_terminal_inbound_type_drains_without_waking(
    make_adapter, make_message, terminal_type
):
    # An error notice and a delivery receipt close an exchange rather than open one.
    # Waking a turn on one — whose routed reply the peer then wakes on — is how two
    # auto-replying agents keep each other awake indefinitely.
    adapter = make_adapter()
    msg = make_message(sender_handle="alice", msg_type=terminal_type)
    await _dispatch(adapter, msg)
    assert adapter.events == []  # no turn woken
    assert adapter.journal.is_consumed("me", str(msg.id)) is True  # drained, cannot replay
    assert adapter.client.acked == [str(msg.id)]


async def test_ignore_types_env_can_reopen_a_terminal_type(
    make_adapter, make_message, monkeypatch
):
    monkeypatch.setenv("RINE_IGNORE_TYPES", "")
    adapter = make_adapter()
    await _dispatch(adapter, make_message(sender_handle="alice", msg_type="rine.v1.error"))
    assert len(adapter.events) == 1


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


async def test_post_delivery_callback_survives_the_gateways_generation_pop(tmp_path, make_message):
    # Everything exactly-once rests on this callback: the gateway pops it with the run
    # generation it bound to the turn, and an entry registered without a generation is
    # dropped for any non-None generation. An adapter cannot register with a matching
    # one — the token is minted inside the message handler, after _dispatch has already
    # handed the turn over — so without the adapter's own pop the journal write and the
    # ack never run, the message stays ``new``, and the turn never closes.
    adapter = _real_adapter(tmp_path, RecordingClient())
    msg = make_message()
    await _dispatch(adapter, msg)
    [session_key] = list(adapter._post_delivery_callbacks)

    callback = adapter.pop_post_delivery_callback(session_key, generation=7)
    assert callback is not None
    await callback()

    assert adapter.journal.is_consumed("me", str(msg.id)) is True
    assert adapter.client.acked == [str(msg.id)]
    assert str(msg.id) not in adapter.turns


async def test_generation_owned_callbacks_keep_the_gateways_ownership_check(tmp_path):
    # The relaxation is scoped to entries registered without a generation, which are only
    # ever ours. A gateway callback that claims a generation still belongs to that run, so
    # a stale run cannot clear a fresher run's callback.
    adapter = _real_adapter(tmp_path, RecordingClient())
    adapter.register_post_delivery_callback("s", lambda: None, generation=3)

    assert adapter.pop_post_delivery_callback("s", generation=4) is None
    assert adapter.pop_post_delivery_callback("s", generation=3) is not None


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
    assert str(msg.id) not in adapter.turns  # in-flight mark still cleared
