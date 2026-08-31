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
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundTurn:
    """One inbound rine message whose agent turn is scheduled but not yet finished."""

    chat_id: str
    message_type: str
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


__all__ = ["InboundTurn", "InboundTurns"]
