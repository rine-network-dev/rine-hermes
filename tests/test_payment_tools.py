"""Behavioral tests for the x402 payment tools + gateway auto-pay (x402 v1.1).

Each tool is driven through its public async signature; assertions are on the returned
``tool_result``/``tool_error`` JSON string and on the observable HTTP (respx). Only the
rine HTTP boundary AND the external facilitator are mocked — the SDK crypto / wallet /
policy / journal are REAL (a real HPKE self-encrypt + a real EIP-3009 sign), so these
exercise the invariants (marker is {status,rail}-only, deny-by-default, permanent debit,
settle-first, settle only against a payee-issued quote) end to end.

The payer quote / payee payment are self-encrypted (from AGENT_ID to AGENT_ID) so the
tool can read + verify them and reply in-thread against the mocked host — the same idiom
the messaging suite uses. A fulfil additionally serves the quote the payment references
by id, since the payee reloads its OWN quote before it will settle anything.
"""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx
from rine._constants import EncryptionVersion, MessageType
from rine.x402 import read_daily_spent
from rine.x402.types import X402_QUOTE_REF_KEY

from hermes_rine._client import build_client
from hermes_rine._payer import maybe_auto_pay
from hermes_rine.tools.payments import _handle_fulfill, _handle_pay

from .conftest import (
    AGENT_ID,
    API_URL,
    MESSAGE_ID,
    OTHER_AGENT_ID,
    REPLY_ID,
    hpke_self_blob,
    hpke_self_encrypt,
    message_payload,
    mock_agents,
    mock_recipient_keys,
    mock_token,
    sent_message_payload,
)

# EURC on Base Sepolia (the known-asset the policy allows) + an arbitrary recipient.
EURC_SEPOLIA = "0x808456652fdb597867f38412077a9182bf77359f"
PAY_TO = "0x1111111111111111111111111111111111111111"
PAYER_ADDR = "0x2222222222222222222222222222222222222222"
FAC_URL = "https://fac.test"
MARKER_KEY = "rine.x402"
# The payee-issued quote a payment answers — its id rides in the SIGNED payload.
QUOTE_ID = "cccccccc-dddd-eeee-ffff-000000000003"


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK resolution chain at the test config dir + mock host (no boilerplate)."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


def _requirement(amount: str = "10000") -> dict:
    """One acceptable EURC/base-sepolia ``exact`` requirement (10000 = 0.01 EURC)."""
    return {
        "scheme": "exact",
        "network": "eip155:84532",
        "amount": amount,
        "asset": EURC_SEPOLIA,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 600,
        "extra": {"name": "EURC", "version": "2"},
    }


def _quote_payload(amount: str = "10000") -> dict:
    """A verbatim x402 ``PaymentRequired`` object (the quote)."""
    return {"x402Version": 2, "accepts": [_requirement(amount)], "extensions": {}}


def _payment_payload(*, quote_ref: str | None = QUOTE_ID) -> dict:
    """A verbatim x402 ``PaymentPayload`` (signed authorization) with ``accepted``.

    ``extensions[X402_QUOTE_REF_KEY]`` names the payee-issued quote this payment
    answers; the payee reloads THAT quote and reconciles against it before ever
    contacting the facilitator. ``quote_ref=None`` is a payer that stamped no ref.
    """
    return {
        "x402Version": 2,
        "scheme": "exact",
        "network": "base-sepolia",  # v1 slug — payee stores it verbatim
        "payload": {
            "authorization": {"from": PAYER_ADDR, "to": PAY_TO, "value": "10000"},
            "signature": "0xsig",
        },
        "accepted": _requirement(),
        "extensions": {X402_QUOTE_REF_KEY: quote_ref} if quote_ref is not None else {},
    }


def _save_policy(config_dir: str, *, threshold: str = "1.00") -> None:
    from rine.x402 import PaymentPolicy, save_policy

    save_policy(
        config_dir,
        AGENT_ID,
        PaymentPolicy(
            per_tx_cap="5.00",
            daily_cap="50.00",
            allowed_assets=["EURC"],
            allowed_networks=["eip155:84532"],
            auto_pay_threshold=threshold,
        ),
    )


def _create_wallet(config_dir: str) -> None:
    from rine.x402 import create_wallet

    create_wallet(config_dir, AGENT_ID)


