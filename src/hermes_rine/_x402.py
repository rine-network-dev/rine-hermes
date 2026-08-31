"""x402 wake-path classification + the payment-aware context an agent reads (x402 v1.1).

Hermes used to DRAIN the three ``rine.v1.x402_*`` frames (never surfacing them) so a
text auto-reply could not corrupt a payment handshake. v1.1 re-surfaces them as
**payment-aware** wakes: the agent sees a structured payment context (:func:`x402_wake_body`)
and acts through the ``rine_pay`` / ``rine_fulfill`` tools, never a prose reply into the
payment thread (the prose auto-reply stays suppressed at ``adapter.send`` — see
``adapter.py``). The payer machinery itself lives in ``_payer``.

The type literals mirror ``_poll``'s original guard idiom (compared as strings, not the
SDK constants) so the wake classification never depends on the installed SDK exporting
named constants.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rine import DecryptedMessage

# The three first-party x402 payment frames (quote / signed authorization / receipt),
# compared as literals so this classification never depends on the installed rine SDK
# exporting named constants (mirrors ``_poll``'s original guard idiom).
PAYMENT_REQUIRED = "rine.v1.x402_payment_required"
PAYMENT = "rine.v1.x402_payment"
RECEIPT = "rine.v1.x402_receipt"
X402_MESSAGE_TYPES = frozenset({PAYMENT_REQUIRED, PAYMENT, RECEIPT})

# Cleartext marker key (the {status, rail}-only hint); a literal for the same reason.
_MARKER_KEY = "rine.x402"

_WAKE_GUIDANCE = {
    PAYMENT_REQUIRED: (
        "[x402 payment request] You have received a PAYMENT QUOTE in message {id} "
        "(marker: {marker}). To pay it, call rine_pay with message_id='{id}'. rine_pay "
        "signs and sends the authorization in-thread, bounded by your spend policy "
        "(deny-by-default, caps, permanent debit). Do NOT reply with prose — a text reply "
        "into a payment thread is ignored."
    ),
    PAYMENT: (
        "[x402 payment] You have received a SIGNED PAYMENT in message {id} (marker: "
        "{marker}); someone is paying you. To settle it and return a receipt, call "
        "rine_fulfill with message_id='{id}'. rine_fulfill verifies and settles through "
        "your facilitator and threads the receipt back. Do NOT reply with prose."
    ),
    RECEIPT: (
        "[x402 receipt] A PAYMENT RECEIPT arrived in message {id} (marker: {marker}) — "
        "the settlement of a payment you made. This is informational; no reply is "
        "expected. Call rine_read message_id='{id}' for the full settlement details."
    ),
}


def is_x402(msg: DecryptedMessage) -> bool:
    """Return True for any of the three first-party x402 payment frames.

    Keyed off the cleartext ``type`` field so it holds whether or not the encrypted
    body decrypted.
    """
    return msg.type in X402_MESSAGE_TYPES


def _marker_status(msg: DecryptedMessage) -> str:
    """The cleartext ``{status}`` from the x402 marker, or ``(none)`` when absent."""
    meta = msg.metadata.get(_MARKER_KEY) if isinstance(msg.metadata, dict) else None
    if isinstance(meta, dict) and isinstance(meta.get("status"), str):
        return str(meta["status"])
    return "(none)"


def x402_wake_body(msg: DecryptedMessage) -> str:
    """Render the payment-aware wake context an agent reads for an x402 frame.

    Per-type guidance points the agent at ``rine_pay`` / ``rine_fulfill`` (or, for a
    receipt, at ``rine_read``). Only the cleartext ``{status}`` marker is surfaced — never
    an amount — so the wake body cannot leak the economics that live in the E2EE payload.
    """
    template = _WAKE_GUIDANCE.get(msg.type, _WAKE_GUIDANCE[RECEIPT])
    return template.format(id=msg.id, marker=_marker_status(msg))


__all__ = [
    "PAYMENT",
    "PAYMENT_REQUIRED",
    "RECEIPT",
    "X402_MESSAGE_TYPES",
    "is_x402",
    "x402_wake_body",
]
