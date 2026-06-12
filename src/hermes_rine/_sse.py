"""SSE transport — server-push wake, sharing ``_poll._dispatch`` for the policy.

Selected by ``RINE_TRANSPORT=sse``. Instead of polling, this async-iterates
``client.stream(agent=...)`` and, for each ``message`` server-sent event, resolves the
message id from the event ``data`` (a JSON ``MessageRead``) and re-fetches it with
``client.read(id)`` to get a **decrypted** ``DecryptedMessage`` — the exact type
``_dispatch`` expects. (The raw stream ``data`` carries ``encrypted_payload``; we never
read that field — we only lift the ``id`` and let the SDK decrypt via ``read``, so the
no-ciphertext invariant holds and the journal/verify/ack policy is shared verbatim with
poll.) Non-``message`` events (``heartbeat``, ``connected``) are ignored.

The stream is long-lived; on any stream-iteration error or clean end it reconnects with
**bounded exponential backoff** (``_BASE_DELAY`` → ``_MAX_DELAY``), resetting the delay
after a healthy run. ``disconnect()`` cancels the task, unwinding via ``CancelledError``.

**Per-message isolation.** The per-event ``read()`` + ``_dispatch`` pair is wrapped in its
own try/except *inside* the loop so a single bad message cannot tear down the live stream
(the asymmetry the poll path doesn't have). ``CancelledError`` is re-raised so
``disconnect()`` still unwinds. A poison-pill ``CryptoError`` (an undecryptable / tampered
message — ``read()`` *raises* it on the SSE path, unlike the poll path where the SDK
swallows it into ``decrypt_error``) is drained via the shared ``drain_poison_pill`` so it
never wedges the stream — **never surfacing ciphertext**. A transient error (429 / 404 /
timeout / 5xx) is logged at WARNING and the event is skipped (``continue``) rather than
collapsing the whole connection into a reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from rine import CryptoError

from hermes_rine._poll import _dispatch, drain_poison_pill

if TYPE_CHECKING:
    from hermes_rine.adapter import RineAdapter

logger = logging.getLogger("hermes_rine.sse")

_BASE_DELAY = 1.0
_MAX_DELAY = 60.0


def _event_message_id(data: str) -> str | None:
    """Extract a message id from a ``message`` event's JSON ``data``, or None.

    Reads only the ``id`` field — never ``encrypted_payload`` — so ciphertext never
    leaves the stream frame. Malformed / id-less frames are skipped.
    """
    try:
        payload = json.loads(data)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        message_id = payload.get("id")
        if isinstance(message_id, str) and message_id:
            return message_id
    return None


async def _consume_stream(adapter: RineAdapter) -> None:
    """Iterate one live stream connection, dispatching each ``message`` event once.

    Each event's ``read()`` + ``_dispatch`` is isolated so one bad message can't tear
    down the stream: ``CancelledError`` re-raises (so ``disconnect()`` unwinds), a
    poison-pill ``CryptoError`` is drained (never re-raised), and a transient error is
    logged + skipped. Only an error from ``stream()`` iteration itself reaches
    ``_sse_loop`` and triggers a reconnect.
    """
    async for event in adapter.client.stream():
        if event.event != "message":
            continue
        message_id = _event_message_id(event.data)
        if message_id is None:
            logger.debug("rine sse: skipping message event with no resolvable id")
            continue
        try:
            # Re-fetch through the SDK so we hold a *decrypted* message (no ciphertext).
            msg = await adapter.client.read(message_id)
            await _dispatch(adapter, msg)
        except asyncio.CancelledError:
            raise  # disconnect() requested unwind — propagate, never swallow
        except CryptoError as err:
            # Poison pill: the SDK raises here (poll swallows into decrypt_error). Drain
            # it via the shared policy so it can't wedge the stream — no ciphertext.
            await drain_poison_pill(adapter, message_id, str(err))
        except Exception:  # noqa: BLE001 — transient (429/404/timeout/5xx): skip this one
            logger.warning(
                "rine sse: skipping message %s after a read/dispatch error",
                message_id,
                exc_info=True,
            )


async def _sse_loop(adapter: RineAdapter) -> None:
    """Run the SSE listener, reconnecting with bounded exponential backoff."""
    delay = _BASE_DELAY
    while adapter.running:
        try:
            await _consume_stream(adapter)
            delay = _BASE_DELAY  # clean end of stream — reconnect promptly
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — stream dropped; back off and retry
            logger.warning("rine sse: stream error; reconnecting in %.1fs", delay, exc_info=True)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            delay = min(delay * 2, _MAX_DELAY)


__all__ = ["_sse_loop"]
