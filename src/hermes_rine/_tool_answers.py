"""What the model already said, through a rine tool, during an inbound turn.

The adapter answers a woken turn by relaying its final text. When the model has already
answered the same message with ``rine_reply``, or sent into the same conversation or group
with ``rine_send`` / ``rine_send_and_wait``, or sent to the peer who wrote to it, that second
message is a duplicate the peer stores and pays for. The tools write what they delivered here
and :func:`already_answered` reads it before the adapter sends. A direct send always opens a
conversation of its own, so a 1:1 send is tied to its inbound by the peer it went to.

Only a call that returned is recorded, so a failed call never suppresses the adapter's
answer. The tools build their own clients and hold no reference to the adapter, so this is
one process-wide ledger. Every entry belongs to the gateway session whose turn made it
(``HERMES_SESSION_KEY``), so a send from another chat's concurrent turn answers nothing here.
Hermes runs the turns of one session one after another and :meth:`InboundTurns.end` drops
the session's entries when its turn finishes, which keeps an earlier turn's send from
silencing a later one. It is bounded: the oldest entry falls out first.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from hermes_rine.adapter import RineAdapter

logger = logging.getLogger("hermes_rine.outbound")

_MAX_ENTRIES = 512


def current_session_key() -> str:
    """The gateway session the running tool call belongs to, or "" outside one."""
    try:
        from gateway.session_context import get_session_env  # type: ignore[import-not-found]
    except ImportError:
        return ""
    return str(get_session_env("HERMES_SESSION_KEY", ""))


class _Entry(NamedTuple):
    session_key: str
    key: str


class ToolAnswers:
    """Delivered tool messages, each owned by the session whose turn sent it."""

    def __init__(self) -> None:
        self._entries: deque[_Entry] = deque(maxlen=_MAX_ENTRIES)

    def note_reply(self, message_id: str) -> None:
        """``rine_reply`` delivered a reply to ``message_id``."""
        self._add(f"reply:{message_id}")

    def note_send(
        self, conversation_id: str, group_handle: str | None, recipient_handle: str | None
    ) -> None:
        """``rine_send`` / ``rine_send_and_wait`` delivered to this conversation, group or peer."""
        self._add(f"conversation:{conversation_id}")
        if group_handle:
            self._add(f"group:{group_handle.lower()}")
        if recipient_handle:
            self._add(f"peer:{recipient_handle.lower()}")

    def _add(self, key: str) -> None:
        if session_key := current_session_key():
            self._entries.append(_Entry(session_key, key))

    def forget(self, session_key: str) -> None:
        """Drop what the session's finished turn delivered, so the next turn starts clean."""
        kept = [entry for entry in self._entries if entry.session_key != session_key]
        self._entries = deque(kept, maxlen=_MAX_ENTRIES)

    def _has(self, session_key: str, key: str) -> bool:
        return _Entry(session_key, key) in self._entries

    def reason(
        self,
        session_key: str,
        *,
        message_id: str,
        conversation_id: str,
        group_handle: str | None,
        peer_handle: str | None,
    ) -> str | None:
        """Why a tool already answered this session's turn, or None when none did."""
        if self._has(session_key, f"reply:{message_id}"):
            return f"rine_reply already answered it ({message_id})"
        if conversation_id and self._has(session_key, f"conversation:{conversation_id}"):
            return f"a rine tool already sent into its conversation ({conversation_id})"
        if group_handle and self._has(session_key, f"group:{group_handle.lower()}"):
            return f"a rine tool already sent to its group ({group_handle})"
        if peer_handle and self._has(session_key, f"peer:{peer_handle.lower()}"):
            return f"a rine tool already sent to its sender ({peer_handle})"
        return None


TOOL_ANSWERS = ToolAnswers()


def already_answered(adapter: RineAdapter, message_id: str | None) -> bool:
    """True, with one log line saying why, when a tool answered ``message_id``'s turn."""
    turn = adapter.turns.get(message_id)
    if message_id is None or turn is None:
        return False
    reason = TOOL_ANSWERS.reason(
        turn.session_key,
        message_id=message_id,
        conversation_id=turn.conversation_id,
        group_handle=turn.group_handle,
        peer_handle=None if turn.group_handle else turn.chat_id,
    )
    if reason is None:
        return False
    logger.info("[rine] no auto-reply to %s (%s)", message_id, reason)
    return True


__all__ = ["TOOL_ANSWERS", "ToolAnswers", "already_answered"]
