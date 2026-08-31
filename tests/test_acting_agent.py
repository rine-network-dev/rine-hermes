"""Behavioral tests for the acting-agent parameter.

Every group tool and ``rine_send`` take an optional ``agent``: which of the org's
own agents the call acts as. An org with one agent never names it and its
requests go out exactly as before; an org with two has no other lever inside a
tool call, because a call that has to act as somebody is refused when it cannot
be attributed to one agent (server 422 on ``X-Rine-Agent``, or the SDK's own
refusal before it) — so without this parameter the ten acting group verbs and
``rine_send`` are unreachable the moment a second agent exists.

The deployment-wide lever is ``RINE_AGENT``, read by the SDK client
constructor and therefore by every tool here. It sits **below** this parameter:
a tool call that names an agent wins, silently, because the two are different
rungs of one ladder.

The other three — ``rine_groups``, ``rine_group_inspect``, ``rine_group_roster``
— are NOT in that set and were never refused: ``GET /groups`` and
``GET /groups/{id}/members`` are authorised for the calling ORG and take no
acting-agent narrowing. They carry the header for uniformity, which is all the
two wire tests below claim; a test asserting they answer differently with it
would be asserting a behaviour the server does not have.

Only the rine HTTP boundary is mocked (respx); the SDK is real, so a header
asserted here is the header that would go on the wire.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from rine._constants import EncryptionVersion

from hermes_rine._schemas import SCHEMAS
from hermes_rine._schemas_base import _ACTING_AGENT
from hermes_rine.tools import _HANDLERS
from hermes_rine.tools.groups import _handle_group_invites
from hermes_rine.tools.groups_list import _handle_group_roster, _handle_groups
from hermes_rine.tools.messaging import _handle_send, _handle_send_and_wait
from hermes_rine.tools.payments import _handle_pay

from .conftest import (
    AGENT_ID,
    API_URL,
    GROUP_HANDLE,
    GROUP_ID,
    MESSAGE_ID,
    OTHER_HANDLE,
    SECOND_AGENT_ID,
    SELF_HANDLE,
    hpke_self_encrypt,
    message_payload,
    mock_agents,
    mock_group_row,
    mock_recipient_keys,
    mock_self_keys,
    mock_token,
    mock_two_agents,
    mock_webfinger,
    registered_tool_id,
    sent_message_payload,
)

#: The tools this parameter covers, derived from the registry rather than listed:
#: every group tool (``rine_groups`` and the twelve ``rine_group_*``) plus
#: ``rine_send``, which addresses a group by handle and 422s in the same org for
#: the same reason.
GROUP_TOOLS = sorted(name for name in _HANDLERS if name.startswith("rine_group"))
ACTING_AGENT_TOOLS = [*GROUP_TOOLS, "rine_send"]


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL (see messaging tests)."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


class TestContract:
    def test_every_group_tool_and_send_offer_an_acting_agent(self):
        for name in ACTING_AGENT_TOOLS:
            props = SCHEMAS[name]["parameters"]["properties"]
            assert "agent" in props, f"{name} cannot name the agent it acts as"
            assert props["agent"]["type"] == "string"

    def test_the_acting_agent_is_never_required(self):
        # The single-agent org is the common one and must not be made to answer a
        # question it has no answer to: a required `agent` would break every
        # existing caller of every one of these tools.
        for name in ACTING_AGENT_TOOLS:
            assert "agent" not in SCHEMAS[name]["parameters"].get("required", [])

    def test_thirteen_group_tools_are_covered(self):
        # Derived, not typed: a group tool added later without an acting agent
        # fails the test above, and this one says the set did not shrink.
        assert len(GROUP_TOOLS) == 13

    def test_one_sentence_describes_the_acting_agent_everywhere(self):
        # Two descriptions for one concept inside one plugin is what a model
        # reads as two different parameters — the payments tools shipped this
        # parameter first and now share the same words.
        described = {
            json.dumps(schema["parameters"]["properties"]["agent"], sort_keys=True)
            for schema in SCHEMAS.values()
            if "agent" in schema["parameters"]["properties"]
        }
        assert described == {json.dumps(_ACTING_AGENT, sort_keys=True)}

    def test_the_description_says_when_the_agent_is_needed(self):
        # A model that is told only *what* the field is never fills it in, because
        # nothing in a one-agent org ever asked it to.
        assert "single agent" in _ACTING_AGENT["description"]
        assert "more than one active agent" in _ACTING_AGENT["description"]


class TestSingleAgentOrgIsUnchanged:
    @respx.mock
    async def test_a_group_list_naming_no_agent_sends_no_agent_header(self):
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        out = await _handle_groups({})

        assert json.loads(out)["count"] == 0
        assert "X-Rine-Agent" not in route.calls[0].request.headers

    @respx.mock
    async def test_an_empty_agent_is_read_as_no_agent(self):
        # A model that fills the field in with "" has named nobody; sending that
        # as a header would turn an unchanged call into a 422.
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_groups({"agent": ""})

        assert "X-Rine-Agent" not in route.calls[0].request.headers


class TestNamingTheActingAgent:
    """The agent a tool call names reaches the wire — as the id it resolves to.

    The tool takes the spelling a model can produce (a handle here); the SDK
    answers it from the org's OWN agents and sends the id, because an acting
    agent must be an agent the caller owns. Asserting the id rather than the
    handle is therefore the stronger claim: it says the lookup happened and
    found *this* agent, where the handle would only have said the string
    survived the trip.
    """

    @respx.mock
    async def test_a_group_list_names_the_acting_agent_on_the_wire(self):
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_groups({"agent": SELF_HANDLE})

        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_a_roster_read_by_group_uuid_names_the_acting_agent(self):
        # The case a constructor-only wiring misses: this read resolves no agent
        # id of its own, so the header exists only if the call carries the agent.
        # What that buys is uniformity, never a narrower answer — the route is
        # org-scoped (`list_members`), so this asserts the wire and nothing about
        # the result.
        mock_token()
        mock_agents()
        mock_group_row()
        route = respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_group_roster({"group": GROUP_ID, "agent": SELF_HANDLE})

        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_the_offers_addressed_to_an_agent_are_read_as_that_agent(self):
        # Agent-scoped, not group-scoped: which agent asks IS the query here, so
        # an unattributed call answers about the wrong inbox or not at all.
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_group_invites({"agent": SELF_HANDLE})

        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID


class TestAValueThatNamesNobodyInAOneAgentOrg:
    """The slot resolves instead of refusing, and says what it ignored.

    Measured behaviour, not a guess: a model handed an optional actor slot fills
    it with the schema's own placeholder text (``"null"``, ``"<agent>"``, the
    literal word ``agent``) and then reads the refusal back as its answer instead
    of retrying. In an org with one agent that refusal was pure loss — the slot
    carries no information there, because there is nothing to choose.

    So the call succeeds as the only agent and the ignored value is *reported*.
    The channel is the tool result, which is the only thing the model reads; the
    SDK mints the same sentence into a ``logger.warning`` that reaches an
    operator's terminal and no model's context, which is why the resolving
    happens in the handler rather than inside the verb.
    """

    @respx.mock
    async def test_a_value_naming_nobody_acts_as_the_only_agent_and_says_so(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        data = json.loads(await _handle_group_invites({"agent": "null"}))

        # It SUCCEEDED — the whole point. A warning delivered on a refusal is
        # still a refusal, and the refusal is what this round removed.
        assert "error" not in data
        assert data["invites"] == []
        # All three facts the caller needs: what was passed, who acted instead,
        # and that the slot was never load-bearing here.
        assert "'null'" in data["warning"]
        assert "test-agent" in data["warning"]
        assert "Omit agent" in data["warning"]

    @respx.mock
    async def test_the_ignored_value_still_reaches_the_wire_as_the_resolved_agent(self):
        # The warning is not the only correction: the header carries the agent
        # that actually acted, so a server-side audit of the call agrees with
        # what the model was told.
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_group_invites({"agent": "<agent>"})

        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_a_correct_value_is_never_complained_at(self):
        # Warning on a correct call would teach a model that naming the agent
        # correctly produces complaints — the lesson this round is trying to
        # avoid teaching.
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        data = json.loads(await _handle_group_invites({"agent": "test-agent"}))

        assert "warning" not in data

    @respx.mock
    async def test_naming_nobody_is_still_refused_once_a_second_agent_exists(self):
        # The fallback is legitimate only because a one-agent org has nothing to
        # choose. Two agents and it is a guess, so the refusal stands.
        mock_token()
        mock_two_agents()
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        data = json.loads(await _handle_group_invites({"agent": "null"}))

        assert "warning" not in data
        assert "No agent named 'null'" in data["error"]
        assert "second-agent" in data["error"]

    @pytest.mark.parametrize(
        ("handler", "args"),
        [(_handle_groups, {}), (_handle_group_roster, {"group": GROUP_ID})],
        ids=["rine_groups", "rine_group_roster"],
    )
    @respx.mock
    async def test_an_org_scoped_read_carries_no_warning(self, handler, args):
        # ``GET /groups`` and ``GET /groups/{id}/members`` answer for the whole
        # org and ``agent`` narrows neither. Nothing was ignored and no agent
        # acted, so a warning here would attribute an org-wide answer to one
        # agent and invite a retry that changes no byte of it.
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))
        mock_group_row()
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        data = json.loads(await handler({**args, "agent": "null"}))

        assert "error" not in data
        assert "warning" not in data

    async def test_a_call_that_names_nobody_reaches_no_agent_directory(self):
        """A refusal that was local and free stays local and free.

        No mocks at all: any HTTP would fail. ``rine_send_and_wait`` addressed to
        a group is refused by the SDK before a request goes out, and settling an
        actor nobody named in front of it would answer *"Rine auth failed"* to a
        model whose actual mistake was the tool choice. Buying a warning nobody
        asked for with the surface's clearest refusal is a net loss.
        """
        data = json.loads(await _handle_send_and_wait({"to": GROUP_HANDLE, "body": "nope"}))

        assert data["error"] == (
            f"{registered_tool_id(_handle_send_and_wait)} is 1:1 only;"
            f" use {registered_tool_id(_handle_send)} for groups."
        )


class TestTwoAgentOrg:
    @respx.mock
    async def test_a_send_that_names_the_acting_agent_goes_out(self, config_dir, other_keys):
        mock_token()
        mock_two_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        out = await _handle_send({"to": OTHER_HANDLE, "body": "hi", "agent": AGENT_ID})

        assert json.loads(out)["sent"] is True
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_a_send_that_names_nobody_is_refused_with_the_agents_to_choose_from(
        self, config_dir, other_keys
    ):
        # The defect this parameter closes, pinned from the failing side: the
        # refusal names both agents, which is what makes the retry with ``agent``
        # possible.
        mock_token()
        mock_two_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        out = await _handle_send({"to": OTHER_HANDLE, "body": "hi"})

        error = json.loads(out)["error"]
        assert "This org has more than one agent" in error
        assert "second-agent" in error

    @respx.mock
    async def test_a_payment_reads_its_quote_as_the_acting_agent(self, config_dir):
        # `rine_pay` always took an `agent` and handed it to the payer ladder, but
        # the read in front of that ladder named nobody — so the argument it
        # advertises never got as far as being used.
        mock_token()
        mock_two_agents()
        mock_self_keys(config_dir)
        route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
            return_value=httpx.Response(
                200,
                json=message_payload(
                    encryption_version=EncryptionVersion.HPKE_V1,
                    encrypted_payload=hpke_self_encrypt(config_dir, {"text": "hello"}),
                    msg_type="rine.v1.task_request",
                ),
            )
        )

        out = await _handle_pay({"message_id": MESSAGE_ID, "agent": AGENT_ID})

        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID
        assert "more than one agent" not in out


class TestTheRetiredSpelling:
    """``as`` is still read as the actor, and is not on the schema.

    A model is shown one spelling — ``agent`` — so the tolerance is invisible to
    it by design. What it buys is the caller that has read older prose, or an
    older transcript, and writes ``as``: before this the property was silently
    dropped (no schema declares ``additionalProperties: false``) and the call
    acted as somebody else entirely.
    """

    def test_the_retired_spelling_is_not_declared_on_any_schema(self):
        for name, schema in SCHEMAS.items():
            assert "as" not in schema["parameters"]["properties"], name

    @respx.mock
    async def test_a_call_that_says_as_still_names_the_acting_agent(self):
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_groups({"as": SELF_HANDLE})

        # The id the handle resolves to — the alias reaches the same lookup the
        # canonical spelling does, not a shortcut past it.
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_both_spellings_naming_one_agent_are_one_value(self):
        mock_token()
        mock_agents()
        route = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_groups({"agent": SELF_HANDLE, "as": SELF_HANDLE.upper()})

        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID

    @respx.mock
    async def test_two_spellings_naming_two_agents_are_refused_as_a_tool_message(self):
        # Same rung, so no precedence rule may pick a winner — the loser is an
        # identity the caller believed it was acting as. The refusal comes back
        # as a tool message like every other failure here, never as a traceback.
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))

        out = await _handle_groups({"agent": SELF_HANDLE, "as": "second-agent"})

        error = json.loads(out)["error"]
        assert "named the acting agent twice" in error
        assert SELF_HANDLE in error and "second-agent" in error


class TestTheDeploymentWideAgent:
    @respx.mock
    async def test_a_two_agent_org_acts_as_the_agent_the_environment_names(self, monkeypatch):
        # The whole point of ``RINE_AGENT`` for a host like Hermes: the operator
        # names the agent once, in the process environment, and the model — which
        # is never shown a list of the org's agents — does not have to. Without it
        # this call sends no header at all and the org's two agents are a coin
        # toss the server refuses to make.
        monkeypatch.setenv("RINE_AGENT", "second-agent")
        mock_token()
        mock_two_agents()
        route = respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        await _handle_groups({})

        assert route.calls[0].request.headers["X-Rine-Agent"] == SECOND_AGENT_ID

    @respx.mock
    async def test_a_named_agent_beats_the_environment_without_saying_so(
        self, config_dir, other_keys, monkeypatch
    ):
        # Cross-rung precedence is silent: only two values on the SAME rung
        # refuse. A tool call that names an agent has said the last word.
        monkeypatch.setenv("RINE_AGENT", "second-agent")
        mock_token()
        mock_two_agents()
        mock_webfinger()
        mock_recipient_keys(config_dir)
        route = respx.post(f"{API_URL}/messages").mock(
            return_value=httpx.Response(200, json=sent_message_payload())
        )

        out = await _handle_send({"to": OTHER_HANDLE, "body": "hi", "agent": AGENT_ID})

        assert json.loads(out)["sent"] is True
        assert route.calls[0].request.headers["X-Rine-Agent"] == AGENT_ID
