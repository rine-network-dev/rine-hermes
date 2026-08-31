"""Behavioral tests for the messaging handlers.

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
from rine import TRANSCRIPT_REFERENCE_REQUIRED, not_a_conversation_uuid
from rine._constants import EncryptionVersion, MessageType

from hermes_rine._schemas import SCHEMAS
from hermes_rine.tools.messaging import (
    _handle_inbox,
    _handle_read,
    _handle_reply,
    _handle_send,
    _handle_send_and_wait,
    _handle_thread,
)

from .conftest import (
    AGENT_ID,
    API_URL,
    CONVERSATION_ID,
    GROUP_HANDLE,
    GROUP_ID,
    MESSAGE_ID,
    OTHER_AGENT_ID,
    OTHER_HANDLE,
    REPLY_ID,
    group_payload,
    hpke_peer_encrypt,
    hpke_self_encrypt,
    message_payload,
    mock_agents,
    mock_group_row,
    mock_recipient_keys,
    mock_self_keys,
    mock_token,
    mock_webfinger,
    registered_tool_id,
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
    async def test_send_when_the_spelling_differs_from_the_resolved_handle_echoes_both(
        self, config_dir, other_keys
    ):
        """In JSON, ``to`` keeps what was written, ``resolved_to`` says where it landed.

        The caller's own spelling is what a model can correlate its own turn
        against, and it is also the value a host parses off this key — so the
        resolved handle arrives beside it rather than in place of it.
        """
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(
                200, json=sent_message_payload() | {"recipient_handle": OTHER_HANDLE}
            )
        )

        out = await _handle_send({"to": OTHER_HANDLE.upper(), "body": "hello there"})

        data = json.loads(out)
        assert data["to"] == OTHER_HANDLE.upper()
        assert data["resolved_to"] == OTHER_HANDLE

    @respx.mock
    async def test_send_when_the_spelling_is_the_resolved_handle_says_it_once(
        self, config_dir, other_keys
    ):
        """The no-arrow arm: a key repeating ``to`` is a string the model must re-read."""
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(
                200, json=sent_message_payload() | {"recipient_handle": OTHER_HANDLE}
            )
        )

        out = await _handle_send({"to": OTHER_HANDLE, "body": "hello there"})

        data = json.loads(out)
        assert data["to"] == OTHER_HANDLE
        assert "resolved_to" not in data

    @respx.mock
    async def test_send_when_the_server_returned_no_handle_omits_the_key_rather_than_nulling_it(
        self, config_dir, other_keys
    ):
        """``→ None`` is banned; in JSON that is an absent key, not ``resolved_to: null``.

        A UUID-addressed 1:1 is the reachable case — nothing resolved to a
        handle, so there is no second spelling to name.
        """
        mock_token()
        mock_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        out = await _handle_send({"to": OTHER_HANDLE, "body": "hello there"})

        data = json.loads(out)
        assert "resolved_to" not in data
        assert data["sent"] is True

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
        """A bare name is a group reference, and an unresolvable one still refuses cleanly.

        It cannot be an agent identifier — the agent namespace declines anything
        without an ``@`` before any request goes out — so the SDK asks this org's
        groups instead, and the refusal names both spellings rather than sending
        a group question to the agent directory.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        group_payload(
                            name="logistics",
                            handle="#logistics@acme.rine.network",
                            group_id="cccccccc-dddd-eeee-ffff-000000000000",
                        )
                    ]
                },
            )
        )

        out = await _handle_send({"to": "nonexistent", "body": "hi"})

        data = json.loads(out)
        assert "error" in data
        assert (
            "Not found: 'nonexistent' names neither an agent nor a group you can reach."
            in data["error"]
        )
        # 🔴 The lead stopped naming the GROUP namespace, and the
        # remedy must not follow it: this refusal still ends on the group half of
        # the directory. If the predicate in ``_client._not_found_message`` ever
        # narrows back to ``startswith(_GROUP_NOT_FOUND_LEAD)``, this line is the
        # only thing between here and every bare-name miss being pointed at
        # ``rine_discover``, which cannot resolve a group handle.
        assert (
            data["error"]
            .rstrip()
            .endswith("Try rine_discover_groups to search the public directory.")
        )
        # Both spellings, as worked values. The template this used to show
        # (``#group@org``) is gone because a model reading a refusal that names
        # the shape of an answer fills the shape in rather than supplying a real
        # one — so the assertion is that the sentence carries a group handle
        # somebody could actually send to, and says what the bare name was read
        # as. That half used to say a bare name was neither, which stopped being
        # true the day bare names resolved.
        assert "#logistics@acme.rine.network" in data["error"]
        assert "A bare name is read as a group this org is seated in" in data["error"]
        assert "rine_discover_groups" in data["error"]

    @respx.mock
    async def test_send_error_says_sent_false_rather_than_omitting_it(self, config_dir):
        """A refusal NEGATES the key the success asserts instead of leaving it out.

        The model reads the tool result and nothing else — the host's own failure
        classification never enters its context — so a refusal that merely omits
        ``sent`` leaves "did it send?" unanswered. The twin of this class's first
        test, which pins the ``sent is True`` half the key gets its meaning from.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))

        out = await _handle_send({"to": "nonexistent", "body": "hi"})

        assert json.loads(out)["sent"] is False


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
        assert data["error"] == (
            f"{registered_tool_id(_handle_send_and_wait)} is 1:1 only;"
            f" use {registered_tool_id(_handle_send)} for groups."
        )

    async def test_send_and_wait_to_group_never_raises(self, config_dir):
        out = await _handle_send_and_wait({"to": "#team@acme.rine.network", "body": "nope"})
        assert isinstance(out, str)


# =====================================================================
# rine_inbox
# =====================================================================


class TestInbox:
    @respx.mock
    async def test_inbox_returns_plaintext_never_ciphertext(self, config_dir):
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

        out = await _handle_inbox({})

        assert "inbox hello" in out
        assert "encrypted_payload" not in out
        assert ciphertext not in out
        # status=new must be forwarded (the tool floor reads only NEW mail).
        assert b"status=new" in inbox_route.calls[0].request.url.query

    @respx.mock
    async def test_inbox_marks_only_decryptable_ids_delivered(self, config_dir):
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

        await _handle_inbox({})

        assert ack.called
        assert json.loads(ack.calls[0].request.content) == {"message_ids": [MESSAGE_ID]}

    @respx.mock
    async def test_inbox_does_not_ack_undecryptable_message(self, config_dir):
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

        out = await _handle_inbox({})

        assert "[unreadable]" in out
        assert "Q0lQSEVSVEVYVA==" not in out
        assert not ack.called

    @respx.mock
    async def test_inbox_ack_failure_warns_and_notes_without_losing_read(self, config_dir, caplog):
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
            out = await _handle_inbox({})

        assert "still here" in out
        assert "could not mark delivered" in out
        data = json.loads(out)
        assert data["ack_failed"] is True
        assert any("mark_delivered failed" in r.message for r in caplog.records)

    @respx.mock
    async def test_inbox_empty_returns_no_messages(self, config_dir):
        mock_token()
        mock_agents()
        ack = respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(200, json={"marked": 0})
        )
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_inbox({})

        assert "No new messages." in out
        assert not ack.called

    @respx.mock
    async def test_inbox_at_a_named_status_forwards_that_status(self, config_dir):
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_inbox({"status": "read"})

        assert b"status=read" in route.calls[0].request.url.query
        # The empty-state names what was asked for; "No new messages." here would
        # answer a question nobody put.
        assert "No read messages." in out

    @respx.mock
    async def test_inbox_status_all_sends_no_status_filter_at_all(self, config_dir):
        """``all`` is a tool-layer value, not a wire one.

        Neither the server nor the SDK knows a fourth status — the way to ask for
        every status is to send none — so a pass-through would be a 422.
        """
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_inbox({"status": "all"})

        assert b"status" not in route.calls[0].request.url.query
        assert "No messages." in out

    @respx.mock
    async def test_inbox_at_a_non_new_status_marks_nothing_delivered(self, config_dir):
        """Only the ``new`` path acks. A look back is a read, not a state change."""
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        ciphertext = hpke_self_encrypt(config_dir, {"text": "already handed over"})
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

        out = await _handle_inbox({"status": "delivered"})

        assert "already handed over" in out
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
    async def test_read_malformed_mls_surfaces_structured_error_not_ciphertext(self, config_dir):
        """A malformed mls-v1 message reports the failure in the body, never ciphertext.

        An mls-v1 message that names no group has no MLS state to open it with. The SDK's
        ``read()`` contract is that it never raises for this — it degrades, carrying the
        reason on ``decrypt_error`` — so the handler returns a normal ``tool_result``
        whose body is ``[unreadable] <reason>``, not a ``tool_error``. This test asserted
        ``data["error"]`` and died on a ``KeyError`` for that reason; the premise was
        stale, not the code. What must hold either way is asserted here: the reason is
        named, no verification is claimed for a message that was never opened, and the
        ciphertext never reaches the agent.
        """
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

        body = json.loads(out)["message"]
        assert "[unreadable]" in body
        assert "names no group" in body
        assert "malformed message, not a missing key" in body
        assert "signature verified" not in body
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
# rine_thread
# =====================================================================


class TestThread:
    @respx.mock
    async def test_thread_renders_role_tagged_transcript(self, config_dir, other_keys):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        mock_recipient_keys(config_dir)
        received_ct = hpke_peer_encrypt(config_dir, {"text": "peer turn one"})
        row = message_payload(
            encryption_version=EncryptionVersion.HPKE_V1,
            encrypted_payload=received_ct,
            from_agent_id=OTHER_AGENT_ID,
            to_agent_id=AGENT_ID,
        )
        row["direction"] = "received"
        row["sender_handle"] = OTHER_HANDLE
        respx.get(f"{API_URL}/conversations/{CONVERSATION_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [row], "truncated": False})
        )

        out = await _handle_thread({"conversation_id": CONVERSATION_ID})

        data = json.loads(out)
        assert f"[received] {OTHER_HANDLE}: peer turn one" in data["thread"]
        assert received_ct not in out

    @respx.mock
    async def test_thread_renders_sent_received_and_unavailable_rows(self, config_dir, other_keys):
        """Both-sided transcript renders all row kinds.

        ``[sent] you``, ``[received]``, and an ``[unavailable]`` passthrough all
        render via ``_render_thread_line``.
        """
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        mock_recipient_keys(config_dir)
        sent_ct = hpke_self_encrypt(config_dir, {"text": "my turn one"})
        received_ct = hpke_peer_encrypt(config_dir, {"text": "peer turn one"})

        sent_row = message_payload(
            encryption_version=EncryptionVersion.HPKE_V1,
            encrypted_payload="ZHVtbXk=",  # peer-sealed copy the sender cannot open
            from_agent_id=AGENT_ID,
            to_agent_id=OTHER_AGENT_ID,
        )
        sent_row["direction"] = "sent"
        sent_row["self_encrypted_payload"] = sent_ct

        received_row = message_payload(
            encryption_version=EncryptionVersion.HPKE_V1,
            encrypted_payload=received_ct,
            from_agent_id=OTHER_AGENT_ID,
            to_agent_id=AGENT_ID,
        )
        received_row["direction"] = "received"
        received_row["sender_handle"] = OTHER_HANDLE

        # A sent row with no self-blob → the SDK degrades it to ``[unavailable]``.
        unavailable_row = message_payload(
            encryption_version=EncryptionVersion.HPKE_V1,
            encrypted_payload="ZHVtbXk=",
            from_agent_id=AGENT_ID,
            to_agent_id=OTHER_AGENT_ID,
        )
        unavailable_row["direction"] = "sent"

        respx.get(f"{API_URL}/conversations/{CONVERSATION_ID}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "messages": [sent_row, received_row, unavailable_row],
                    "truncated": False,
                },
            )
        )

        out = await _handle_thread({"conversation_id": CONVERSATION_ID})

        data = json.loads(out)
        assert "[sent] you: my turn one" in data["thread"]
        assert f"[received] {OTHER_HANDLE}: peer turn one" in data["thread"]
        assert "[sent] you: [unavailable]" in data["thread"]
        assert sent_ct not in out  # ciphertext never surfaces
        assert received_ct not in out

    @respx.mock
    async def test_thread_empty_conversation(self, config_dir):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/conversations/{CONVERSATION_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [], "truncated": False})
        )

        out = await _handle_thread({"conversation_id": CONVERSATION_ID})

        data = json.loads(out)
        assert data["thread"] == "No messages in this conversation."


class TestThreadByGroup:
    """The transcript verb takes the name of the thing it reads.

    Every other group tool on this surface takes ``_GROUP_REF``; this one used
    to demand a conversation UUID, an id of a different entity whose only source
    was a column in another tool's output. These tests pin the group spelling
    reaching the group's own route, the two "exactly one" refusals, and that a
    conversation reference the wire cannot read as a UUID is refused before any
    request is made.
    """

    @respx.mock
    async def test_thread_by_group_handle_reads_the_groups_own_route(self, config_dir, other_keys):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        mock_recipient_keys(config_dir)
        received_ct = hpke_peer_encrypt(config_dir, {"text": "the delivery is booked"})
        row = message_payload(
            encryption_version=EncryptionVersion.HPKE_V1,
            encrypted_payload=received_ct,
            from_agent_id=OTHER_AGENT_ID,
            to_agent_id=AGENT_ID,
        )
        row["direction"] = "received"
        row["sender_handle"] = OTHER_HANDLE
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        route = respx.get(f"{API_URL}/groups/{GROUP_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [row], "truncated": False})
        )

        out = await _handle_thread({"group": GROUP_HANDLE})

        assert route.called
        data = json.loads(out)
        assert f"[received] {OTHER_HANDLE}: the delivery is booked" in data["thread"]

    @respx.mock
    async def test_thread_by_group_uuid_reads_the_group_without_resolving_it(self):
        """A UUID is already the reference the route takes: no ``GET /groups``."""
        mock_token()
        mock_agents()
        mock_group_row()
        listing = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        route = respx.get(f"{API_URL}/groups/{GROUP_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [], "truncated": False})
        )

        await _handle_thread({"group": GROUP_ID})

        assert route.called
        assert not listing.called

    @respx.mock
    async def test_thread_naming_both_references_refuses_and_reads_nothing(self):
        """The refusal is the SDK's one sentence, and it names both properties."""
        mock_token()
        mock_agents()
        conversation = respx.get(f"{API_URL}/conversations/{CONVERSATION_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [], "truncated": False})
        )
        group = respx.get(f"{API_URL}/groups/{GROUP_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [], "truncated": False})
        )

        out = await _handle_thread({"conversation_id": CONVERSATION_ID, "group": GROUP_ID})

        data = json.loads(out)
        assert data["error"] == TRANSCRIPT_REFERENCE_REQUIRED
        assert not conversation.called and not group.called

    @respx.mock
    async def test_thread_naming_neither_reference_refuses_and_reads_nothing(self):
        mock_token()
        mock_agents()

        out = await _handle_thread({})

        data = json.loads(out)
        assert data["error"] == TRANSCRIPT_REFERENCE_REQUIRED
        assert not any(call.request.url.path.endswith("/messages") for call in respx.calls)

    @respx.mock
    async def test_thread_with_a_group_name_in_conversation_id_names_group_and_sends_nothing(
        self,
    ):
        """The likeliest mistake, answered locally.

        Passing the group's NAME to the conversation parameter used to reach the
        server and come back a framework 422 carrying a pydantic error object.
        It is refused here before anything is sent, in a sentence that names the
        parameter that does take a group.
        """
        mock_token()
        mock_agents()

        out = await _handle_thread({"conversation_id": "quarterly planning group"})

        data = json.loads(out)
        assert data["error"] == not_a_conversation_uuid("quarterly planning group")
        # The refusal no longer carries the ``group=#logistics@…`` worked example
        # this line used to match — the sentence describes the three spellings
        # the parameter takes instead. What must survive is that the refusal
        # points the caller at the parameter that does read a group.
        assert "name the group instead" in data["error"]
        assert not any(call.request.url.path.endswith("/messages") for call in respx.calls)

    @respx.mock
    async def test_thread_with_a_dashless_conversation_uuid_still_reads(self):
        """The pre-flight refuses only what the wire would have refused.

        The server types that path parameter ``uuid.UUID``, which accepts the
        dashless spelling; a stricter local check would have been a capability
        removal wearing a validation fix.
        """
        mock_token()
        mock_agents()
        dashless = CONVERSATION_ID.replace("-", "")
        route = respx.get(f"{API_URL}/conversations/{dashless}/messages").mock(
            return_value=httpx.Response(200, json={"messages": [], "truncated": False})
        )

        out = await _handle_thread({"conversation_id": dashless})

        assert route.called
        assert "error" not in json.loads(out)


