"""Behavioral tests for the messaging handlers (SPEC §5 "Tools").

Each handler is driven through its public async signature (``await handler(args)``);
assertions are on the returned ``tool_result``/``tool_error`` JSON **string** and on
observable HTTP. Only the rine HTTP boundary is mocked (respx); the SDK crypto /
config / HTTP is real, so a real HPKE self-encrypt round-trip exercises the
ciphertext-never-leaks invariant.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx
from rine._constants import EncryptionVersion, MessageType

from hermes_rine.tools.messaging import (
    _handle_check_inbox,
    _handle_read,
    _handle_reply,
    _handle_send,
    _handle_send_and_wait,
)

from .conftest import (
    AGENT_ID,
    API_URL,
    CONVERSATION_ID,
    GROUP_HANDLE,
    MESSAGE_ID,
    OTHER_AGENT_ID,
    OTHER_HANDLE,
    REPLY_ID,
    hpke_self_encrypt,
    message_payload,
    mock_agents,
    mock_recipient_keys,
    mock_self_keys,
    mock_token,
    mock_webfinger,
    self_keys_json,
    sent_message_payload,
)


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK resolution chain at the test config dir + mock API URL.

    The handlers build their client via ``build_client()`` with no overrides, so the
    SDK resolves ``config_dir``/``api_url`` from env. This wires every messaging test
    to the on-disk creds/keys + the respx-mocked host without per-test boilerplate.
    """
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


# =====================================================================
# rine_send
# =====================================================================


class TestSend:
    @respx.mock
    async def test_send_to_agent_when_recipient_exists_returns_message_id(
        self, config_dir, other_keys
    ):
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        out = await _handle_send({"to": OTHER_HANDLE, "body": "hello there"})

        assert route.called
        data = json.loads(out)
        assert data["sent"] is True
        assert data["message_id"] == MESSAGE_ID
        assert data["conversation_id"] == CONVERSATION_ID

    @respx.mock
    async def test_send_encrypts_body_on_the_wire_not_plaintext(self, config_dir, other_keys):
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        await _handle_send({"to": OTHER_HANDLE, "body": "hello there"})

        body = json.loads(route.calls[0].request.content)
        assert body["encryption_version"] == EncryptionVersion.HPKE_V1
        assert "encrypted_payload" in body
        assert "hello there" not in route.calls[0].request.content.decode()

    @respx.mock
    async def test_send_forwards_idempotency_key_and_message_type(self, config_dir, other_keys):
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        await _handle_send(
            {
                "to": OTHER_HANDLE,
                "body": "hi",
                "idempotency_key": "k1",
                "message_type": "rine.v1.custom",
            }
        )

        request = route.calls[0].request
        assert request.headers.get("Idempotency-Key") == "k1"
        assert json.loads(request.content)["type"] == "rine.v1.custom"

    @respx.mock
    async def test_send_error_returns_tool_error_string_not_raise(self, config_dir):
        mock_token()
        mock_agents()
        # A bare name (no @, not a UUID) trips the SDK NotFoundError before any send.
        out = await _handle_send({"to": "nonexistent", "body": "hi"})

        data = json.loads(out)
        assert "error" in data
        assert "Not found" in data["error"]
        assert "rine_discover" in data["error"]


# =====================================================================
# rine_send_and_wait
# =====================================================================


class TestSendAndWait:
    @respx.mock
    async def test_send_and_wait_reply_returns_plaintext_not_ciphertext(
        self, config_dir, other_keys
    ):
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        reply_ct = hpke_self_encrypt(config_dir, {"text": "ack: done"})
        respx.get(f"{API_URL}/agents/{AGENT_ID}/keys").mock(
            return_value=httpx.Response(200, json=self_keys_json(config_dir))
        )
        respx.post(f"{API_URL}/messages/sync").mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": sent_message_payload(),
                    "reply": message_payload(
                        encryption_version=EncryptionVersion.HPKE_V1,
                        encrypted_payload=reply_ct,
                        msg_id=REPLY_ID,
                        from_agent_id=AGENT_ID,
                        msg_type=MessageType.TASK_RESPONSE,
                    ),
                    "conversation_id": CONVERSATION_ID,
                    "status": "completed",
                },
            )
        )

        out = await _handle_send_and_wait({"to": OTHER_HANDLE, "body": "do it", "wait_seconds": 5})

        data = json.loads(out)
        assert data["reply"] == "ack: done"
        assert data["verification"] == "signature verified"
        assert reply_ct not in out

    @respx.mock
    async def test_send_and_wait_no_reply_returns_timeout_note(self, config_dir, other_keys):
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        respx.post(f"{API_URL}/messages/sync").mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": sent_message_payload(),
                    "reply": None,
                    "conversation_id": CONVERSATION_ID,
                    "status": "timeout",
                },
            )
        )

        out = await _handle_send_and_wait({"to": OTHER_HANDLE, "body": "do it", "wait_seconds": 5})

        data = json.loads(out)
        assert data["reply"] is None
        assert "no reply within 5s" in data["note"]

    async def test_send_and_wait_to_group_returns_one_to_one_only_string(self, config_dir):
        """A group handle trips the SDK's typed UnsupportedTargetError (pre-request)."""
        out = await _handle_send_and_wait({"to": GROUP_HANDLE, "body": "nope"})

        data = json.loads(out)
        assert data["error"] == "send_and_wait is 1:1 only; use rine_send for groups."

    async def test_send_and_wait_to_group_never_raises(self, config_dir):
        out = await _handle_send_and_wait({"to": "#team@acme.rine.network", "body": "nope"})
        assert isinstance(out, str)


