"""Bounded per-DM-peer reply-target stash (REQ-FORK-01).

Maps a DM ``chat_id`` (the sender handle, exactly what ``RineAdapter.send`` receives)
to the latest inbound rine message id, so the agent's reply turn replies IN-PLACE
(via the reply endpoint, preserving the inbound conversation) instead of forking a
NEW conversation through ``client.send``. ``_poll._dispatch`` records each inbound DM;
``RineAdapter.send`` reads it. Group replies stay broadcasts (no stash).

Bounded like the eve channel's ``SEEN_CAP``: overwrite-per-key keeps one short id per
peer, and the whole map is cleared wholesale once it exceeds the cap — no LRU needed
for what is at most one id per distinct DM peer.

Known v0.1 edge case (REQ-FORK-01): the key is the DM ``chat_id``, but ``send`` only
ever receives that ``chat_id`` — never the specific inbound id of the turn it answers
(the Hermes gateway ``send`` seam carries no per-turn message id). So if two inbound DMs
from the *same* peer are dispatched before the first reply turn runs, the second's
``record`` overwrites the first, and both reply turns then link to the latest inbound id.
This is latest-wins by construction, mirrors the same-sender callback-chaining edge case
in ``_poll`` (which fires both turns' callbacks after whichever completes next), and is
purely cosmetic: a same-peer reply-in-place always lands in that peer's existing
conversation regardless of which exact parent id is referenced (the server's reply
endpoint routes by the original's parties, not the exact message), so there is NO fork.
Precise per-turn parent linkage would require the gateway to thread the inbound id
through the turn context; not warranted for v0.1.
"""

from __future__ import annotations

# Soft cap on the number of distinct DM peers tracked at once.
_REPLY_TO_CAP = 5000


class ReplyStash:
    """A bounded ``chat_id -> latest inbound message id`` map."""

    def __init__(self, cap: int = _REPLY_TO_CAP) -> None:
        self._cap = cap
        self._by_chat: dict[str, str] = {}

    def record(self, chat_id: str, message_id: str) -> None:
        """Stash the latest inbound message id for ``chat_id`` (clears on overflow)."""
        if len(self._by_chat) >= self._cap and chat_id not in self._by_chat:
            self._by_chat.clear()
        self._by_chat[chat_id] = message_id

    def get(self, chat_id: str) -> str | None:
        """The latest inbound message id stashed for ``chat_id``, or None."""
        return self._by_chat.get(chat_id)


__all__ = ["ReplyStash"]