class TestThreadContract:
    """What the model is handed for ``rine_thread``, and what it is not.

    Zero new tools: the group reference is a property on the verb that already
    reads a transcript, not a second verb answering "how do I read a group?".
    ``tests/test_register.py`` holds the count of 25 that pins the other half of
    that claim.
    """

    def test_thread_takes_a_group_by_the_reference_every_group_tool_takes(self):
        """One reference across the group tools — ``rine_group_join`` excepted.

        The comparison used to be against ``rine_group_join``, which was the
        wrong witness rather than a wrong rule: join is the one group verb aimed
        at a group this org is NOT in, so the bare name the other nine resolve
        against this org's seats reaches only this agent's pending invitations
        there. Every remaining group tool still shares this reference, and the
        divergence below is asserted rather than left to the diff.
        """
        properties = SCHEMAS["rine_thread"]["parameters"]["properties"]
        for tool in ("rine_group_invite", "rine_group_inspect", "rine_group_vote"):
            assert properties["group"] == SCHEMAS[tool]["parameters"]["properties"]["group"]

    def test_thread_takes_a_bare_name_wider_than_the_one_irreversible_verb_reads_it(self):
        """The divergence is deliberate, and it is a narrowing on join's side.

        A model reads both schemas in one system prompt, so the difference has to
        be legible in the sentence itself: join says which one set it reads a
        bare name against, and why nothing wider, rather than silently narrowing
        the spelling the tool beside it invites.
        """
        thread = SCHEMAS["rine_thread"]["parameters"]["properties"]["group"]["description"]
        join = SCHEMAS["rine_group_join"]["parameters"]["properties"]["group"]["description"]
        assert "bare name" in thread
        assert thread != join
        assert "A bare name is read only against this agent's pending invitations" in join
        assert "join cannot be taken back" in join

    def test_thread_requires_neither_reference_individually(self):
        assert SCHEMAS["rine_thread"]["parameters"]["required"] == []

    def test_thread_states_the_exactly_one_rule_in_the_words_it_refuses_with(self):
        schema = SCHEMAS["rine_thread"]
        assert TRANSCRIPT_REFERENCE_REQUIRED in schema["description"]
        conversation = schema["parameters"]["properties"]["conversation_id"]["description"]
        assert TRANSCRIPT_REFERENCE_REQUIRED in conversation


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
