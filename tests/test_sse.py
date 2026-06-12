"""SSE transport per-message isolation tests (``_sse._consume_stream``).

A single bad message must never tear down the live stream (the asymmetry vs poll the
audit flagged). These tests drive ``_consume_stream`` against a fake stream client whose
``read()`` is scripted per id: a good read dispatches + completes; a poison-pill
``CryptoError`` is drained (journaled/acked for unsupported, quarantined for a signature
failure) without raising; a transient error is skipped — and in every case the iterator
keeps advancing to the following events.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest
from rine import CryptoError, RateLimitError

from hermes_rine._sse import _consume_stream

MSG_GOOD = "11111111-1111-1111-1111-111111111111"
MSG_POISON = "22222222-2222-2222-2222-222222222222"
MSG_AFTER = "33333333-3333-3333-3333-333333333333"
MSG_SIG = "44444444-4444-4444-4444-444444444444"

SIG_FAILURE = "Sender signature verification failed"


def _event(message_id: str | None, *, kind: str = "message"):
    """A minimal duck-typed SSE event (``.event`` + ``.data``)."""
    data = json.dumps({"id": message_id}) if message_id is not None else "{}"
    return type("Event", (), {"event": kind, "data": data})()


class StreamClient:
    """Fake ``RineClient`` for SSE tests: scripted ``stream()`` + per-id ``read()``.

    ``reads`` maps a message id to either a ``DecryptedMessage`` (success) or an
    ``Exception`` instance to raise. Records the ids whose ``read()`` was attempted so a
    test can prove the iterator kept advancing past a bad one.
    """

    def __init__(self, events, reads) -> None:
        self._events = events
        self._reads = reads
        self.read_attempts: list[str] = []
        self.acked: list[str] = []

    async def stream(self, *, agent=None):
        for ev in self._events:
            yield ev

    async def read(self, message_id: str):
        self.read_attempts.append(message_id)
        outcome = self._reads[message_id]
        if isinstance(outcome, BaseException):  # incl. CancelledError (BaseException in 3.11)
            raise outcome
        return outcome

    async def mark_delivered(self, ids: list[str]) -> int:
        self.acked.extend(ids)
        return len(ids)


def _adapter_with_stream(make_adapter, events, reads):
    adapter = make_adapter()
    adapter.client = StreamClient(events, reads)
    return adapter


async def test_poison_pill_unsupported_is_drained_not_stream_torn_down(make_adapter, make_message):
    good = make_message(message_id=UUID(MSG_GOOD))
    after = make_message(message_id=UUID(MSG_AFTER))
    adapter = _adapter_with_stream(
        make_adapter,
        [_event(MSG_POISON), _event(MSG_GOOD), _event(MSG_AFTER)],
        {
            MSG_POISON: CryptoError("HPKE decryption failed — unsupported"),
            MSG_GOOD: good,
            MSG_AFTER: after,
        },
    )
    await _consume_stream(adapter)
    # The poison pill did NOT tear down the stream — both later messages were read.
    assert adapter.client.read_attempts == [MSG_POISON, MSG_GOOD, MSG_AFTER]
    # The poison pill drained via journal + ack (it was not a signature failure).
    assert adapter.journal.is_consumed("me", MSG_POISON) is True
    assert MSG_POISON in adapter.client.acked
    # Good messages each scheduled a turn (callbacks pending).
    assert len(adapter.events) == 2


async def test_signature_failure_poison_pill_never_dispatched_but_drained(make_adapter):
    adapter = _adapter_with_stream(
        make_adapter,
        [_event(MSG_SIG)],
        {MSG_SIG: CryptoError(SIG_FAILURE)},
    )
    await _consume_stream(adapter)
    # Signature failure: NEVER dispatched (security), but DRAINED (journaled + acked, with a
    # WARNING) so it can't replay every reconnect; the stream stays intact.
    assert adapter.events == []
    assert adapter.journal.is_consumed("me", MSG_SIG) is True
    assert MSG_SIG in adapter.client.acked


async def test_transient_read_error_skips_message_keeps_stream(make_adapter, make_message):
    after = make_message(message_id=UUID(MSG_AFTER))
    adapter = _adapter_with_stream(
        make_adapter,
        [_event(MSG_POISON), _event(MSG_AFTER)],
        {MSG_POISON: RateLimitError("rate limited"), MSG_AFTER: after},
    )
    await _consume_stream(adapter)
    # Transient error skipped (not journaled, not acked) but the stream kept going.
    assert adapter.client.read_attempts == [MSG_POISON, MSG_AFTER]
    assert adapter.journal.is_consumed("me", MSG_POISON) is False
    assert len(adapter.events) == 1  # the later good message still dispatched


async def test_cancelled_error_propagates_out_of_consume(make_adapter):
    adapter = _adapter_with_stream(
        make_adapter,
        [_event(MSG_POISON)],
        {MSG_POISON: asyncio.CancelledError()},
    )
    with pytest.raises(asyncio.CancelledError):
        await _consume_stream(adapter)  # disconnect() must still unwind
