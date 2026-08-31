"""x402 payment handlers: ``rine_pay`` (payer) + ``rine_fulfill`` (payee).

Thin adapters over the rine SDK — never reimplementing signing / policy / journal /
verify / settle:

- ``rine_pay`` delegates the whole payer ladder to
  :func:`hermes_rine._x402.prepare_and_send_payment` (the same composition the opt-in
  gateway auto-pay uses), returning the ``rine_pay`` typed-status vocabulary verbatim.
- ``rine_fulfill`` delegates verify → settle → receipt to the SDK's ONE payee
  orchestrator :func:`rine.x402.fulfill`; a failed verification is a business
  outcome (a ``success=False`` receipt threaded back), not a tool error.

Both return a ``tool_result`` / ``tool_error`` JSON string like every other rine tool —
never a raw traceback, never key material, never ciphertext. Facilitator verify/settle is
plain external HTTP through the payee-configured facilitator (pure-metadata-relay posture).
"""

from __future__ import annotations

import os
from typing import Any

from rine.x402 import FEATURED_FACILITATOR, FacilitatorClient, X402Error, fulfill
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, acting_agent, build_client, settle_actor
from hermes_rine._payer import prepare_and_send_payment


async def _handle_pay(args: dict[str, Any], **_: Any) -> str:
    """Pay a received x402 quote (``rine_pay``): sign + reserve + reply in-thread.

    Reads the ``payment_required`` quote by id, then runs the shared payer ladder. The
    reservation is a permanent debit made as the last local act before transmit; a repeat
    pay of the same id returns ``already-paid`` unless ``allow_repay`` is set. With
    ``auto_pay`` the tool pays only at/below the policy's autoPayThreshold, else returns
    ``above-auto-pay-threshold`` without paying.
    """
    message_id = args["message_id"]
    # The quote is READ as the acting agent too. This tool has always taken an
    # ``agent`` and handed it to the payer ladder, but the read in front of it
    # named nobody — so in an org with two agents the call was refused before
    # the ladder ran, and the argument it declares never got a chance to work.
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            msg = await client.read(message_id, agent=acting)
            outcome = await prepare_and_send_payment(
                client,
                msg,
                auto_pay=bool(args.get("auto_pay", False)),
                force=bool(args.get("allow_repay", False)),
                add_marker=bool(args.get("emit_marker", True)),
                agent=acting,
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    fields: dict[str, Any] = {**note, "status": outcome.status}
    if outcome.message_id is not None:
        fields["payment_message_id"] = outcome.message_id
    if outcome.conversation_id is not None:
        fields["conversation_id"] = outcome.conversation_id
    if outcome.detail is not None:
        fields["reason"] = outcome.detail
    return tool_result(**fields)


def _resolve_facilitator(args: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Resolve the payee's facilitator (preset name, base URL, api key) for a fulfill.

    Precedence: explicit tool arg → env → the SDK default preset. The api key comes ONLY
    from ``RINE_X402_FACILITATOR_API_KEY`` (a secret — never a model-supplied tool arg).
    """
    preset = args.get("facilitator") or os.getenv("RINE_X402_FACILITATOR") or FEATURED_FACILITATOR
    base_url = args.get("facilitator_url") or os.getenv("RINE_X402_FACILITATOR_URL") or None
    api_key = os.getenv("RINE_X402_FACILITATOR_API_KEY") or None
    return preset, base_url, api_key


async def _handle_fulfill(args: dict[str, Any], **_: Any) -> str:
    """Settle a received x402 payment (``rine_fulfill``): verify → settle → receipt.

    Reads the signed ``x402_payment`` by id and hands it to the SDK payee orchestrator,
    which verifies and (on a valid authorization) settles through the configured
    facilitator, then threads the receipt back in the payment's thread. A failed
    verification is NOT an error — it threads a ``success=False`` receipt so the payer's
    thread reaches a terminal ``payment-failed`` (``settled: false`` in the result).
    """
    message_id = args["message_id"]
    preset, base_url, api_key = _resolve_facilitator(args)
    # Read as the acting agent, for the reason ``_handle_pay`` gives.
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            payment = await client.read(message_id, agent=acting)
            async with FacilitatorClient(preset, base_url=base_url, api_key=api_key) as fac:
                result = await fulfill(
                    client,
                    payment,
                    facilitator=fac,
                    agent=acting,
                    add_marker=bool(args.get("emit_marker", True)),
                )
    except X402Error as err:
        return tool_error(f"cannot fulfill message {message_id}: {err.message}")
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    settlement = result.settlement
    settled = bool(settlement.get("success")) if settlement else False
    fields: dict[str, Any] = {
        **note,
        "status": "receipt-sent",
        "settled": settled,
        "receipt_id": str(result.receipt.id),
        "conversation_id": str(result.receipt.conversation_id),
        "receipt_marker": result.receipt.metadata,
    }
    if not settled:
        reason = result.verification.get("invalidReason")
        if not reason and settlement:
            reason = settlement.get("errorReason")
        fields["reason"] = str(reason or "verification_failed")
    return tool_result(**fields)


__all__ = ["_handle_fulfill", "_handle_pay"]
