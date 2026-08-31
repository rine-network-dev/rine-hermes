"""Behavioral tests for the acting agent on the five message tools.

The group tools and ``rine_send`` take an optional ``agent``; the read tools did
not, so reading stayed refused. The SDK resolves an acting agent on all five of
``rine_inbox``, ``rine_read``, ``rine_thread``, ``rine_reply`` and
``rine_send_and_wait`` because it decrypts and signs as one agent — so with two
active agents in the org and no parameter to name one with, each raised the SDK's
refusal with no answer available inside the tool call. The catch-up flow this
enables (``rine_groups`` reports a group's ``conversation_id``, ``rine_thread``
reads the backlog behind it) dead-ended on its second step.

The same parameter, wired the same way, on the same five. What is pinned here is
the refusal from the failing side, the recovery from the named side, and that a
single-agent org's request is the one it always sent.

🔴 What is NOT pinned here, and cannot be: what the SERVER does with the header.
``GET /messages/{id}`` and ``POST /messages/{id}/reply`` authorise by ORG and
resolve no acting agent, so naming a sibling agent on ``rine_read`` /
``rine_reply`` is accepted rather than refused. respx replaces the rine HTTP
boundary, so an ``X-Rine-Agent`` assertion below proves the header left this side
and nothing about the answer it gets. The evidence for that half has to be a
server test.

Only the rine HTTP boundary is mocked (respx); the SDK is real, so a header
asserted here is the header that would go on the wire and a plaintext asserted
here came through a real decrypt.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx
from rine._constants import EncryptionVersion

from hermes_rine._client import build_client
from hermes_rine._schemas import SCHEMAS
from hermes_rine.tools import _HANDLERS
from hermes_rine.tools.groups_list import _handle_groups
from hermes_rine.tools.messaging import (
    _handle_inbox,
    _handle_read,
    _handle_reply,
    _handle_send_and_wait,
    _handle_thread,
)

from .conftest import (
    AGENT_ID,
    API_URL,
    GROUP_CONVERSATION_ID,
    MESSAGE_ID,
    OTHER_HANDLE,
    REPLY_ID,
    group_payload,
    hpke_self_encrypt,
    message_payload,
    mock_agents,
    mock_recipient_keys,
    mock_self_keys,
    mock_token,
    mock_two_agents,
    mock_webfinger,
    sent_message_payload,
)

#: The tools this parameter covers, derived from the registry rather than listed:
#: everything ``tools/messaging.py`` handles. A sixth message tool added later
#: without the parameter fails the contract tests below rather than shipping
#: unreachable.
MESSAGE_TOOLS = sorted(
    name
    for name, (handler, _env) in _HANDLERS.items()
    if handler.__module__ == "hermes_rine.tools.messaging"
)

#: The tools that legitimately take no acting agent, and why: three unauthenticated
#: directory reads (no org, no agent) and ``rine_whoami`` (the question is who the
#: credentials are, which is what it answers). Everything else acts or reads as one
#: agent. The shipped prose says exactly this, in SKILL.md, the README and
#: references/hermes.md — this is the registry it is checked against.
NO_ACTING_AGENT = {"rine_discover", "rine_discover_groups", "rine_inspect", "rine_whoami"}


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


def self_message(config_dir: str, text: str, **kwargs) -> dict:
    """A server message row this agent can really decrypt and verify."""
    return message_payload(
        encryption_version=EncryptionVersion.HPKE_V1,
        encrypted_payload=hpke_self_encrypt(config_dir, {"text": text}),
        **kwargs,
    )


class TestContract:
    def test_every_message_tool_offers_an_acting_agent(self):
        for name in MESSAGE_TOOLS:
            props = SCHEMAS[name]["parameters"]["properties"]
            assert "agent" in props, f"{name} cannot name the agent it reads as"
            assert props["agent"]["type"] == "string"

    def test_the_five_message_tools_are_covered(self):
        # Derived, not typed: this says the set did not shrink under the test above.
        assert MESSAGE_TOOLS == [
            "rine_inbox",
            "rine_read",
            "rine_reply",
            "rine_send",
            "rine_send_and_wait",
            "rine_thread",
        ]

    def test_the_acting_agent_is_never_required(self):
        # A single-agent org has no answer to give here and must not be asked:
        # required would break every existing caller of all five.
        for name in MESSAGE_TOOLS:
            assert "agent" not in SCHEMAS[name]["parameters"].get("required", [])

    def test_only_the_directory_reads_and_whoami_take_no_acting_agent(self):
        # The claim the skill, the README and references/hermes.md all make, held
        # against the registry that decides it.
        without = {
            name
            for name, schema in SCHEMAS.items()
            if "agent" not in schema["parameters"]["properties"]
        }
        assert without == NO_ACTING_AGENT


class TestSingleAgentOrgIsUnchanged:
    """Unlike the group listings, a read is not header-free when it names nobody.

    Every SDK call here resolves an acting agent *before* building the header, so the
    SDK has always attached the org's auto-resolved agent — with or without this
    parameter. "Unchanged" therefore has to be measured against the call the old
    handler made, not against the absence of a header, which is why the call site
    from before this parameter existed is reproduced below as the baseline
    instead of being asserted about.
    """

    @respx.mock
    async def test_a_read_naming_no_agent_sends_the_request_it_sent_before(self, config_dir):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(200, json=self_message(config_dir, "hello"))
        )

        # The call site from before this parameter existed, verbatim: a bare
        # client and an unnamed read.
        async with build_client() as client:
            await client.read(MESSAGE_ID)
        before = route.calls[0].request

        await _handle_read({"message_id": MESSAGE_ID})
        after = route.calls[1].request

        assert after.url == before.url
        assert after.headers.get("X-Rine-Agent") == before.headers.get("X-Rine-Agent")

    @respx.mock
    async def test_a_thread_read_naming_no_agent_reads_as_the_orgs_only_agent(self, config_dir):
        mock_token()
        mock_agents()
        mock_self_keys(config_dir)
        route = respx.get(f"{API_URL}/conversations/{GROUP_CONVERSATION_ID}/messages").mock(
            return_value=httpx.Response(200, json={"messages": []})
        )

        out = await _handle_thread({"conversation_id": GROUP_CONVERSATION_ID})

        assert "No messages in this conversation." in json.loads(out)["thread"]
        # The one agent the org holds, resolved by the SDK as it always was —
        # never an empty header, which is what passing the argument through raw
        # would produce and what the server refuses.
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID


class TestATwoAgentOrgCanReadAgain:
    @respx.mock
    async def test_a_read_that_names_nobody_is_refused_with_the_agents_to_choose_from(
        self, config_dir
    ):
        # The defect this parameter closes, pinned from the failing side: the
        # refusal names both agents, which is what makes the retry with ``agent``
        # possible.
        mock_token()
        mock_two_agents()
        respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(200, json=self_message(config_dir, "hello"))
        )

        out = await _handle_read({"message_id": MESSAGE_ID})

        error = json.loads(out)["error"]
        assert "This org has more than one agent" in error
        assert "second-agent" in error

    @respx.mock
    async def test_a_read_that_names_the_acting_agent_returns_the_plaintext(self, config_dir):
        mock_token()
        mock_two_agents()
        mock_self_keys(config_dir)
        route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(200, json=self_message(config_dir, "the standup moved"))
        )

        out = await _handle_read({"message_id": MESSAGE_ID, "agent": AGENT_ID})

        assert "the standup moved" in json.loads(out)["message"]
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_the_catch_up_flow_reaches_a_group_backlog(self, config_dir):
        # The full catch-up flow, driven end to end: list the groups as an agent,
        # take the conversation id out of that answer exactly as the tool
        # description tells the model to, and read the backlog with it.
        mock_token()
        mock_two_agents()
        mock_self_keys(config_dir)
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(conversation_id=GROUP_CONVERSATION_ID)]}
            )
        )
        thread = respx.get(f"{API_URL}/conversations/{GROUP_CONVERSATION_ID}/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "messages": [
                        self_message(config_dir, "we shipped it", msg_type="rine.v1.dm"),
                    ]
                },
            )
        )

        listing = json.loads(await _handle_groups({"agent": AGENT_ID}))
        found = re.search(r"conversation_id=(\S+)", listing["groups"])
        assert found is not None, listing["groups"]

        out = await _handle_thread({"conversation_id": found.group(1), "agent": AGENT_ID})

        assert "we shipped it" in json.loads(out)["thread"]
        assert thread.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_an_inbox_read_and_its_ack_are_both_the_named_agent(self, config_dir):
        # The ack resolves an agent of its own and carries it in the PATH, not in a
        # header — so an unnamed ack behind a named read would fail, and the mail
        # this call just handed over would arrive again on the next check.
        mock_token()
        mock_two_agents()
        mock_self_keys(config_dir)
        inbox = respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": [self_message(config_dir, "morning")]})
        )
        ack = respx.post(f"{API_URL}/agents/{AGENT_ID}/messages/mark-delivered").mock(
            return_value=httpx.Response(200, json={"marked": 1})
        )

        out = json.loads(await _handle_inbox({"agent": AGENT_ID}))

        assert out["count"] == 1
        assert out["ack_failed"] is False
        assert inbox.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID
        assert json.loads(ack.calls[0].request.content)["message_ids"] == [MESSAGE_ID]

    @respx.mock
    async def test_a_reply_names_the_acting_agent_on_the_wire(self, config_dir):
        mock_token()
        mock_two_agents()
        mock_self_keys(config_dir)
        respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(200, json=self_message(config_dir, "ping"))
        )
        route = respx.post(f"{API_URL}/messages/{MESSAGE_ID}/reply").mock(
            return_value=httpx.Response(200, json={**sent_message_payload(), "id": REPLY_ID})
        )

        out = json.loads(
            await _handle_reply({"message_id": MESSAGE_ID, "body": "pong", "agent": AGENT_ID})
        )

        assert out["reply_id"] == REPLY_ID
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_send_and_wait_names_the_acting_agent_on_the_wire(self, config_dir, other_keys):
        mock_token()
        mock_two_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages/sync").mock(
            return_value=httpx.Response(
                200, json={"message": sent_message_payload(), "reply": None}
            )
        )

        out = json.loads(
            await _handle_send_and_wait(
                {"to": OTHER_HANDLE, "body": "are you there", "wait_seconds": 1, "agent": AGENT_ID}
            )
        )

        assert out["reply"] is None
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID
