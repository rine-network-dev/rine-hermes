"""The shared x402 payer composition + opt-in gateway auto-pay (x402 v1.1).

One read→parse→policy→sign→reserve→reply ladder, reused by the ``rine_pay`` tool AND the
opt-in gateway auto-pay (:func:`maybe_auto_pay`). It wraps ``rine.x402`` free functions —
never reimplementing signing / policy / journal — and returns the ``rine_pay`` typed
status vocabulary verbatim, never raising for an expected refusal.

Kept separate from ``_x402`` (wake classification / context) so each stays under the
source-file cap and the wake path only pulls the payer machinery lazily-of-need.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rine.x402 import (
    X402Error,
    X402ErrorCode,
    is_auto_payable,
    load_policy,
    parse_x402_payload,
    prepare_payment,
    read_daily_spent,
    select_requirement,
    wallet_key_exists,
)

from hermes_rine._x402 import PAYMENT_REQUIRED

if TYPE_CHECKING:
    from rine import DecryptedMessage, RineClient

logger = logging.getLogger("hermes_rine.payer")

# The ``rine_pay`` typed-status vocabulary, reused VERBATIM (parity with the MCP tool and
# the cc-plugin skill). ``prepare_and_send_payment`` returns exactly these.
PAYMENT_SUBMITTED = "payment-submitted"
NO_WALLET = "no-wallet"
NOT_PAYMENT_REQUIRED = "not-payment-required"
POLICY_REFUSED = "policy-refused"
ABOVE_AUTO_PAY_THRESHOLD = "above-auto-pay-threshold"
ALREADY_PAID = "already-paid"
INAUTHENTIC_QUOTE = "inauthentic-quote"
SENDER_UNVERIFIABLE = "sender-unverifiable"

# Map the structured X402Error codes to the payer status vocabulary. Anything not a
# wallet-lock / replay is a policy refusal (deny-by-default, caps, bad requirement).
_STATUS_FOR_CODE = {
    X402ErrorCode.WALLET_BUSY: "wallet-busy",
    X402ErrorCode.ALREADY_PAID: ALREADY_PAID,
    X402ErrorCode.INAUTHENTIC_SENDER: INAUTHENTIC_QUOTE,
    X402ErrorCode.SENDER_UNVERIFIABLE: SENDER_UNVERIFIABLE,
}


@dataclass
class PayOutcome:
    """A typed payer result: a ``rine_pay``-vocabulary status + optional send facts."""

    status: str
    message_id: str | None = None
    conversation_id: str | None = None
    detail: str | None = None


def _auto_payable(config_dir: str, agent_id: str, pr: dict[str, Any]) -> bool:
    """Whether the selectable requirement is at/below the policy's autoPayThreshold.

    A read-only pre-check used ONLY on the ``auto_pay`` path so a sub-threshold gate can
    refuse BEFORE :func:`prepare_payment` reserves (the reserve is a permanent debit).
    ``select_requirement`` may raise :class:`X402Error` (deny-by-default / caps); that
    propagates so the caller maps it to ``policy-refused``.
    """
    policy = load_policy(config_dir, agent_id)
    if policy is None:
        return False
    daily = read_daily_spent(config_dir, agent_id)
    requirement = select_requirement(pr, policy, daily)
    return is_auto_payable(policy, requirement)


async def prepare_and_send_payment(
    client: RineClient,
    msg: DecryptedMessage,
    *,
    auto_pay: bool,
    force: bool,
    add_marker: bool,
    agent: str | None,
) -> PayOutcome:
    """Payer ladder: parse the quote → (auto-pay gate) → sign+reserve → reply in-thread.

    Shared by the ``rine_pay`` tool and gateway auto-pay. Returns a typed
    :class:`PayOutcome` for every EXPECTED refusal (never raises for one); genuine plane
    errors (network/crypto) propagate for the caller to surface. ``msg`` is the
    already-decrypted quote (the read stays the caller's job, matching TS ``fulfill``).
    """
    if msg.type != PAYMENT_REQUIRED:
        return PayOutcome(NOT_PAYMENT_REQUIRED, detail=f"message type is {msg.type}")
    if msg.decrypt_error is not None or msg.plaintext is None:
        return PayOutcome(NOT_PAYMENT_REQUIRED, detail="quote could not be decrypted")

    config_dir = client.config_dir
    agent_id = await client._resolve_agent_id(agent)
    if not wallet_key_exists(config_dir, agent_id):
        return PayOutcome(NO_WALLET, detail="no wallet key for this agent")
    try:
        pr = parse_x402_payload(msg.plaintext)
    except ValueError:
        return PayOutcome(NOT_PAYMENT_REQUIRED, detail="quote payload is not a JSON object")

    try:
        if auto_pay and not _auto_payable(config_dir, agent_id, pr):
            return PayOutcome(ABOVE_AUTO_PAY_THRESHOLD, detail="quote exceeds autoPayThreshold")
        prepared = prepare_payment(
            config_dir,
            agent_id,
            pr,
            quote=msg,
            message_id=str(msg.id),
            force=force,
            add_marker=add_marker,
        )
    except X402Error as err:
        return PayOutcome(_STATUS_FOR_CODE.get(err.code, POLICY_REFUSED), detail=err.message)

    reply = await client.reply(
        str(msg.id),
        prepared.message.payload,
        message_type=prepared.message.message_type,
        metadata=prepared.message.metadata,
        content_type=prepared.message.content_type,
        agent=agent_id,
    )
    return PayOutcome(
        PAYMENT_SUBMITTED,
        message_id=str(reply.id),
        conversation_id=str(reply.conversation_id),
    )


def auto_pay_enabled() -> bool:
    """Whether opt-in gateway auto-pay is enabled (``RINE_X402_AUTO_PAY``, default OFF).

    Auto-pay is opt-in config only, default OFF everywhere. When unset (or falsey)
    every ``payment_required`` frame wakes the agent instead (Mode T).
    """
    return os.getenv("RINE_X402_AUTO_PAY", "").strip().lower() in {"1", "true", "yes", "on"}


async def maybe_auto_pay(client: RineClient, msg: DecryptedMessage) -> bool:
    """Attempt an opt-in, sub-threshold gateway auto-pay of a quote (default OFF).

    Returns True only when the quote was actually paid (so the caller drains the frame).
    Any refusal — above threshold, no policy, no wallet, caps, a send failure — returns
    False so the frame falls through to a normal payment-aware wake and a human/LLM
    decides. Caps + deny-by-default + reserve-lock + permanent debit still bound it.
    """
    if not auto_pay_enabled() or msg.type != PAYMENT_REQUIRED:
        return False
    try:
        outcome = await prepare_and_send_payment(
            client, msg, auto_pay=True, force=False, add_marker=True, agent=None
        )
    except Exception:  # noqa: BLE001 — any plane error falls through to waking the agent
        logger.warning("rine wake: gateway auto-pay errored for %s; waking", msg.id, exc_info=True)
        return False
    if outcome.status == PAYMENT_SUBMITTED:
        logger.info("rine wake: gateway auto-paid message %s", msg.id)
        return True
    logger.info(
        "rine wake: gateway auto-pay declined %s (%s); waking the agent", msg.id, outcome.status
    )
    return False


__all__ = [
    "PayOutcome",
    "auto_pay_enabled",
    "maybe_auto_pay",
    "prepare_and_send_payment",
]