def _mock_frame_and_reply(config_dir: str, payload: dict, msg_type: str):
    """Mock the GET (self-encrypted frame) + the in-thread reply POST; return the route."""
    mock_token()
    mock_agents()
    mock_recipient_keys(AGENT_ID)
    ct = hpke_self_encrypt(config_dir, payload)
    respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
        return_value=httpx.Response(
            200,
            json=message_payload(
                encryption_version=EncryptionVersion.HPKE_HYBRID_V2,
                encrypted_payload=ct,
                msg_type=msg_type,
            ),
        )
    )
    return respx.post(f"{API_URL}/messages/{MESSAGE_ID}/reply").mock(
        return_value=httpx.Response(200, json={**sent_message_payload(), "id": REPLY_ID})
    )


def _mock_referenced_quote(config_dir: str):
    """Mock the by-id GET of the payee-issued quote the payment references.

    The row is what the server hands its SENDER: ``direction="sent"`` plus the
    self-seal, so the payee reopens its own quote through the self-seal path (the
    ``encrypted_payload`` copy is the payer's and stays opaque here).
    """
    return respx.get(f"{API_URL}/messages/{QUOTE_ID}").mock(
        return_value=httpx.Response(
            200,
            json=message_payload(
                encryption_version=EncryptionVersion.HPKE_HYBRID_V2,
                encrypted_payload="ZHVtbXk=",  # the payer's copy — never opened here
                self_encrypted_payload=hpke_self_blob(config_dir, _quote_payload()),
                to_agent_id=OTHER_AGENT_ID,
                msg_id=QUOTE_ID,
                msg_type=MessageType.X402_PAYMENT_REQUIRED,
                direction="sent",
            ),
        )
    )


# =====================================================================
# rine_pay (payer)
# =====================================================================


class TestPay:
    @respx.mock
    async def test_pay_submits_and_marker_is_status_rail_only(self, config_dir):
        _create_wallet(config_dir)
        _save_policy(config_dir)
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )

        out = await _handle_pay({"message_id": MESSAGE_ID})

        data = json.loads(out)
        assert data["status"] == "payment-submitted"
        assert data["payment_message_id"] == REPLY_ID
        body = json.loads(reply.calls[0].request.content)
        assert body["type"] == MessageType.X402_PAYMENT
        # Marker is {status, rail} ONLY — never the amount/asset/address.
        assert body["metadata"] == {MARKER_KEY: {"status": "payment-submitted", "rail": "x402"}}
        assert "10000" not in json.dumps(body["metadata"])
        assert EURC_SEPOLIA not in json.dumps(body["metadata"])

    @respx.mock
    async def test_pay_when_quote_sender_unverifiable_returns_sender_unverifiable(
        self, config_dir
    ):
        _create_wallet(config_dir)
        _save_policy(config_dir)
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )
        respx.get(f"{API_URL}/agents/{AGENT_ID}/keys").mock(
            return_value=httpx.Response(503, json={"detail": "key service unavailable"})
        )

        out = await _handle_pay({"message_id": MESSAGE_ID})

        assert json.loads(out)["status"] == "sender-unverifiable"
        assert not reply.called
        assert read_daily_spent(config_dir, AGENT_ID) == 0

    @respx.mock
    async def test_pay_without_wallet_returns_no_wallet(self, config_dir):
        _save_policy(config_dir)  # policy but no wallet key
        _mock_frame_and_reply(config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED)

        out = await _handle_pay({"message_id": MESSAGE_ID})

        assert json.loads(out)["status"] == "no-wallet"

    @respx.mock
    async def test_pay_non_quote_type_returns_not_payment_required(self, config_dir):
        _create_wallet(config_dir)
        _save_policy(config_dir)
        _mock_frame_and_reply(config_dir, {"text": "hello"}, "rine.v1.task_request")

        out = await _handle_pay({"message_id": MESSAGE_ID})

        assert json.loads(out)["status"] == "not-payment-required"

    @respx.mock
    async def test_pay_without_policy_is_deny_by_default_policy_refused(self, config_dir):
        _create_wallet(config_dir)  # wallet but NO policy → deny-by-default
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )

        out = await _handle_pay({"message_id": MESSAGE_ID})

        assert json.loads(out)["status"] == "policy-refused"
        assert not reply.called  # nothing signed or sent

    @respx.mock
    async def test_pay_auto_pay_above_threshold_refuses_without_paying(self, config_dir):
        _create_wallet(config_dir)
        _save_policy(config_dir, threshold="0.001")  # 1000 micro < 10000 quote
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )

        out = await _handle_pay({"message_id": MESSAGE_ID, "auto_pay": True})

        assert json.loads(out)["status"] == "above-auto-pay-threshold"
        assert not reply.called  # the reserve/permanent-debit path never ran

    @respx.mock
    async def test_pay_repeat_same_id_returns_already_paid(self, config_dir):
        _create_wallet(config_dir)
        _save_policy(config_dir)
        _mock_frame_and_reply(config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED)

        first = await _handle_pay({"message_id": MESSAGE_ID})
        second = await _handle_pay({"message_id": MESSAGE_ID})

        assert json.loads(first)["status"] == "payment-submitted"
        assert json.loads(second)["status"] == "already-paid"

    @respx.mock
    async def test_pay_emit_marker_false_sends_empty_metadata(self, config_dir):
        _create_wallet(config_dir)
        _save_policy(config_dir)
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )

        out = await _handle_pay({"message_id": MESSAGE_ID, "emit_marker": False})

        assert json.loads(out)["status"] == "payment-submitted"
        assert json.loads(reply.calls[0].request.content)["metadata"] == {}


