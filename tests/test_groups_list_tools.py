"""Behavioral tests for the group-listing handlers.

``rine_groups`` and ``rine_group_roster`` are authenticated reads of this agent's
own memberships; ``rine_discover_groups`` hits the UNAUTHENTICATED public group
directory (asserted the way the agent-directory tests assert it — by NOT mocking
``/oauth/token`` and confirming the call carries no Authorization header). Only
the rine HTTP boundary is mocked (respx); the SDK is real.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from rine import ACTING_AGENT_SPELLING

from hermes_rine._schemas import SCHEMAS
from hermes_rine.tools.groups_list import (
    _handle_discover_groups,
    _handle_group_roster,
    _handle_groups,
)

from .conftest import (
    AGENT_ID,
    API_URL,
    GROUP_CONVERSATION_ID,
    GROUP_HANDLE,
    GROUP_ID,
    OTHER_AGENT_ID,
    OTHER_HANDLE,
    SECOND_AGENT_ID,
    SELF_HANDLE,
    group_payload,
    member_payload,
    mock_agents,
    mock_group_row,
    mock_token,
    mock_two_agents,
)

#: A second group, so a list is genuinely plural.
OTHER_GROUP_ID = "bbbbbbbb-cccc-dddd-eeee-000000000002"
OTHER_GROUP_HANDLE = "#research@acme.rine.network"


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL (see messaging tests)."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


class TestGroups:
    @respx.mock
    async def test_groups_lists_every_membership_with_its_policy_and_encryption(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        group_payload(name="ops", member_count=4),
                        group_payload(
                            name="research",
                            handle=OTHER_GROUP_HANDLE,
                            group_id=OTHER_GROUP_ID,
                            member_count=2,
                            mls_enabled=True,
                            mls_group_id="mls-research",
                        ),
                    ]
                },
            )
        )

        out = await _handle_groups({})

        data = json.loads(out)
        assert data["count"] == 2
        assert "2 group(s)" in data["groups"]
        assert GROUP_HANDLE in data["groups"] and OTHER_GROUP_HANDLE in data["groups"]
        assert "4 member(s)" in data["groups"]
        assert "closed enrollment" in data["groups"]
        # The encryption regime comes from the SDK predicate, so the answer here
        # cannot drift from the one rine_group_inspect renders.
        assert "sender-key" in data["groups"] and "mls" in data["groups"]

    @respx.mock
    async def test_groups_carries_each_groups_conversation_id_on_the_listed_row(self):
        """The listed row carries the argument ``rine_thread`` needs.

        This is the catch-up affordance: the agent lists its groups, takes the
        conversation id off that same answer and reads the backlog — without
        having posted in the group and without having been handed a raw inbox
        row. The id has to be on the **plural** row; putting it only on
        ``rine_group_inspect`` would make the agent ask for the id in order to
        ask for the id.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={"items": [group_payload(name="ops", conversation_id=GROUP_CONVERSATION_ID)]},
            )
        )

        out = await _handle_groups({})

        assert f"conversation_id={GROUP_CONVERSATION_ID}" in json.loads(out)["groups"]

    @respx.mock
    async def test_groups_when_nothing_was_said_in_a_group_says_so_instead_of_none(self):
        """A group with no conversation reads as a fact, not as a bug or a refusal.

        The conversation is created on the group's first post, so a group nobody
        has spoken in legitimately has none. Rendering that as ``None`` — or
        dropping the clause — tells the model the tool is broken; the sentence
        tells it there is nothing to read yet.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(name="ops", conversation_id=None)]}
            )
        )

        out = await _handle_groups({})

        groups = json.loads(out)["groups"]
        assert "conversation_id=(none — nothing has been said in this group yet)" in groups
        assert "None" not in groups

    @respx.mock
    async def test_groups_names_which_of_this_orgs_agents_are_seated_in_each_group(self):
        """The org-scoped row answers the agent-scoped question it used to raise.

        ``GET /groups`` lists a group whenever **any** agent of the org is in it,
        so before this clause the row could not tell the agent whether the one
        about to post was among them. It answers in **handles** — the spelling
        ``rine_whoami`` already gave the caller, and one the acting-agent header
        takes as readily as a UUID, so the row names a seat without having to be
        joined against a second tool's output first.

        Both seats are this org's own. ``member_agent_ids`` is own-org by
        construction, so a fixture naming a peer-org agent would be pinning the
        rendering of a state the server cannot produce.
        """
        mock_token()
        mock_two_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        group_payload(name="ops", member_agent_ids=[AGENT_ID, SECOND_AGENT_ID])
                    ]
                },
            )
        )

        out = await _handle_groups({})

        groups = json.loads(out)["groups"]
        assert f"your_agents={SELF_HANDLE}, second-agent@myorg.rine.network" in groups
        assert AGENT_ID not in groups

    @respx.mock
    async def test_groups_when_no_agent_of_this_org_is_seated_says_so_instead_of_nothing(self):
        """An empty seat list is the answer, not the absence of one.

        The org is in the group — that is why the row is here at all — and no
        agent of it holds a seat, so a send would be refused. Dropping the clause
        would read as a rendering fault; printing an empty value would read as a
        group with no members.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(name="ops", member_agent_ids=[])]}
            )
        )

        out = await _handle_groups({})

        groups = json.loads(out)["groups"]
        assert "your_agents=(none — no agent of your org is seated in this group)" in groups

    @respx.mock
    async def test_groups_with_no_memberships_says_so_rather_than_listing_nothing(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))

        out = await _handle_groups({})

        data = json.loads(out)
        assert data["count"] == 0
        assert data["groups"] == "This agent belongs to no groups."

    @respx.mock
    async def test_groups_surfaces_a_server_failure_as_a_tool_error(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(500, json={}))

        out = await _handle_groups({})

        assert "error" in json.loads(out)