# =====================================================================
# rine_check_inbox
# =====================================================================


class TestCheckInbox:
    @respx.mock
    async def test_check_inbox_returns_plaintext_never_ciphertext(self, config_dir):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        ciphertext = hpke_self_encrypt(config_dir, {"text": "inbox hello"})
        inbox_route = respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        message_payload(
                            encryption_version=EncryptionVersion.HPKE_V1,
                            encrypted_payload=ciphertext,
                        )
                    ],
                    "total": 1,
                },
            )
        )
        respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(200, json={"marked": 1})
        )

        out = await _handle_check_inbox({})

        assert "inbox hello" in out
        assert "encrypted_payload" not in out
        assert ciphertext not in out
        # status=new must be forwarded (the tool floor reads only NEW mail).
        assert b"status=new" in inbox_route.calls[0].request.url.query

    @respx.mock
    async def test_check_inbox_marks_only_decryptable_ids_delivered(self, config_dir):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        ciphertext = hpke_self_encrypt(config_dir, {"text": "ack me"})
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        message_payload(
                            encryption_version=EncryptionVersion.HPKE_V1,
                            encrypted_payload=ciphertext,
                        )
                    ],
                    "total": 1,
                },
            )
        )
        ack = respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(200, json={"marked": 1})
        )

        await _handle_check_inbox({})

        assert ack.called
        assert json.loads(ack.calls[0].request.content) == {"message_ids": [MESSAGE_ID]}

    @respx.mock
    async def test_check_inbox_does_not_ack_undecryptable_message(self, config_dir):
        """An MLS message renders [unreadable] + is NOT marked delivered."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        message_payload(
                            encryption_version=EncryptionVersion.MLS_V1,
                            encrypted_payload="Q0lQSEVSVEVYVA==",
                        )
                    ],
                    "total": 1,
                },
            )
        )
        ack = respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(200, json={"marked": 0})
        )

        out = await _handle_check_inbox({})

        assert "[unreadable]" in out
        assert "Q0lQSEVSVEVYVA==" not in out
        assert not ack.called

    @respx.mock
    async def test_check_inbox_ack_failure_warns_and_notes_without_losing_read(
        self, config_dir, caplog
    ):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        ciphertext = hpke_self_encrypt(config_dir, {"text": "still here"})
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        message_payload(
                            encryption_version=EncryptionVersion.HPKE_V1,
                            encrypted_payload=ciphertext,
                        )
                    ],
                    "total": 1,
                },
            )
        )
        respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(500, json={"detail": "ack down"})
        )

        with caplog.at_level(logging.WARNING, logger="hermes_rine.messaging"):
            out = await _handle_check_inbox({})

        assert "still here" in out
        assert "could not mark delivered" in out
        data = json.loads(out)
        assert data["ack_failed"] is True
        assert any("mark_delivered failed" in r.message for r in caplog.records)

    @respx.mock
    async def test_check_inbox_empty_returns_no_messages(self, config_dir):
        mock_token()
        mock_agents()
        ack = respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(200, json={"marked": 0})
        )
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_check_inbox({})

        assert "No new messages." in out
        assert not ack.called


# =====================================================================
# rine_read
# =====================================================================


class TestRead:
    @respx.mock
    async def test_read_hpke_returns_plaintext_and_verified(self, config_dir):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        ciphertext = hpke_self_encrypt(config_dir, {"text": "read me"})
        respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(
                200,
                json=message_payload(
                    encryption_version=EncryptionVersion.HPKE_V1,
                    encrypted_payload=ciphertext,
                ),
            )
        )

        out = await _handle_read({"message_id": MESSAGE_ID})

        assert "read me" in out
        assert "signature verified" in out
        assert "encrypted_payload" not in out
        assert ciphertext not in out

    @respx.mock
    async def test_read_mls_surfaces_unreadable_not_ciphertext(self, config_dir):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(
                200,
                json=message_payload(
                    encryption_version=EncryptionVersion.MLS_V1,
                    encrypted_payload="Q0lQSEVSVEVYVA==",
                ),
            )
        )

        out = await _handle_read({"message_id": MESSAGE_ID})

        assert "[unreadable]" in out
        assert "MLS" in out
        assert "Q0lQSEVSVEVYVA==" not in out

    @respx.mock
    async def test_read_missing_returns_not_found_string(self, config_dir):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(404, json={"detail": "message not found"})
        )

        out = await _handle_read({"message_id": MESSAGE_ID})

        data = json.loads(out)
        assert "Not found" in data["error"]
        assert "message not found" in data["error"]


# =====================================================================
# rine_reply
# =====================================================================


class TestReply:
    @respx.mock
    async def test_reply_returns_reply_id_never_ciphertext(self, config_dir, other_keys):
        mock_token()
        mock_agents()
        # reply() GETs the original to find the sender, then fetches their key.
        respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(
                200,
                json=message_payload(
                    encryption_version=EncryptionVersion.HPKE_V1,
                    encrypted_payload="ZHVtbXk=",
                    from_agent_id=OTHER_AGENT_ID,
                    to_agent_id=AGENT_ID,
                ),
            )
        )
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages/{MESSAGE_ID}/reply").mock(
            return_value=httpx.Response(
                200,
                json={**sent_message_payload(), "id": REPLY_ID, "encrypted_payload": "U0VDUkVU"},
            )
        )

        out = await _handle_reply({"message_id": MESSAGE_ID, "body": "on it"})

        assert route.called
        data = json.loads(out)
        assert data["replied_to"] == MESSAGE_ID
        assert data["reply_id"] == REPLY_ID
        assert "U0VDUkVU" not in out
        assert "encrypted_payload" not in out