# =====================================================================
# rine_fulfill (payee — bind quote → verify → settle → receipt via the SDK orchestrator)
# =====================================================================


class TestFulfill:
    @respx.mock
    async def test_fulfill_settles_and_receipt_marker_completed(self, config_dir):
        reply = _mock_frame_and_reply(config_dir, _payment_payload(), MessageType.X402_PAYMENT)
        quote = _mock_referenced_quote(config_dir)
        verify = respx.post(f"{FAC_URL}/verify").mock(
            return_value=httpx.Response(200, json={"isValid": True})
        )
        settle = respx.post(f"{FAC_URL}/settle").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": True,
                    "transaction": "0xhash",
                    "network": "base-sepolia",
                    "payer": PAYER_ADDR,
                },
            )
        )

        out = await _handle_fulfill({"message_id": MESSAGE_ID, "facilitator_url": FAC_URL})

        data = json.loads(out)
        assert data["status"] == "receipt-sent"
        assert data["settled"] is True
        assert quote.called  # bound to the payee's own quote before any facilitator call
        assert verify.called and settle.called  # settle-first: verify then settle
        # What gets settled is the reconciled requirement off the payee's own quote.
        assert json.loads(settle.calls[0].request.content)["paymentRequirements"] == _requirement()
        body = json.loads(reply.calls[0].request.content)
        assert body["type"] == MessageType.X402_RECEIPT
        assert body["metadata"] == {MARKER_KEY: {"status": "payment-completed", "rail": "x402"}}

    @respx.mock
    async def test_fulfill_failed_verify_skips_settle_and_reports_unsettled(self, config_dir):
        reply = _mock_frame_and_reply(config_dir, _payment_payload(), MessageType.X402_PAYMENT)
        _mock_referenced_quote(config_dir)
        respx.post(f"{FAC_URL}/verify").mock(
            return_value=httpx.Response(
                200, json={"isValid": False, "invalidReason": "insufficient_funds"}
            )
        )
        settle = respx.post(f"{FAC_URL}/settle").mock(return_value=httpx.Response(200, json={}))

        out = await _handle_fulfill({"message_id": MESSAGE_ID, "facilitator_url": FAC_URL})

        data = json.loads(out)
        assert data["settled"] is False
        assert data["reason"] == "insufficient_funds"
        assert not settle.called  # settle SKIPPED on a failed verification
        body = json.loads(reply.calls[0].request.content)
        assert body["metadata"] == {MARKER_KEY: {"status": "payment-failed", "rail": "x402"}}

    @respx.mock
    async def test_fulfill_without_quote_ref_refuses_before_the_facilitator(self, config_dir):
        reply = _mock_frame_and_reply(
            config_dir, _payment_payload(quote_ref=None), MessageType.X402_PAYMENT
        )
        quote = _mock_referenced_quote(config_dir)
        verify = respx.post(f"{FAC_URL}/verify")

        out = await _handle_fulfill({"message_id": MESSAGE_ID, "facilitator_url": FAC_URL})

        data = json.loads(out)
        assert data["settled"] is False
        assert data["reason"] == "no-quote"  # unbindable payment, fail closed
        assert not quote.called and not verify.called  # no fetch, no facilitator, no funds
        body = json.loads(reply.calls[0].request.content)
        assert body["metadata"] == {MARKER_KEY: {"status": "payment-failed", "rail": "x402"}}

    @respx.mock
    async def test_fulfill_when_payment_sender_unverifiable_sends_nothing(self, config_dir):
        reply = _mock_frame_and_reply(config_dir, _payment_payload(), MessageType.X402_PAYMENT)
        respx.get(f"{API_URL}/agents/{AGENT_ID}/keys").mock(
            return_value=httpx.Response(503, json={"detail": "key service unavailable"})
        )
        verify = respx.post(f"{FAC_URL}/verify")

        out = await _handle_fulfill({"message_id": MESSAGE_ID, "facilitator_url": FAC_URL})

        data = json.loads(out)
        assert data["status"] == "sender-unverifiable"
        assert data["settled"] is False
        assert not verify.called
        assert not reply.called

    @respx.mock
    async def test_fulfill_wrong_type_returns_tool_error(self, config_dir):
        _mock_frame_and_reply(config_dir, {"text": "hello"}, "rine.v1.task_request")

        out = await _handle_fulfill({"message_id": MESSAGE_ID, "facilitator_url": FAC_URL})

        data = json.loads(out)
        assert "error" in data
        assert "cannot fulfill" in data["error"]


