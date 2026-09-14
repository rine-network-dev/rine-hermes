"""The set-aside retry sweep: re-read parked messages on a tick, wake on the ones that open.

An inbound message the agent could not read is acked and parked on the durable set-aside
list (:mod:`hermes_rine._setaside`) rather than left ``new``. This module is what brings
it back when the cause clears — a group sender key that arrives late, a restored config
dir — so "unreadable" stops meaning "gone".

Split out of :mod:`hermes_rine._poll` because the retry's accounting is a policy of its
own and does not belong in the wake decision: ``_poll._dispatch`` decides whether a
*fresh* message wakes a turn, while this module decides how many times a *parked* one is
worth re-reading and when to stop. ``_poll`` does not import this module; this module
imports ``_poll``.

🔴 Three properties, each answering a way the retry can make things worse than the
forget-it behaviour it replaces:

**The re-read is a peek** (``X-Rine-Peek``), so the retry carries no delivery side effect
of its own. 🔴 On *this* surface that suppresses a mark which is already stamped:
``_poll._dispatch`` parks an unreadable message and then acks it unconditionally, so every
row this module sees is already ``delivered_at``-marked and a plain read would consume
nothing that was not consumed at park time. The peek is here so the retry's correctness
does not rest on that — one rule for the re-read on every surface that keeps a set-aside
list, including the ones that park a row **without** acking it, where a plain read is the
"the retry consumed my message" failure the list exists to prevent.

🔴 Never read that backwards, as "the sweep peeks, so the park path could stop acking".
Leaving an unreadable row ``new`` is the design that was rejected: the poll loop would
re-offer it on every tick and re-quarantine it forever (the log/IO storm), and a peeking
sweep does nothing to prevent that.

**A still-sealed row is never re-dispatched.** :func:`_retry_row` counts the attempt
itself and returns. Handing a still-unreadable message back to ``_dispatch`` would re-ack
an already-acked row and re-log it at INFO on **every tick, forever** — the log/IO storm
that got the never-ack design rejected, rebuilt inside its replacement.

**Every outcome spends an attempt or drops the row.** A row only survives a tick untouched
when the read itself failed transiently. Anything else — still sealed, refused, opened but
declined by policy — moves the counter, so the oldest-first batch cannot be permanently
occupied by rows that will never resolve while newer ones starve behind them. That is what
:class:`hermes_rine._poll.DispatchOutcome` is for.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from rine import NotFoundError

from hermes_rine._poll import DispatchOutcome, _dispatch, _safe_setaside, setaside_reason
from hermes_rine._setaside import SetAsideReason, sweep_interval_seconds

if TYPE_CHECKING:
    from hermes_rine._setaside import SetAsideRow
    from hermes_rine.adapter import RineAdapter

logger = logging.getLogger("hermes_rine.sweep")

__all__ = ["_retry_row", "_sweep_loop", "_sweep_once"]


def _count_attempt(adapter: RineAdapter, row: SetAsideRow, reason: SetAsideReason) -> None:
    """Spend one retry attempt on a row that did not resolve; log the give-up once.

    The increment lives in the store's upsert, so the ``attempts`` reported here is the
    committed one. The WARNING fires only on the ``exhausted`` **transition** — a row that
    was already given up on is not re-announced, and a row re-classified as an
    authenticity failure announces itself on the same line because ``reason`` is in it.
    """
    updated = _safe_setaside(
        adapter,
        row.message_id,
        reason,
        exhausted=reason is SetAsideReason.AUTHENTICITY,
    )
    if updated is not None and updated.exhausted and not row.exhausted:
        logger.warning(
            "rine wake: giving up on set-aside message %s after %d attempt(s) (%s); it stays "
            "listed for rine_unreadable_list but the sweep will not retry it again",
            row.message_id,
            updated.attempts,
            updated.reason,
        )


async def _retry_row(adapter: RineAdapter, row: SetAsideRow) -> None:
    """Peek-read one parked message and act on what comes back. Never raises out.

    Four outcomes: the server no longer has it (forget), the read failed transiently
    (leave it alone — a network blip is not the message's fault and must not burn its
    budget), it is still sealed (count an attempt, do not dispatch), or it opens and goes
    through the ordinary ``_dispatch`` — which is what journals it, acks it and wakes the
    turn, so the retry path and the live path agree on policy by construction.
    """
    agent = adapter.agent_handle
    try:
        msg = await adapter.client.read(row.message_id, peek=True)
    except NotFoundError:
        logger.info(
            "rine wake: set-aside message %s is gone from the server; forgetting it",
            row.message_id,
        )
        adapter.setaside.forget(agent, row.message_id)
        return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a transient read must not end the sweep
        logger.warning("rine wake: set-aside re-read failed for %s", row.message_id, exc_info=True)
        return

    if msg.decrypt_error:
        # Still sealed. The attempt is counted HERE rather than by re-entering
        # ``_dispatch``, which would ack an already-acked row and re-log it every tick.
        # Re-classified on every read: a row parked as merely unreadable that comes back
        # forged must stop being retried, not keep its old verdict.
        _count_attempt(adapter, row, setaside_reason(msg))
        return

    logger.info("rine wake: set-aside message %s now opens; dispatching", row.message_id)
    outcome = await _dispatch(adapter, msg)
    if outcome is DispatchOutcome.HANDLED:
        adapter.setaside.forget(agent, row.message_id)
        return
    if outcome is DispatchOutcome.DEFERRED:
        # It opened but ``_dispatch`` declined it: a policy refusal (disallowed sender,
        # strict-mode unverifiable) or a turn already in flight. Nothing recorded it, and
        # a policy refusal re-decides identically forever — so spend the attempt here.
        logger.info(
            "rine wake: set-aside message %s opened but was not handled; counting the attempt",
            row.message_id,
        )
        _count_attempt(adapter, row, row.reason)
    # DispatchOutcome.PARKED: it re-sealed between the peek and the dispatch and
    # ``_dispatch`` already re-recorded it, spending the attempt itself. Counting a second
    # one here would halve the bound for exactly the rows most likely to heal.


async def _sweep_once(adapter: RineAdapter) -> None:
    """Retry every due set-aside row for this agent, oldest first.

    Bounded twice over: ``SetAsideStore.due`` returns at most ``SETASIDE_SWEEP_BATCH``
    rows (so a full list is covered across consecutive ticks instead of in one burst),
    and each row carries an attempt counter that drops it out of ``due`` for good.
    """
    for row in adapter.setaside.due(adapter.agent_handle):
        await _retry_row(adapter, row)


async def _sweep_loop(adapter: RineAdapter) -> None:
    """Retry the set-aside list on a tick — not on demand only.

    Its own task rather than a step in the listener's loop body, because ``_sse_loop``
    sits inside ``_consume_stream`` for hours without returning: a tick hung off the
    listener would never fire under ``RINE_TRANSPORT=sse``. That is the transport that
    needs it most — the server counts a frame yielded over an authenticated agent stream
    as a delivery (``mark_stream_delivered``), so by the time any client sees an SSE
    message the row is already consumed and only a local record can bring it back.

    It sleeps FIRST: nothing is parked the instant a gateway connects, and a sweep racing
    the listener's own first pass would re-read rows that pass is about to park.
    """
    while adapter.running:
        await asyncio.sleep(sweep_interval_seconds())
        if not adapter.running:
            break
        try:
            await _sweep_once(adapter)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — a transient sweep error must not kill the loop
            logger.error("rine wake: set-aside sweep error", exc_info=True)