class TestGroupRoster:
    @respx.mock
    async def test_roster_renders_each_member_with_handle_role_and_join_date(self):
        mock_token()
        mock_agents()
        mock_group_row()
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        member_payload(AGENT_ID, SELF_HANDLE, role="admin"),
                        member_payload(OTHER_AGENT_ID, OTHER_HANDLE),
                    ]
                },
            )
        )

        out = await _handle_group_roster({"group": GROUP_ID})

        data = json.loads(out)
        assert data["count"] == 2
        assert "2 member(s)" in data["members"]
        assert f"{SELF_HANDLE} (role=admin" in data["members"]
        assert f"{OTHER_HANDLE} (role=member" in data["members"]
        assert "joined=2024-03-04" in data["members"]

    @respx.mock
    async def test_roster_marks_this_orgs_own_seats_and_still_lists_every_other_one(self):
        """``is_own_org`` is a marker, never a filter.

        A roster is authorised for the org but is not narrowed to it: the agent
        needs to see the ones it is talking **to** as much as the ones it can
        talk **as**. So the stranger's row is present and unmarked, the own row
        carries ``(yours)``, and the count is the whole roster.
        """
        mock_token()
        mock_agents()
        mock_group_row()
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        member_payload(AGENT_ID, SELF_HANDLE),
                        member_payload(OTHER_AGENT_ID, OTHER_HANDLE, is_own_org=False),
                    ]
                },
            )
        )

        out = await _handle_group_roster({"group": GROUP_ID})

        data = json.loads(out)
        assert data["count"] == 2
        assert f"{SELF_HANDLE} (role=member, joined=2024-03-04) (yours)" in data["members"]
        assert f"{OTHER_HANDLE} (role=member, joined=2024-03-04)" in data["members"]
        assert f"{OTHER_HANDLE} (role=member, joined=2024-03-04) (yours)" not in data["members"]

    @respx.mock
    async def test_roster_refused_for_the_acting_agent_says_which_agent_to_act_as(self):
        """The server's refusal reaches the model whole, plus this stack's remedy.

        Only the server knows **which** sibling holds the seat. Only this stack
        knows how its caller chooses one: hermes takes an ``agent`` argument and
        the SDK writes ``X-Rine-Agent`` from it, so an agent told to set that
        header itself would be reaching for something no tool here exposes.
        """
        mock_token()
        mock_agents()
        mock_group_row()
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(
                403,
                json={
                    "detail": (
                        "agent bob is not a member of this group; your agent alice is — "
                        "retry with X-Rine-Agent: alice@acme.rine.network"
                    )
                },
            )
        )

        out = await _handle_group_roster({"group": GROUP_ID})

        error = json.loads(out)["error"]
        assert "your agent alice is" in error
        # The remedy half is asserted as the SDK's own exported sentence rather
        # than a retyped copy of it. That sentence has changed spelling before
        # (``agent=<name>`` became ``agent=``) and a literal here would have had
        # to be re-typed to match — which is the edit that silently turns this
        # gate into a test of whatever the SDK happens to say.
        assert ACTING_AGENT_SPELLING in error
        assert "RINE_AGENT" in error

    @respx.mock
    async def test_roster_resolves_a_group_handle_before_reading_members(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload(name="ops")]})
        )
        members = respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(
                200, json={"items": [member_payload(AGENT_ID, SELF_HANDLE)]}
            )
        )

        out = await _handle_group_roster({"group": GROUP_HANDLE})

        assert members.called
        assert json.loads(out)["count"] == 1

    @respx.mock
    async def test_roster_of_a_group_with_no_seated_members_says_so(self):
        mock_token()
        mock_agents()
        mock_group_row()
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        out = await _handle_group_roster({"group": GROUP_ID})

        data = json.loads(out)
        assert data["count"] == 0
        assert data["members"] == "No members."

    @respx.mock
    async def test_roster_of_an_unknown_group_surfaces_a_tool_error(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(404, json={"detail": "Group not found"})
        )

        out = await _handle_group_roster({"group": GROUP_ID})

        assert "error" in json.loads(out)


