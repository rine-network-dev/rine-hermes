"""Behavioral tests for the group-administration handlers.

Covers `rine_group_requests`, `rine_group_vote`, `rine_group_leave`,
`rine_group_sync` and `rine_group_reclaim` — the verbs an agent needs once it is
inside a group. Only
the rine HTTP boundary is mocked (respx); the SDK is real, so a handler that
stopped calling the right SDK method fails here rather than passing against a
double.

Hermes returns structured ``tool_result`` rather than prose, so the assertions are
on the fields an agent reads, not on a rendered sentence.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from hermes_rine._schemas import SCHEMAS
from hermes_rine.tools import _HANDLERS
from hermes_rine.tools.groups_admin import (
    _handle_group_leave,
    _handle_group_requests,
    _handle_group_sync,
    _handle_group_vote,
)
from hermes_rine.tools.groups_reclaim import _handle_group_reclaim

from .conftest import (
    AGENT_ID,
    API_URL,
    GROUP_HANDLE,
    GROUP_ID,
    OTHER_AGENT_ID,
    group_payload,
    mock_agents,
    mock_group_row,
    mock_token,
)

REQUEST_ID = "ffffffff-0000-1111-2222-333333333333"

_ADMIN_TOOLS = [
    "rine_group_requests",
    "rine_group_vote",
    "rine_group_leave",
    "rine_group_sync",
    "rine_group_reclaim",
]


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


def _request_row(*, status: str = "pending", expires_at: str | None = None, **over) -> dict:
    """One row as the server sends it, with the whole voting picture on it."""
    return {
        "id": REQUEST_ID,
        "group_id": GROUP_ID,
        "agent_id": OTHER_AGENT_ID,
        "invited_by": None,
        "message": None,
        "status": status,
        "expires_at": expires_at,
        "created_at": "2024-01-01T00:00:00Z",
        "resolved_at": None,
        "quorum_total_members": 9,
        "electorate_size": 3,
        "approvals": 1,
        "denials": 0,
        "approvals_needed": 1,
        "denials_needed": 2,
        "your_vote": None,
        "you_may_vote": True,
        "you_may_vote_reason": "in_electorate",
        **over,
    }


@pytest.mark.parametrize("name", _ADMIN_TOOLS)
def test_each_admin_verb_is_registered_with_a_schema(name):
    """A handler with no ``SCHEMAS`` entry makes ``register_rine_tools`` KeyError."""
    assert name in _HANDLERS
    assert SCHEMAS[name]["name"] == name


class TestGroupRequests:
    @respx.mock
    async def test_requests_reports_one_row_per_outstanding_entry(self):
        mock_token()
        mock_agents()
        mock_group_row()
        route = respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [_request_row()], "total": 1})
        )

        out = await _handle_group_requests({"group": GROUP_ID, "outstanding": "live"})

        assert route.calls.last.request.url.params["outstanding"] == "live"
        data = json.loads(out)
        assert data["outstanding"] == "live"
        assert data["requests"] == [
            {
                "request_id": REQUEST_ID,
                "agent_id": OTHER_AGENT_ID,
                "status": "pending",
                "expires_at": None,
                "quorum_total_members": 9,
                "electorate_size": 3,
                "approvals": 1,
                "denials": 0,
                "approvals_needed": 1,
                "denials_needed": 2,
                "your_vote": None,
                "you_may_vote": True,
                "you_may_vote_reason": "in_electorate",
                "eligibility": "you can vote",
            }
        ]

    @respx.mock
    async def test_requests_when_the_server_reports_a_denial_bar_then_the_model_reads_it(self):
        """The denial half of the outcome reaches the model, or it cannot see it at all.

        This payload is hand-built rather than rendered from the row, so a field
        the dict does not name is invisible here while every prose surface prints
        it. The denial bar moves on the same shrinking electorate the approval bar
        does, so a request can be refused by a denial that fell short when it was
        cast — an outcome a model reading approvals alone cannot account for.
        """
        mock_token()
        mock_agents()
        mock_group_row()
        row = _request_row(denials=1, denials_needed=1)
        respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [row], "total": 1})
        )

        data = json.loads(await _handle_group_requests({"group": GROUP_ID}))

        assert data["requests"][0]["denials"] == 1
        assert data["requests"][0]["denials_needed"] == 1

    @respx.mock
    async def test_requests_when_no_denial_bar_is_reachable_then_it_is_null_not_zero(self):
        """A bar nobody can reach is ``null``; zero would mean one more denial refuses it."""
        mock_token()
        mock_agents()
        mock_group_row()
        row = _request_row(
            electorate_size=0,
            approvals_needed=None,
            denials_needed=None,
            you_may_vote=False,
            you_may_vote_reason="electorate_empty",
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [row], "total": 1})
        )

        data = json.loads(await _handle_group_requests({"group": GROUP_ID}))

        assert data["requests"][0]["approvals_needed"] is None
        assert data["requests"][0]["denials_needed"] is None

    @respx.mock
    async def test_requests_when_a_member_nominated_the_agent_then_the_row_is_in_the_queue(self):
        """A nomination is a pending row like any other, and the vote queue holds it.

        An invite to a majority or unanimity group files a join
        request instead of minting a voucher, so ``pending`` is no longer only
        self-applications. It is born carrying the nominator's own approval,
        so the nominator's own row must not read as though they still
        owe a vote.
        """
        mock_token()
        mock_agents()
        mock_group_row()
        row = _request_row(
            invited_by=AGENT_ID,
            approvals=1,
            your_vote="approve",
            you_may_vote=False,
            you_may_vote_reason="already_voted",
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [row], "total": 1})
        )

        data = json.loads(await _handle_group_requests({"group": GROUP_ID}))

        assert data["requests"][0]["status"] == "pending"
        assert data["requests"][0]["approvals"] == 1
        assert data["requests"][0]["eligibility"] == "you already voted approve"

    @respx.mock
    async def test_requests_when_the_agent_joined_after_a_request_then_it_says_so(self):
        """The reason predicts the refusal, so the model need not try the vote."""
        mock_token()
        mock_agents()
        mock_group_row()
        row = _request_row(you_may_vote=False, you_may_vote_reason="not_in_electorate")
        respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [row], "total": 1})
        )

        data = json.loads(await _handle_group_requests({"group": GROUP_ID}))

        assert data["requests"][0]["you_may_vote"] is False
        assert data["requests"][0]["eligibility"] == (
            "you cannot vote: you were not in the group when this was filed"
        )

    @respx.mock
    async def test_requests_when_the_server_predates_electorates_then_the_payload_agrees(self):
        """A null on the wire is resolved, not handed to the model as a null.

        A server older than 0017 sends neither eligibility field and lets any
        current member vote on a pending request. Passing the raw value through
        produced ``you_may_vote: null`` beside a reason and a clause that both
        say the vote counts — a self-contradicting payload, on the one surface
        whose reader is a language model.
        """
        mock_token()
        mock_agents()
        mock_group_row()
        row = _request_row(
            electorate_size=None,
            you_may_vote=None,
            you_may_vote_reason=None,
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [row], "total": 1})
        )

        data = json.loads(await _handle_group_requests({"group": GROUP_ID}))

        assert data["requests"][0]["you_may_vote"] is True
        assert data["requests"][0]["you_may_vote_reason"] == "electorate_not_recorded"
        assert "any member may" in data["requests"][0]["eligibility"]

    @respx.mock
    async def test_requests_when_the_electorate_has_left_then_nobody_is_eligible(self):
        mock_token()
        mock_agents()
        mock_group_row()
        row = _request_row(
            electorate_size=0,
            approvals_needed=None,
            you_may_vote=False,
            you_may_vote_reason="electorate_empty",
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [row], "total": 1})
        )

        data = json.loads(await _handle_group_requests({"group": GROUP_ID}))

        assert data["requests"][0]["electorate_size"] == 0
        assert data["requests"][0]["eligibility"] == "nobody eligible is still in the group"

    @respx.mock
    async def test_requests_defaults_to_the_vote_queue(self):
        """`pending` is the only population a vote can act on, so it is the default."""
        mock_token()
        mock_agents()
        mock_group_row()
        route = respx.get(f"{API_URL}/groups/{GROUP_ID}/requests").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_group_requests({"group": GROUP_ID})

        assert route.calls.last.request.url.params["outstanding"] == "pending"
        assert json.loads(out)["requests"] == []


class TestGroupVote:
    @respx.mock
    async def test_vote_reports_the_choice_and_the_new_status(self):
        mock_token()
        mock_agents()
        mock_group_row()
        route = respx.post(f"{API_URL}/groups/{GROUP_ID}/requests/{REQUEST_ID}/vote").mock(
            return_value=httpx.Response(
                200, json={"request_id": REQUEST_ID, "your_vote": "deny", "status": "denied"}
            )
        )

        out = await _handle_group_vote(
            {"group": GROUP_ID, "request_id": REQUEST_ID, "vote": "deny"}
        )

        assert route.calls.last.request.content == b'{"vote":"deny"}'
        data = json.loads(out)
        assert data["your_vote"] == "deny"
        assert data["status"] == "denied"

    @respx.mock
    async def test_vote_when_it_carries_for_a_consenting_applicant_then_it_reports_a_member(self):
        """The ordinary carried vote: the applicant asked to be here and is now in."""
        mock_token()
        mock_agents()
        mock_group_row()
        respx.post(f"{API_URL}/groups/{GROUP_ID}/requests/{REQUEST_ID}/vote").mock(
            return_value=httpx.Response(
                200,
                json={"request_id": REQUEST_ID, "your_vote": "approve", "status": "approved"},
            )
        )

        data = json.loads(
            await _handle_group_vote(
                {"group": GROUP_ID, "request_id": REQUEST_ID, "vote": "approve"}
            )
        )

        assert data["status"] == "approved"
        assert data["detail"] == (
            "the electorate approved this request and the applicant is now a member"
        )

    @respx.mock
    async def test_vote_when_it_carries_on_an_unconsented_nomination_then_it_seats_nobody(self):
        """A fourth answer no client anticipated, and it must not read as approved.

        A member may nominate an agent, and the electorate approving that
        nomination is not the same event as the agent agreeing to be in the
        group. The server resolves that case to ``invited`` — a spendable
        voucher, not a seat. Reported as a bare token, a model reads it as the
        agent being in; with no clause at all it reads as an unknown status.
        """
        mock_token()
        mock_agents()
        mock_group_row()
        respx.post(f"{API_URL}/groups/{GROUP_ID}/requests/{REQUEST_ID}/vote").mock(
            return_value=httpx.Response(
                200,
                json={"request_id": REQUEST_ID, "your_vote": "approve", "status": "invited"},
            )
        )

        data = json.loads(
            await _handle_group_vote(
                {"group": GROUP_ID, "request_id": REQUEST_ID, "vote": "approve"}
            )
        )

        assert data["status"] == "invited"
        assert "spendable invitation instead of a seat" in data["detail"]
        assert "now a member" not in data["detail"]

    @respx.mock
    async def test_vote_when_the_server_answers_a_status_with_no_clause_then_it_says_so(self):
        """A fall-through arm that stays silent reports an unknown answer as understood."""
        mock_token()
        mock_agents()
        mock_group_row()
        respx.post(f"{API_URL}/groups/{GROUP_ID}/requests/{REQUEST_ID}/vote").mock(
            return_value=httpx.Response(
                200,
                json={"request_id": REQUEST_ID, "your_vote": "approve", "status": "deferred"},
            )
        )

        data = json.loads(
            await _handle_group_vote(
                {"group": GROUP_ID, "request_id": REQUEST_ID, "vote": "approve"}
            )
        )

        assert data["detail"] == (
            "the server answered a request status this plugin has no clause for"
        )

    @respx.mock
    async def test_vote_refusal_is_returned_as_a_tool_error(self):
        mock_token()
        mock_agents()
        mock_group_row()
        respx.post(f"{API_URL}/groups/{GROUP_ID}/requests/{REQUEST_ID}/vote").mock(
            return_value=httpx.Response(409, json={"detail": "Already voted"})
        )

        out = await _handle_group_vote(
            {"group": GROUP_ID, "request_id": REQUEST_ID, "vote": "deny"}
        )

        assert "Already voted" in json.loads(out)["error"]


class TestGroupLeave:
    @respx.mock
    async def test_leave_removes_the_acting_agent_and_reports_the_sdk_sentence(self):
        """A leave names THIS agent, and the sentence about it is the SDK's."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        route = respx.delete(f"{API_URL}/groups/{GROUP_ID}/members/{AGENT_ID}").mock(
            return_value=httpx.Response(204)
        )

        out = await _handle_group_leave({"group": GROUP_HANDLE})

        assert route.called
        data = json.loads(out)
        assert data["left"] is True
        assert data["message"]


