"""In-flight inbound turns — the context every outbound decision reads.

An agent turn woken by a rine message needs three things remembered for exactly as
long as that turn runs, and the gateway threads none of them back through ``send``:

* **which chat it belongs to**, so interim chatter inside the turn can be told apart
  from traffic the host originates on its own (a cron job's scheduled delivery, the
  agent's ``send_message`` tool). Only the first kind can duplicate the turn's answer;
  the second has no turn to duplicate and is relayed;
* **what type the waking message carried**, so the routed reply mirrors it
  (``_outbound.reply_type_for``) — ``send`` receives the inbound id, never its type;
* **whether it was an x402 payment frame**, whose prose reply is suppressed entirely
  because the pay/fulfill tools thread the signed payload or receipt in place.

One registry holds all three, keyed by message id, because they share one lifetime:
opened by ``_poll._dispatch`` just before it schedules the turn, closed by the
post-delivery callback the turn fires (or immediately, if scheduling failed). That
lifetime is also the wake guard — an id still in the registry has a turn running, so a
poll interval shorter than a turn cannot dispatch it twice.

**Why a turn also remembers its session key.** The gateway's post-delivery callback
registry is keyed by *session* — peer and chat — never by message id, and it fires one
callback per completed turn without saying which turn completed. Two messages from one
peer therefore share a key, so a callback written to close "its own" message closes the
wrong one: the second turn is ended before it has run, and a turn that has been ended is
invisible to every outbound decision above (its interim narration is relayed to the peer
as real traffic, its reply type is forgotten, its x402 suppression is lost).

So the completion order is reconstructed here instead of being asked of the gateway.
``_turns`` is an insertion-ordered dict, and Hermes runs same-session turns serially, so
the oldest outstanding turn for a session key IS the one that just finished —
:meth:`InboundTurns.oldest_for_session` is what a callback resolves its target through,
and :meth:`InboundTurns.has_session` is what tells ``_dispatch`` a callback is already
registered for this session and a second one must not be.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundTurn:
    """One inbound rine message whose agent turn is scheduled but not yet finished."""

    chat_id: str
    message_type: str
    session_key: str
    x402: bool = False


class InboundTurns:
    """The turns currently in flight, keyed by the id of the message that woke each."""

    def __init__(self) -> None:
        self._turns: dict[str, InboundTurn] = {}

    def begin(self, message_id: str, turn: InboundTurn) -> None:
        """Open ``message_id``'s turn (wake guard + outbound context for its chat)."""
        self._turns[message_id] = turn

    def end(self, message_id: str) -> None:
        """Close a turn once it has completed, or failed to schedule at all."""
        self._turns.pop(message_id, None)

    def __contains__(self, message_id: object) -> bool:
        """True while that message id's turn is running (no re-dispatch, no re-ack)."""
        return message_id in self._turns

    def message_type(self, message_id: str | None) -> str | None:
        """The type the inbound message carried, or None once its turn is over."""
        turn = self._turns.get(message_id) if message_id else None
        return turn.message_type if turn is not None else None

    def in_chat(self, chat_id: str) -> bool:
        """True while an inbound message has a turn running for ``chat_id``."""
        return any(turn.chat_id == chat_id for turn in self._turns.values())

    def x402_in_chat(self, chat_id: str) -> bool:
        """True while an x402 payment frame has a turn running for ``chat_id``."""
        return any(turn.chat_id == chat_id and turn.x402 for turn in self._turns.values())

    def has_session(self, session_key: str) -> bool:
        """True while any turn for ``session_key`` is still open.

        ``_dispatch`` reads this to decide whether the gateway already holds a
        post-delivery callback for this session: registering a second one under the same
        key is what closes an unrun turn, so the second message rides the first
        registration instead.
        """
        return any(turn.session_key == session_key for turn in self._turns.values())

    def oldest_for_session(self, session_key: str) -> str | None:
        """The id of the longest-open turn for ``session_key``, or None if there is none.

        The gateway fires one post-delivery callback per completed turn but does not say
        which turn completed. Hermes runs same-session turns serially and ``_turns``
        preserves insertion order, so the oldest outstanding turn for the session is the
        one that just finished — which is the message the callback must journal, ack and
        close.
        """
        for message_id, turn in self._turns.items():
            if turn.session_key == session_key:
                return message_id
        return None


__all__ = ["InboundTurn", "InboundTurns"]
