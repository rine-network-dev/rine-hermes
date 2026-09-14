"""``rine_retry_unreadable`` — retry the durable set-aside list on demand.

The wake channel parks every inbound message it could not read (``_setaside``) and
retries it on a tick. This is the same work on demand, for the case the tick cannot
serve: the operator has just fixed the cause — accepted a group invite so the sender key
arrives, restored a config dir — and wants the backlog now rather than at the next sweep.

It re-reads with **peek** (``X-Rine-Peek``), so a row that is still unreadable is not
consumed by the attempt, and it reports every row it holds — including the ones that
will never be retried — because "where did my message go?" is the question the list
exists to answer. Nothing is sent. Ciphertext never reaches the result: the body goes
through the same ``_format`` renderers every other read uses.

🔴 It keys the store by the acting agent's **handle**, the same key
``RineAdapter.connect`` records under. A different key convention here would list an
empty set-aside list on a perfectly healthy install, which reads as "nothing was ever
parked" rather than as a bug.
"""

from __future__ import annotations

import logging
from typing import Any

from rine import NotFoundError, RineClient
from rine.sender_key_words import SenderKeyRefusal
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import (
    _format_error,
    acting_agent,
    build_client,
    primary_handle,
    settle_actor,
)
from hermes_rine._format import render_message
from hermes_rine._setaside import (
    SETASIDE_MAX_ATTEMPTS,
    SetAsideReason,
    SetAsideRow,
    SetAsideStore,
)

logger = logging.getLogger("hermes_rine.unreadable")

#: Why a listed row is not being retried — stated per reason, because the two are
#: different answers: one is "it never can", the other "it was tried and gave up".
_EXHAUSTED: dict[SetAsideReason, str] = {
    SetAsideReason.AUTHENTICITY: (
        "refused for authenticity; never retried (those bytes cannot start to verify)"
    ),
    SetAsideReason.UNREADABLE: f"gave up after {SETASIDE_MAX_ATTEMPTS} attempts",
}


def _line(row: SetAsideRow, verdict: str) -> str:
    """One listed row: ids and counters, then what happened to it. Never content."""
    return f"{row.message_id} [{row.reason}] attempts={row.attempts}: {verdict}"


async def _store_key(client: RineClient, acting: str | None) -> str:
    """The handle the wake channel keys this agent's set-aside rows by.

    ``RineAdapter.connect`` records under the first live agent's handle, so the verb has
    to arrive at the same string. ``settle_actor`` has already resolved whatever the call
    named into an agent id, so the work left is mapping that id back to its handle; when
    nothing was named, this takes the same first live agent ``connect`` takes.
    """
    me = await client.whoami()
    if acting:
        for agent in me.agents:
            if str(agent.id) == acting:
                return agent.handle
    return primary_handle(me)


async def _retry_row(
    client: RineClient,
    store: SetAsideStore,
    handle: str,
    acting: str | None,
    row: SetAsideRow,
    refusal: SenderKeyRefusal,
) -> tuple[str, bool]:
    """Peek-read one parked row; return its report line and whether it opened."""
    try:
        msg = await client.read(row.message_id, agent=acting, peek=True)
    except NotFoundError:
        store.forget(handle, row.message_id)
        return _line(row, "gone from the server; dropped from the list"), False
    except Exception as err:  # noqa: BLE001 — one bad row must not hide the rest
        return _line(row, f"retry failed: {_format_error(err)}"), False
    if msg.decrypt_error is None:
        store.forget(handle, row.message_id)
        return f"{row.message_id} opened: {render_message(msg, refusal)}", True
    # 🔴 ``touch``, never ``record``: an on-demand look must not spend the AUTOMATIC
    # retry's budget. This verb is model-facing and gated by no allowlist, so a model
    # that retries a few times "helpfully" while a sender key is still in flight would
    # exhaust every live row — the tick would then have nothing due, forever, and the
    # documented remedy ("fix the cause, the sweep will get it") would be silently dead.
    # ``touch`` returns None only if something else dropped the row mid-walk.
    looked = store.touch(handle, row.message_id) or row
    return _line(looked, f"still unreadable ({msg.decrypt_error})"), False


async def _handle_retry_unreadable(args: dict[str, Any], **_: Any) -> str:
    """Retry every eligible parked message and list the rest (``rine_retry_unreadable``)."""
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            handle = await _store_key(client, acting)
            store = SetAsideStore()
            try:
                lines, opened, retried = await _retry_all(client, store, handle, acting)
            finally:
                store.close()
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        set_aside=len(lines),
        retried=retried,
        opened=opened,
        messages="\n".join(lines) if lines else "Nothing set aside.",
    )


async def _retry_all(
    client: RineClient,
    store: SetAsideStore,
    handle: str,
    acting: str | None,
) -> tuple[list[str], int, int]:
    """Walk the whole list: retry what is eligible, report what is not."""
    refusal = SenderKeyRefusal()
    lines: list[str] = []
    opened = 0
    retried = 0
    for row in store.entries(handle):
        if row.exhausted:
            lines.append(_line(row, _EXHAUSTED[row.reason]))
            continue
        retried += 1
        line, did_open = await _retry_row(client, store, handle, acting, row, refusal)
        lines.append(line)
        opened += int(did_open)
    if retried:
        logger.info("rine retry unreadable: %d of %d set-aside message(s) opened", opened, retried)
    return lines, opened, retried


__all__ = ["_handle_retry_unreadable"]