# =====================================================================
# Gateway auto-pay (opt-in, default OFF)
# =====================================================================


class TestGatewayAutoPay:
    async def test_auto_pay_disabled_by_default_never_touches_client(self, make_message):
        # Default OFF: maybe_auto_pay returns False WITHOUT resolving/paying. A client that
        # raises if used proves the short-circuit (default OFF).
        msg = make_message(
            message_id=UUID(MESSAGE_ID),
            msg_type="rine.v1.x402_payment_required",
            plaintext=json.dumps(_quote_payload()),
        )

        class _Boom:
            config_dir = "unused"

            async def _resolve_agent_id(self, agent=None):
                raise AssertionError("auto-pay must not resolve when disabled")

        assert await maybe_auto_pay(_Boom(), msg) is False

    @respx.mock
    async def test_auto_pay_enabled_pays_sub_threshold_quote(
        self, config_dir, monkeypatch, make_message
    ):
        monkeypatch.setenv("RINE_X402_AUTO_PAY", "1")
        _create_wallet(config_dir)
        _save_policy(config_dir, threshold="1.00")  # 10000 micro <= 1.00 → auto-payable
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )
        msg = make_message(
            message_id=UUID(MESSAGE_ID),
            msg_type="rine.v1.x402_payment_required",
            plaintext=json.dumps(_quote_payload()),
        )

        async with build_client() as client:
            paid = await maybe_auto_pay(client, msg)

        assert paid is True
        assert reply.called  # the sub-threshold quote was settled without a turn

    @respx.mock
    async def test_auto_pay_when_quote_sender_not_verified_declines_and_wakes(
        self, config_dir, monkeypatch, make_message
    ):
        monkeypatch.setenv("RINE_X402_AUTO_PAY", "1")
        _create_wallet(config_dir)
        _save_policy(config_dir, threshold="1.00")
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )
        msg = make_message(
            message_id=UUID(MESSAGE_ID),
            msg_type="rine.v1.x402_payment_required",
            plaintext=json.dumps(_quote_payload()),
            verification_status="sender-mismatch",
            verified=False,
        )

        async with build_client() as client:
            paid = await maybe_auto_pay(client, msg)

        assert paid is False
        assert not reply.called
        assert read_daily_spent(config_dir, AGENT_ID) == 0

    @respx.mock
    async def test_auto_pay_above_threshold_declines_and_wakes(
        self, config_dir, monkeypatch, make_message
    ):
        monkeypatch.setenv("RINE_X402_AUTO_PAY", "1")
        _create_wallet(config_dir)
        _save_policy(config_dir, threshold="0.001")  # 1000 micro < 10000 → above threshold
        reply = _mock_frame_and_reply(
            config_dir, _quote_payload(), MessageType.X402_PAYMENT_REQUIRED
        )
        msg = make_message(
            message_id=UUID(MESSAGE_ID),
            msg_type="rine.v1.x402_payment_required",
            plaintext=json.dumps(_quote_payload()),
        )

        async with build_client() as client:
            paid = await maybe_auto_pay(client, msg)

        assert paid is False  # falls through to a normal payment-aware wake
        assert not reply.called