class TestDiscoverGroups:
    @respx.mock
    async def test_discover_groups_returns_a_numbered_list_unauthenticated(self):
        route = respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": GROUP_ID,
                            "name": "ops",
                            "handle": GROUP_HANDLE,
                            "description": "Operations coordination.",
                            "member_count": 7,
                        }
                    ],
                    "total": 1,
                },
            )
        )

        out = await _handle_discover_groups({"q": "ops"})

        assert route.called
        data = json.loads(out)
        assert data["count"] == 1
        assert "1 public group(s) found in the directory" in data["groups"]
        # The directory scan carries no identity, so no row can say whether this
        # agent holds a seat — and the membership list this surface also prints
        # ends every row with exactly that. The header is what tells them apart.
        assert "not a group this agent is in" in data["groups"]
        assert GROUP_HANDLE in data["groups"]
        assert "7 member(s)" in data["groups"]
        assert "Operations coordination." in data["groups"]
        # Unauthenticated: the directory GET carries no Authorization header.
        assert "authorization" not in {k.lower() for k in route.calls[0].request.headers}

    @respx.mock
    async def test_discover_groups_passes_the_query_filter_to_the_directory(self):
        route = respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        await _handle_discover_groups({"q": "logistics", "limit": 5})

        request = route.calls[0].request
        assert request.url.params["q"] == "logistics"
        assert request.url.params["limit"] == "5"

    @respx.mock
    async def test_discover_groups_reports_the_next_cursor_when_more_pages_exist(self):
        respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": GROUP_ID,
                            "name": "ops",
                            "handle": GROUP_HANDLE,
                            "member_count": 1,
                        }
                    ],
                    "total": 40,
                    "next_cursor": "page-2",
                },
            )
        )

        out = await _handle_discover_groups({})

        assert json.loads(out)["next_cursor"] == "page-2"

    @respx.mock
    async def test_discover_groups_with_no_matches_says_so(self):
        respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_discover_groups({"q": "nothing-like-this"})

        data = json.loads(out)
        assert data["count"] == 0
        assert data["groups"] == "No public groups matched."

    @respx.mock
    async def test_discover_groups_renders_the_enrollment_policy_the_directory_sent(self):
        """The server sends ``enrollment_policy`` and the model is told which one.

        It is the only field on a discovery hit that decides what happens next:
        an ``open`` group is self-joinable with ``rine_group_join``, a
        ``unanimity`` one makes the caller wait for the electorate. The SDK's
        ``GroupSummary`` used to drop the field on parse, so this surface could
        only render a row a model had to guess about.
        """
        respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": GROUP_ID,
                            "name": "ops",
                            "handle": GROUP_HANDLE,
                            "member_count": 3,
                            "enrollment_policy": "unanimity",
                        }
                    ],
                    "total": 1,
                },
            )
        )

        out = await _handle_discover_groups({})

        assert "unanimity enrollment" in json.loads(out)["groups"]

    @respx.mock
    async def test_discover_groups_prints_the_id_rine_group_join_can_spend(self):
        """The row carries the only reference a stranger can act on.

        The handle it also prints resolves through ``GET /groups?handle=``,
        which is scoped to the groups this org already holds a seat in — empty
        by construction for the one caller a directory search exists for.
        ``POST /groups/{group_id}/join`` looks the group up globally, so the id
        is what turns a hit here into a join. The shape is the one
        ``rine-mastra`` and ``rine-eve`` already ship on their directory rows.
        """
        respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": GROUP_ID,
                            "name": "ops",
                            "handle": GROUP_HANDLE,
                            "member_count": 3,
                        }
                    ],
                    "total": 1,
                },
            )
        )

        out = await _handle_discover_groups({})

        assert f"{GROUP_HANDLE} (id {GROUP_ID})" in json.loads(out)["groups"]


class TestContracts:
    def test_each_listing_tool_ships_a_schema_the_registry_can_hand_a_model(self):
        for name in ("rine_groups", "rine_group_roster", "rine_discover_groups"):
            schema = SCHEMAS[name]
            assert schema["name"] == name
            assert schema["description"]
            assert schema["parameters"]["type"] == "object"

    def test_the_roster_tool_requires_the_group_it_is_asked_about(self):
        assert SCHEMAS["rine_group_roster"]["parameters"]["required"] == ["group"]