class TestGroupSync:
    @respx.mock
    async def test_sync_recovers_a_sender_key_group_rather_than_refusing_it(self):
        """A sender-key group has no epoch chain, so the repair is its waiting keys.

        The fields an agent reads are the ones this group's encryption actually
        has: what was installed, and whether this agent's own material was
        retired. No rung, no epoch, no leaf — five nulls standing in for a chain
        the group has not got would be worse than an honest shape.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=group_payload())
        )
        # The scan is this agent's whole distribution inbox; an empty one is the
        # ordinary answer for a member whose peers have all already sent.
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        out = await _handle_group_sync({"group": GROUP_HANDLE})

        data = json.loads(out)
        assert "error" not in data
        assert data["regime"] == "sender-key"
        assert data["ingested"] == 0
        assert data["retired"] is False
        assert "rung" not in data
        assert "epoch" not in data
        assert "nothing new was installed" in data["message"]

    @respx.mock
    async def test_sync_scans_a_group_whose_tree_was_never_founded_and_names_this_agents_verb(
        self,
    ):
        """🔴 Closed, meant to run MLS, never founded — and it reads as open.

        The group is NOT frozen: the server accepts sender-key sends into it, so
        keys for it really are waiting and the scan is real work. It arrives as
        the sender-key shape plus one extra key — never as an MLS shape, which
        this surface would project into five nulls and read as a recovery. The
        remedy it can reach is ``rine_group_reclaim``; ``resume_admission`` is
        not a tool this plugin has.
        """
        mock_token()
        mock_agents()
        unfounded = group_payload(mls_enabled=True)
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [unfounded]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=unfounded)
        )
        inbox = respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        out = await _handle_group_sync({"group": GROUP_HANDLE})

        data = json.loads(out)
        assert "error" not in data
        assert data["regime"] == "sender-key"
        assert data["ingested"] == 0
        assert "ratchet tree was never founded" in data["mls_never_founded_note"]
        assert "Run rine_group_reclaim on it" in data["mls_never_founded_note"]
        # 🔴 Not under the key `warning`: `tool_result` already takes an actor
        # note by that name, so a second one raises rather than merging.
        assert "warning" not in data
        assert "rung" not in data
        # The scan really ran — refusing ahead of it cost the agent those keys.
        assert inbox.called

    @respx.mock
    async def test_sync_omits_the_warning_key_entirely_on_a_healthy_group(self):
        # Absent, never null. An agent reading these keys gets the ones its
        # group's encryption actually has.
        mock_token()
        mock_agents()
        open_group = group_payload(enrollment_policy="open", mls_enabled=True)
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [open_group]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=open_group)
        )
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        data = json.loads(await _handle_group_sync({"group": GROUP_HANDLE}))

        assert data["regime"] == "sender-key"
        assert "mls_never_founded_note" not in data
        # Same rule for the shortfall: this page covered the whole listing, so
        # there is no key rather than a zero reporting that nothing happened.
        assert "unscanned" not in data

    @respx.mock
    async def test_sync_carries_the_shortfall_when_the_page_did_not_cover_the_inbox(self):
        """The structured answer says how much the scan did not read, not only the prose.

        This handler projects its keys one by one while `rine-mcp` spreads the
        whole result and the CLI prints it, so a key left out of the projection
        is the same question answered differently by three surfaces. An agent
        branching on fields rather than parsing prose cannot otherwise tell
        "nothing was waiting" from "the scan read a fraction of what is waiting":
        both read `ingested: 0`.
        """
        mock_token()
        mock_agents()
        open_group = group_payload(enrollment_policy="open", mls_enabled=True)
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [open_group]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=open_group)
        )
        respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 136})
        )

        data = json.loads(await _handle_group_sync({"group": GROUP_HANDLE}))

        assert data["regime"] == "sender-key"
        assert data["unscanned"] == 136
        assert "136 older sender-key distribution(s)" in data["message"]

    @respx.mock
    async def test_sync_recovers_a_legitimately_open_group_rather_than_refusing_it(self):
        """🔴 The regression the never-founded arm most plausibly causes.

        An open group carries the SAME four MLS columns as the never-founded
        closed one — null latch, no init in flight — and it also carries
        ``mls_enabled=True``, because the server column defaults true for every
        group. Only ``enrollment_policy`` tells them apart, and this is the row
        that proves the arm reads it. The other sender-key test here cannot: its
        ``mls_enabled=False`` row is one no shipped client creates.
        """
        mock_token()
        mock_agents()
        openish = group_payload(enrollment_policy="open", mls_enabled=True)
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [openish]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=openish)
        )
        inbox = respx.get(f"{API_URL}/agents/{AGENT_ID}/messages").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        out = await _handle_group_sync({"group": GROUP_HANDLE})

        data = json.loads(out)
        assert "error" not in data
        assert data["regime"] == "sender-key"
        assert data["ingested"] == 0
        # The scan is the recovery here, so it must have been paid for.
        assert inbox.called


class TestGroupReclaim:
    @respx.mock
    async def test_reclaim_refuses_a_sender_key_group_in_the_sdk_words(self):
        """An open group runs sender keys, so it has no ratchet tree to clear."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [{**group_payload(), "enrollment_policy": "open"}]}
            )
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json={**group_payload(), "enrollment_policy": "open"})
        )

        out = await _handle_group_reclaim({"group": GROUP_HANDLE})

        assert "no ratchet tree to seat anyone in" in json.loads(out)["error"]
