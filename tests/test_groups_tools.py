"""Behavioral tests for the group handlers.

Covers create (post-quantum MLS by default, founding real MLS state, single-call with
description and with a vote window), invite, remove (by handle as readily as by
UUID), and inspect's MLS-vs-sender-key encryption diagnosis line. Only the rine
HTTP boundary is mocked (respx); the SDK is real.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
import respx

from hermes_rine._schemas import SCHEMAS
from hermes_rine.tools.groups import (
    _handle_group_create,
    _handle_group_inspect,
    _handle_group_invite,
    _handle_group_invites,
    _handle_group_join,
    _handle_group_remove,
)

from .conftest import (
    AGENT_ID,
    API_URL,
    GROUP_CONVERSATION_ID,
    GROUP_HANDLE,
    GROUP_ID,
    OTHER_AGENT_ID,
    OTHER_HANDLE,
    SELF_HANDLE,
    group_payload,
    member_payload,
    mock_agents,
    mock_group_mls_init,
    mock_token,
)

#: A second invitee, so a batch is genuinely plural. Local rather than in
#: conftest: only the admission tests need a third party.
THIRD_AGENT_ID = "cccccccc-dddd-eeee-ffff-111111111111"

#: A second group, so a bare name can genuinely answer to two of them.
OTHER_GROUP_ID = "dddddddd-eeee-ffff-1111-222222222222"

#: ``rine_discover`` written as its own token.
#:
#: 🔴 A plain ``"rine_discover" not in error`` cannot express this: ``rine_discover``
#: is a PREFIX of ``rine_discover_groups``, so the substring test fails on a refusal
#: that names only the groups verb — which is the correct verb for a group 404 and
#: the one the SDK's own refusal names. The word boundary after ``discover`` does not
#: match inside ``rine_discover_groups`` (``_`` is a word character), so this catches
#: the agent-directory verb and tolerates the group one.
_BARE_RINE_DISCOVER = re.compile(r"\brine_discover\b")


def names_agent_discovery(text: str) -> bool:
    """Whether ``text`` names ``rine_discover``, the AGENT-directory verb."""
    return _BARE_RINE_DISCOVER.search(text) is not None


class TestNamesAgentDiscovery:
    """The guard above, driven both ways — it is what an inspect assertion rests on."""

    def test_names_agent_discovery_when_the_generic_agent_refusal_is_given_is_true(self):
        # The exact tail ``_not_found_message`` appends when nothing group-shaped
        # was refused. This is the sentence the inspect assertion exists to catch.
        assert names_agent_discovery(
            "Not found: nope. Try rine_discover to find the right handle."
        )

    def test_names_agent_discovery_when_only_the_groups_verb_is_named_is_false(self):
        assert not names_agent_discovery(
            "Try rine_discover_groups to search the public directory."
        )


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL (see messaging tests)."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


class TestGroupCreate:
    @respx.mock
    async def test_create_returns_mls_confirmation(self):
        mock_token()
        mock_agents()
        mock_group_mls_init()
        respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        out = await _handle_group_create({"name": "ops", "visibility": "private"})

        data = json.loads(out)
        assert data["created"] is True
        assert data["handle"] == GROUP_HANDLE
        assert data["encryption"] == "mls"
        # The regime is named in words too, so a model does not have to
        # know what "mls" implies about post-quantum.
        assert "post-quantum X-Wing" in data["encryption_detail"]

    @respx.mock
    async def test_create_when_the_founding_landed_and_the_re_read_failed_still_reports_mls(self):
        """A founded group is announced as MLS even when the confirming re-read is refused.

        🔴 The founding LANDS here and only the re-read that would carry the
        latch back is refused, so what the SDK hands this tool is the PRE-init
        create response: no latch, no pending, ``mls_enabled`` true — field for
        field the shape every predicate below the admission arm reads as a group
        whose ratchet tree was never founded. It is not one, and calling it one
        sends a model to found what is already founded.

        ``mls_admission`` is what the founding returned and it exists nowhere
        else: ``rine._resources.group_ops.create_group_async`` carries it across
        the failed re-read for exactly this reason, and ``rine.format`` asks for
        it before it asks any predicate. Its TypeScript twin ``createdGroupMls``
        checks ``mlsAdmission`` first for the same reason.

        The never-founded rendering is NOT reachable on this path: a create that
        skips the founding is a create that sent ``mls_enabled=false``, and the
        row comes back sender-key. Which verb THIS surface names for a genuinely
        unfounded group is asserted where that state is real —
        ``test_inspect_group_created_for_mls_and_never_founded_names_this_surfaces_verb``.
        """
        mock_token()
        mock_agents()
        respx.post(f"{API_URL}/groups/{GROUP_ID}/mls/init").mock(
            return_value=httpx.Response(200, json={"members": []})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/mls/commit").mock(
            return_value=httpx.Response(200, json={"epoch": 1})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}").mock(return_value=httpx.Response(503, json={}))
        respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops", mls_enabled=True))
        )

        out = await _handle_group_create({"name": "ops", "visibility": "private"})

        data = json.loads(out)
        assert data["encryption"] == "mls"
        assert "post-quantum X-Wing" in data["encryption_detail"]
        assert "its ratchet tree was never founded" not in data["encryption_detail"]
        assert "rine_group_reclaim" not in data["encryption_detail"]
        # The carried admission, reported. It is the whole of the evidence that
        # the founding landed, so a create that dropped it would go straight
        # back to telling the model the group has no tree.
        assert data["mls_admission"] == {
            "epoch": 1,
            "commits": 1,
            "added": 0,
            "outcomes": [],
        }

    @respx.mock
    async def test_create_without_a_visibility_is_refused_in_rines_words(self):
        """No client default, and the refusal names both choices.

        It is the SDK's sentence, not a copy of it: a second copy with no gate
        keeping the two in step is how the surfaces disagreed in the first place.
        """
        mock_token()
        mock_agents()
        create = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        out = await _handle_group_create({"name": "ops"})

        assert not create.called
        assert "dir.rine.network" in out
        assert 'visibility=\\"public\\"' in out or 'visibility="public"' in out

    def test_create_schema_requires_visibility_with_no_default(self):
        """The model is told by the schema, at the point of choice."""
        prop = SCHEMAS["rine_group_create"]["parameters"]["properties"]["visibility"]
        assert "default" not in prop
        assert prop["enum"] == ["public", "private"]
        assert "dir.rine.network" in prop["description"]
        assert "member-joined signal off" in prop["description"]
        assert "visibility" in SCHEMAS["rine_group_create"]["parameters"]["required"]

    @respx.mock
    async def test_create_with_a_roster_invites_and_seats_nobody(self):
        """A roster mints invitations, and the group still has one member."""
        mock_token()
        mock_agents()
        mock_group_mls_init()
        route = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "group": group_payload(name="ops"),
                    "roster": {
                        "invited": 1,
                        "skipped": 1,
                        "entries": [
                            {"agent_id": OTHER_AGENT_ID, "status": "invited", "reason": None},
                            {
                                "agent_id": THIRD_AGENT_ID,
                                "status": "skipped",
                                "reason": "already_a_member",
                            },
                        ],
                    },
                },
            )
        )

        out = await _handle_group_create(
            {
                "name": "ops",
                "visibility": "private",
                "members": [OTHER_AGENT_ID, THIRD_AGENT_ID],
            }
        )

        body = json.loads(route.calls[0].request.content)
        assert body["members"] == [OTHER_AGENT_ID, THIRD_AGENT_ID]
        roster = json.loads(out)["roster"]
        assert roster["invited"] == 1
        assert roster["entries"][1]["reason"] == "already_a_member"
        # The token to branch on AND the sentence for it, so a model
        # that has no rule for this token still knows what happened.
        assert roster["entries"][1]["detail"] == "already a member of this group"
        assert roster["entries"][0]["detail"] == "invited"

    @respx.mock
    async def test_open_group_roster_is_not_reported_as_a_failure(self):
        """An open group's roster carries the reason it needs no voucher.

        Every entry comes back ``skipped/not_applicable`` because there is
        nothing to mint — and the very same call DMs each of those agents an
        invitation notice. The token alone reads as a failure of the whole
        roster; ``detail`` says what actually happened.
        """
        mock_token()
        mock_agents()
        mock_group_mls_init()
        respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "group": group_payload(name="ops"),
                    "roster": {
                        "invited": 0,
                        "skipped": 1,
                        "entries": [
                            {
                                "agent_id": OTHER_AGENT_ID,
                                "status": "skipped",
                                "reason": "not_applicable",
                            }
                        ],
                    },
                },
            )
        )

        out = await _handle_group_create(
            {
                "name": "ops",
                "visibility": "private",
                "enrollment": "open",
                "members": [OTHER_AGENT_ID],
            }
        )

        entry = json.loads(out)["roster"]["entries"][0]
        assert entry["reason"] == "not_applicable"
        assert entry["detail"].startswith("this group takes open enrollment")
        assert "may join whenever it likes" in entry["detail"]

    @respx.mock
    async def test_create_founds_pq_mls_group_on_the_wire(self):
        """A default group is post-quantum MLS, and asks the server for nothing.

        MLS is the server's own default for a closed group, so what proves the
        group is an MLS one is the founding — not a flag echoing the default
        back at the server.
        """
        mock_token()
        mock_agents()
        mls_init = mock_group_mls_init()
        route = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        await _handle_group_create({"name": "ops", "visibility": "private"})

        body = json.loads(route.calls[0].request.content)
        assert "mls_enabled" not in body
        # The founder immediately opens the MLS init window (no swallowed failure).
        assert mls_init.called

    @respx.mock
    async def test_create_with_enable_mls_false_asks_the_server_and_founds_nothing(self):
        """A sender-key group is asked for on the wire, and no tree is founded.

        Both halves matter. Skipping the founding alone would leave a closed row
        the server still records as an MLS group with no latch — the
        ``mls-never-founded`` state this surface reports, for the life of the
        group, on a group whose operator deliberately asked for sender keys.
        """
        mock_token()
        mock_agents()
        mls_init = mock_group_mls_init()
        route = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops", mls_enabled=False))
        )

        out = await _handle_group_create(
            {"name": "ops", "visibility": "private", "enable_mls": False}
        )

        assert json.loads(route.calls[0].request.content)["mls_enabled"] is False
        assert not mls_init.called
        data = json.loads(out)
        assert data["created"] is True
        assert data["encryption"] == "sender-key"

    @respx.mock
    @pytest.mark.parametrize("refusal", ["false", "False", " no ", 0, "off"])
    async def test_create_when_a_model_writes_a_falsy_non_bool_then_it_still_asks_the_server(
        self, refusal
    ):
        """A model's ``"false"`` reaches the wire as ``false``, not as silence.

        The SDK gates the field on ``enable_mls is False`` — an identity test —
        so every one of these used to pass straight through as *unset*, the
        server applied its ``True`` default, and the caller got a post-quantum
        MLS group after asking for a broadcast one, with no error anywhere. This
        is the one Python surface with no pydantic model between the model's
        JSON and the SDK.
        """
        mock_token()
        mock_agents()
        mls_init = mock_group_mls_init()
        route = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops", mls_enabled=False))
        )

        await _handle_group_create({"name": "ops", "visibility": "private", "enable_mls": refusal})

        assert json.loads(route.calls[0].request.content)["mls_enabled"] is False
        assert not mls_init.called

    @respx.mock
    @pytest.mark.parametrize("empty", [None, "", "   "])
    async def test_create_when_the_flag_is_empty_then_the_server_default_still_decides(
        self, empty
    ):
        """Nothing asked is nothing sent — the unset arm, not a ``false``.

        An empty slot is a model that filled the field with nothing, and this
        plugin states no default of its own: the key stays off the wire and the
        server's ``GroupCreate`` default is what makes the group.
        """
        mock_token()
        mock_agents()
        mls_init = mock_group_mls_init()
        route = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        await _handle_group_create({"name": "ops", "visibility": "private", "enable_mls": empty})

        assert "mls_enabled" not in json.loads(route.calls[0].request.content)
        assert mls_init.called

    def test_create_schema_offers_the_encryption_choice_without_defaulting_it(self):
        """A model can ask for a sender-key group, and omitting it keeps MLS.

        The flag shipped on the TypeScript surfaces while every Python one hid
        it, so a plugin that wanted a broadcast group could only get an MLS one.
        It stays optional: an unnamed value sends no ``mls_enabled`` at all, and
        the group the server makes for it is the MLS group it always made.
        """
        params = SCHEMAS["rine_group_create"]["parameters"]
        prop = params["properties"]["enable_mls"]
        assert prop["type"] == "boolean"
        assert "default" not in prop
        assert "sender-key" in prop["description"]
        assert params["required"] == ["name", "visibility"]

    @respx.mock
    async def test_create_with_description_is_a_single_call(self):
        """description rides the create body — no follow-up groups.update PATCH."""
        mock_token()
        mock_agents()
        mock_group_mls_init()
        create = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json=group_payload(name="ops", description="war room")
            )
        )
        update = respx.patch(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        out = await _handle_group_create(
            {"name": "ops", "visibility": "private", "description": "war room"}
        )

        body = json.loads(create.calls[0].request.content)
        assert body["description"] == "war room"
        assert not update.called
        assert json.loads(out)["created"] is True

    def test_create_schema_offers_the_vote_window_within_the_servers_bounds(self):
        """The vote deadline a model can choose, and the bounds it is choosable in.

        The server has always accepted 1-72 hours at create; until this shipped
        only the CLI could ask for it, so an LLM was stuck with the default 72
        whatever its operator wanted. The units ride in the name so a model
        cannot pass ``3`` meaning days.
        """
        params = SCHEMAS["rine_group_create"]["parameters"]
        prop = params["properties"]["vote_duration_hours"]
        assert prop["type"] == "integer"
        assert (prop["minimum"], prop["maximum"]) == (1, 72)
        assert "hours" in prop["description"]
        # Optional: a group that names no window is founded at the server's 72.
        assert params["required"] == ["name", "visibility"]

    @respx.mock
    async def test_create_with_a_vote_window_is_a_single_call(self):
        """The window rides the create body — no follow-up groups.update PATCH.

        A group founded at the default and then edited is a group that existed,
        for as long as the second call had not landed, under a deadline nobody
        chose — and a join request filed in that window is decided under it.
        """
        mock_token()
        mock_agents()
        mock_group_mls_init()
        create = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )
        update = respx.patch(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        out = await _handle_group_create(
            {
                "name": "ops",
                "visibility": "private",
                "enrollment": "majority",
                "vote_duration_hours": 1,
            }
        )

        assert json.loads(create.calls[0].request.content)["vote_duration_hours"] == 1
        assert not update.called
        assert json.loads(out)["created"] is True

    @respx.mock
    async def test_create_without_a_vote_window_sends_none(self):
        """The server's default stays the server's: an unnamed window is absent, not 72."""
        mock_token()
        mock_agents()
        mock_group_mls_init()
        create = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        await _handle_group_create({"name": "ops", "visibility": "private"})

        assert "vote_duration_hours" not in json.loads(create.calls[0].request.content)

    def test_create_schema_says_the_description_is_not_end_to_end_encrypted(self):
        """House rules everyone reads are also readable by our server.

        The group's messages are end-to-end encrypted and this text is not. A
        model told only that the field is "optional" has no way to know that,
        and a group's description is exactly where an operator is tempted to
        put something private.
        """
        prop = SCHEMAS["rine_group_create"]["parameters"]["properties"]["description"]
        assert "NOT end-to-end encrypted" in prop["description"]
        assert "never put anything private in it" in prop["description"]

    @respx.mock
    async def test_create_name_conflict_returns_clear_error(self):
        mock_token()
        mock_agents()
        respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(409, json={"detail": "group name taken"})
        )

        out = await _handle_group_create({"name": "ops", "visibility": "private"})

        assert "group name taken" in out


class TestGroupInvite:
    @respx.mock
    async def test_invite_agent_returns_status(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/invite").mock(
            return_value=httpx.Response(200, json={"status": "pending", "request_id": None})
        )

        out = await _handle_group_invite(
            {"group": GROUP_HANDLE, "agent_to_invite": OTHER_AGENT_ID}
        )

        data = json.loads(out)
        assert data["invited"] == OTHER_AGENT_ID
        assert data["status"] == "pending"
        assert "nomination the group's electorate decides" in data["detail"]
        assert "nobody is seated until the vote carries" in data["detail"]

    @respx.mock
    async def test_invite_to_a_closed_group_still_mints_a_spendable_invitation(self):
        """The control arm: `closed` is untouched by the voting rule and still invites."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/invite").mock(
            return_value=httpx.Response(200, json={"status": "invited", "id": None})
        )

        data = json.loads(
            await _handle_group_invite({"group": GROUP_HANDLE, "agent_to_invite": OTHER_AGENT_ID})
        )

        assert data["status"] == "invited"
        assert "spends by joining" in data["detail"]
        assert "electorate" not in data["detail"]

    @respx.mock
    async def test_invite_a_batch_into_a_voting_group_reports_each_entry_as_nominated(self):
        """A nomination is not an invitation to accept, and says so per entry.

        Falling through to the skip sentence would name a reason the entry does
        not carry; calling it ``invited`` would tell a model an agent holds a
        voucher the electorate has not granted.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/invite").mock(
            return_value=httpx.Response(
                200,
                json={
                    "invited": 1,
                    "skipped": 0,
                    "entries": [
                        {"agent_id": OTHER_AGENT_ID, "status": "nominated", "reason": None}
                    ],
                },
            )
        )

        data = json.loads(
            await _handle_group_invite(
                {"group": GROUP_HANDLE, "agents_to_invite": [OTHER_AGENT_ID]}
            )
        )

        assert data["entries"][0]["status"] == "nominated"
        assert data["entries"][0]["detail"] == "nominated, awaiting a vote"

    @respx.mock
    async def test_invite_a_batch_posts_agent_ids_and_reports_one_outcome_each(self):
        """A batch drops what it cannot admit and says which one.

        The singular form is untouched — same field, same body, same answer — so
        the plural form is a second entry point on one route, not a rewrite.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        route = respx.post(f"{API_URL}/groups/{GROUP_ID}/invite").mock(
            return_value=httpx.Response(
                200,
                json={
                    "invited": 1,
                    "skipped": 1,
                    "entries": [
                        {"agent_id": OTHER_AGENT_ID, "status": "invited", "reason": None},
                        {
                            "agent_id": THIRD_AGENT_ID,
                            "status": "skipped",
                            "reason": "agent_revoked",
                        },
                    ],
                },
            )
        )

        out = await _handle_group_invite(
            {"group": GROUP_HANDLE, "agents_to_invite": [OTHER_AGENT_ID, THIRD_AGENT_ID]}
        )

        body = json.loads(route.calls[0].request.content)
        assert body["agent_ids"] == [OTHER_AGENT_ID, THIRD_AGENT_ID]
        assert "agent_id" not in body
        data = json.loads(out)
        assert data["invited"] == 1
        assert data["skipped"] == 1
        assert data["entries"][1]["reason"] == "agent_revoked"
        assert data["entries"][1]["detail"].startswith("that agent's identity is revoked")

    @respx.mock
    async def test_invite_naming_both_targets_or_neither_never_reaches_the_wire(self):
        """One invitation or a batch — naming both is ambiguous and is refused here."""
        mock_token()
        mock_agents()
        route = respx.post(f"{API_URL}/groups/{GROUP_ID}/invite").mock(
            return_value=httpx.Response(200, json={"status": "pending", "request_id": None})
        )

        both = await _handle_group_invite(
            {
                "group": GROUP_HANDLE,
                "agent_to_invite": OTHER_AGENT_ID,
                "agents_to_invite": [THIRD_AGENT_ID],
            }
        )
        neither = await _handle_group_invite({"group": GROUP_HANDLE})

        assert not route.called
        for out in (both, neither):
            assert "Name exactly one of agent_to_invite" in out


class TestGroupRemove:
    @respx.mock
    async def test_remove_member_no_longer_claims_keys_were_rotated(self):
        """Nothing rotates on a removal, and this used to say it did.

        On an MLS group the SDK posts a Remove commit; on an open group there is
        no cryptographic eviction at all. ``keys_rotated: true`` described neither
        and told an operator a security property they did not have.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        route = respx.delete(f"{API_URL}/groups/{GROUP_ID}/members/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(204)
        )

        out = await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": OTHER_AGENT_ID})

        assert route.called
        data = json.loads(out)
        assert data["removed"] == OTHER_AGENT_ID
        assert "keys_rotated" not in data

    def test_remove_schema_names_the_member_the_way_the_roster_prints_it(self):
        """The one argument on this surface that demanded an unprintable id.

        ``rine_group_roster`` prints a seated member's handle and never its
        UUID, so a schema that asks for a UUID asks for something no hermes tool
        shows.
        """
        prop = SCHEMAS["rine_group_remove"]["parameters"]["properties"]["agent_id"]
        assert "rine_group_roster" in prop["description"]
        assert "handle" in prop["description"]
        assert "UUID" in prop["description"]

    @respx.mock
    async def test_remove_member_named_by_handle_deletes_that_members_row(self):
        """The handle the roster printed is enough to complete the removal.

        A member named the way this surface shows them resolves server-side, so
        removal never needs an id no hermes tool prints.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        member_payload(AGENT_ID, SELF_HANDLE, role="admin"),
                        member_payload(OTHER_AGENT_ID, OTHER_HANDLE, is_own_org=False),
                    ]
                },
            )
        )
        route = respx.delete(f"{API_URL}/groups/{GROUP_ID}/members/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(204)
        )

        data = json.loads(
            await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": OTHER_HANDLE})
        )

        assert route.called
        # Answered about the spelling the caller used, not the id it resolved to.
        assert data["removed"] == OTHER_HANDLE
        assert data["left"] is False

    @respx.mock
    async def test_remove_member_named_by_uuid_reads_no_roster(self):
        """A UUID still goes straight through: resolution costs a read only when needed."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        roster = respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        respx.delete(f"{API_URL}/groups/{GROUP_ID}/members/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(204)
        )

        await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": OTHER_AGENT_ID})

        assert not roster.called

    @respx.mock
    async def test_remove_member_named_by_a_bare_name_is_refused_and_deletes_nothing(self):
        """A group holds members from several orgs, so a bare name can mean two agents.

        The wrong guess is an irreversible eviction, so this refuses instead —
        and the refusal names where the full handle is printed. Nothing reaches
        the wire, which is the property that makes "nothing was removed" true.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        deleted = respx.delete(url__regex=rf"{API_URL}/groups/{GROUP_ID}/members/.*").mock(
            return_value=httpx.Response(204)
        )

        data = json.loads(await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": "other"}))

        assert not deleted.called
        assert "rine_group_roster" in data["error"]
        assert "Nothing was removed" in data["error"]

    @respx.mock
    async def test_remove_member_bare_name_refusal_teaches_the_shape_without_a_placeholder(self):
        """A refusal states a handle's PARTS; it never prints one to paste.

        The measured failure a pasteable example causes is that the local part
        moves with the conversation while the domain does not, so a model copies
        somebody else's org back into the argument. This refusal used to carry
        one, and it is reached by no gate — ``check-model-facing-examples.mjs``
        governs tool DESCRIPTIONS, and no suite before this one read the string.

        Driven with a group named by UUID and a member named bare, so the only
        ``@`` that could appear in the answer is one this code put there.
        """
        mock_token()
        mock_agents()

        data = json.loads(await _handle_group_remove({"group": GROUP_ID, "agent_id": "other"}))

        error = data["error"]
        assert "@" not in error
        # Everything the deleted example taught is still said, outright.
        assert "the agent's name, an at sign, and its org's domain" in error
        assert "UUID" in error
        assert "Nothing was removed" in error

    @respx.mock
    async def test_remove_member_not_seated_in_the_group_is_refused_and_deletes_nothing(self):
        """A handle nobody on the roster carries is refused before any DELETE.

        Resolving off the group's own roster is what makes this reachable: an
        agent that was invited or nominated and never joined holds no membership
        row, and the refusal says so rather than answering a 404 from the wire.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.get(f"{API_URL}/groups/{GROUP_ID}/members").mock(
            return_value=httpx.Response(
                200, json={"items": [member_payload(AGENT_ID, SELF_HANDLE, role="admin")]}
            )
        )
        deleted = respx.delete(url__regex=rf"{API_URL}/groups/{GROUP_ID}/members/.*").mock(
            return_value=httpx.Response(204)
        )

        data = json.loads(
            await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": OTHER_HANDLE})
        )

        assert not deleted.called
        assert "No seated member" in data["error"]
        assert "rine_group_requests" in data["error"]
        assert "Nothing was removed" in data["error"]

    @respx.mock
    async def test_remove_member_reports_what_this_removal_actually_did(self):
        """The SDK's flags and its sentence, composed nowhere here.

        A server that accepts a bare DELETE holds no MLS state for the group, so
        this is an open-group eviction: no commit, no epoch, and the sentence
        says what actually bounds the departed member's reach — each remaining
        member rotating on their next send.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.delete(f"{API_URL}/groups/{GROUP_ID}/members/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(204)
        )

        data = json.loads(
            await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": OTHER_AGENT_ID})
        )

        assert data["left"] is False
        assert data["encryption"] == "sender-key"
        assert data["mls_removal_epoch"] is None
        assert "no cryptographic eviction" in data["message"]
        assert "rotating their sender key on their next send" in data["message"]

    async def test_eviction_is_reported_as_a_membership_fact_not_a_ratchet_tree_one(
        self, monkeypatch
    ):
        """An evicted agent gets rine's refusal, with a remedy.

        The MLS core answers every later operation on an evicted handle with
        *"this member was removed from the group at epoch 47"* — true about a
        ratchet tree, and unusable: it names no group and offers no next action.
        The SDK translates it; this asserts the handler passes that through
        rather than flattening it into a traceback or a second wording.
        """
        from rine import GroupEvictedError
        from rine._resources.groups import AsyncGroups

        async def _evicted(*_args, **_kwargs):
            raise GroupEvictedError(GROUP_ID)

        monkeypatch.setattr(AsyncGroups, "remove_member", _evicted)
        data = json.loads(
            await _handle_group_remove({"group": GROUP_HANDLE, "agent_id": OTHER_AGENT_ID})
        )

        assert GROUP_ID in data["error"]
        assert "You were removed from group" in data["error"]
        assert "invite you again" in data["error"]
        assert "epoch 47" not in data["error"]


class TestGroupInspect:
    @respx.mock
    async def test_inspect_sender_key_group_reports_readable(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(mls_enabled=False, mls_group_id=None)]}
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[OK] sender-key group" in out
        assert "readable/postable" in out
        assert GROUP_HANDLE in out

    @respx.mock
    async def test_inspect_mls_group_reports_pq_readable(self):
        """An MLS group reports as post-quantum and fully readable/postable from here."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(mls_enabled=True, mls_group_id="mls-xyz")]}
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[OK] post-quantum MLS group" in out
        assert "readable/postable" in out

    @respx.mock
    async def test_inspect_group_when_mls_init_in_flight_reports_its_own_state(self):
        """The init window gets its own render — neither of the other two.

        ``mls_pending`` is an init in flight, not state: nothing is decryptable
        from an MLS epoch until ``mls_group_id`` latches, so the render question
        (``rine.format.group_is_mls``) stays false. But the server already
        refuses a sender-key send here and this surface's sends take the MLS
        branch, so calling the window "sender-key" says the opposite of what is
        happening. It is a normal transient state, and it is reported as one.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [group_payload(mls_enabled=True, mls_group_id=None, mls_pending=True)]
                },
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[OK] MLS group, initialising" in out
        assert "Sends from here already use MLS." in out
        assert "sender-key" not in out

    @respx.mock
    async def test_inspect_open_enrollment_group_reports_sender_key(self):
        """The case the init window must never swallow.

        An open-enrollment group carries ``mls_enabled`` as intent while running
        on sender keys by design, and no init is or will be in flight for it.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={"items": [{**group_payload(mls_enabled=True), "enrollment_policy": "open"}]},
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[OK] sender-key group" in out
        assert "MLS group" not in out

    @respx.mock
    async def test_inspect_group_created_for_mls_and_never_founded_names_this_surfaces_verb(self):
        """The fourth state: created to run MLS, ratchet tree never founded.

        Closed enrollment, ``mls_enabled`` true, no latch and no init in flight
        — field for field what an open group looks like, which is why this used
        to render as an ordinary sender-key group and the operator was never
        told the group had not got the encryption it was created for. The group
        is not frozen: the server takes the sender-key branch for it and a post
        into it is accepted, so the marker stays ``[OK]``.

        Hermes ships ``rine_group_reclaim`` and it founds an unlatched group, so
        the sentence names that tool rather than somebody else's verb. This is
        the ONE state that carries a remedy, so it is also the only place the
        "names its own verb, never nobody's" rule can be asserted — the create
        path cannot reach it.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        group_payload(mls_enabled=True, mls_group_id=None, mls_pending=False)
                    ]
                },
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[OK] sender-key group" in out
        assert "its ratchet tree was never founded" in out
        assert "Run rine_group_reclaim on it" in out
        assert "No verb on this surface founds it" not in out
        assert not re.search(r"broken|frozen|failed|cannot carry|holds no keys", out, re.I)

    @respx.mock
    async def test_inspect_never_founded_group_extends_the_ordinary_sender_key_line(self):
        """The fourth arm APPENDS to the third's sentence — it does not replace it.

        Two renders of the same tool, one ordinary sender-key group and one
        never-founded, asserted against each other: the never-founded diagnosis
        must begin with the ordinary one, so the two can never drift into
        describing the same traffic in two voices. Nothing in the source binds
        them, so this is the binding.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={"items": [{**group_payload(mls_enabled=True), "enrollment_policy": "open"}]},
            )
        )
        ordinary = json.loads(await _handle_group_inspect({"group": GROUP_HANDLE}))
        ordinary_line = ordinary["group"].splitlines()[-1]

        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(mls_enabled=True, mls_group_id=None)]}
            )
        )
        never_founded = json.loads(await _handle_group_inspect({"group": GROUP_HANDLE}))
        never_founded_line = never_founded["group"].splitlines()[-1]

        assert never_founded_line.startswith(f"{ordinary_line} ")
        assert never_founded_line != ordinary_line

    @respx.mock
    async def test_inspect_accepts_bare_group_name(self):
        """A bare name ('ops') resolves on the handle's local part — parity with invite/remove."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(name="ops", handle=GROUP_HANDLE)]}
            )
        )

        out = await _handle_group_inspect({"group": "ops"})

        assert "Not found" not in out
        assert GROUP_HANDLE in out
        assert "sender-key group" in out

    @respx.mock
    async def test_inspect_reports_the_groups_conversation_id(self):
        """The detail block names the argument ``rine_thread`` needs.

        ``rine_groups`` carries the same pointer on its rows, so the agent never
        has to inspect a group just to learn how to read it — but the group's
        own detail block is where a model looks when it has already picked one.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(conversation_id=GROUP_CONVERSATION_ID)]}
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert f"conversation_id: {GROUP_CONVERSATION_ID}" in json.loads(out)["group"]

    @respx.mock
    async def test_inspect_when_nothing_was_said_in_the_group_says_so_instead_of_none(self):
        """A group with no conversation is a healthy group, and reads as one.

        The conversation is created on the group's first post. Printing ``None``
        here would read as a broken tool; the sentence says there is nothing to
        read yet, which is a thing the model can act on.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload(conversation_id=None)]})
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        group = json.loads(out)["group"]
        assert "conversation_id: (none — nothing has been said in this group yet)" in group
        assert "conversation_id: None" not in group

    @respx.mock
    async def test_inspect_names_this_orgs_seated_agents_and_keeps_the_whole_member_count(self):
        """The detail block answers both questions, and does not confuse them.

        ``your_agents`` is which of this org's own agents may act here;
        ``members`` is still how big the group is. The first is additive — it
        replaces nothing — so a block that reports one seat and five members is
        reporting two facts, not contradicting itself.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={"items": [group_payload(member_count=5, member_agent_ids=[AGENT_ID])]},
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        group = json.loads(out)["group"]
        assert f"your_agents: {SELF_HANDLE}" in group
        assert AGENT_ID not in group
        assert "members: 5" in group

    @respx.mock
    async def test_inspect_when_no_agent_of_this_org_is_seated_says_so_instead_of_none(self):
        """A group the org is in and holds no seat in reads as exactly that.

        It is a state the agent has to be able to act on — ask a member for an
        invitation — and it is unreachable from ``members``, which counts every
        org's seats together.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload(member_agent_ids=[])]})
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        group = json.loads(out)["group"]
        assert "your_agents: (none — no agent of your org is seated in this group)" in group
        assert "your_agents: None" not in group
        assert "your_agents: []" not in group

    @respx.mock
    async def test_inspect_unknown_group_returns_group_specific_guidance(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))

        out = await _handle_group_inspect({"group": "#missing@acme.rine.network"})

        data = json.loads(out)
        # Group-specific guidance — NOT the generic NotFound branch, which names
        # rine_discover, and rine_discover searches AGENTS.
        assert "Group not found: #missing@acme.rine.network" in data["error"]
        assert "rine_discover_groups to search the public directory" in data["error"]
        assert not names_agent_discovery(data["error"])

    @respx.mock
    async def test_inspect_ambiguous_bare_name_refuses_and_names_both_groups(self):
        """Two groups answer to one bare name, so neither is picked for the caller.

        ``GET /groups`` has no ``ORDER BY``, so the matcher this replaced returned
        whichever row came first — a coin flip deciding which group an operator
        was shown.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        group_payload(handle="#ops@acme.rine.network", name="ops"),
                        group_payload(
                            group_id=OTHER_GROUP_ID,
                            handle="#ops@beta.rine.network",
                            name="ops",
                        ),
                    ]
                },
            )
        )

        out = await _handle_group_inspect({"group": "ops"})

        error = json.loads(out)["error"]
        assert "ambiguous" in error
        assert "#ops@acme.rine.network" in error
        assert "#ops@beta.rine.network" in error


class TestGroupJoin:
    @respx.mock
    async def test_join_open_group_reports_joined(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/join").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": OTHER_AGENT_ID,
                    "group_id": GROUP_ID,
                    "agent_id": OTHER_AGENT_ID,
                    "role": "member",
                    "joined_at": "2024-01-01T00:00:00Z",
                },
            )
        )

        out = await _handle_group_join({"group": GROUP_HANDLE})

        data = json.loads(out)
        assert data["joined"] is True
        assert data["status"] == "joined"
        assert data["group"] == GROUP_HANDLE

    @respx.mock
    async def test_join_gated_group_reports_pending_request(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/join").mock(
            return_value=httpx.Response(
                202,
                json={
                    "id": OTHER_AGENT_ID,
                    "group_id": GROUP_ID,
                    "agent_id": OTHER_AGENT_ID,
                    "status": "pending",
                    "created_at": "2024-01-01T00:00:00Z",
                },
            )
        )

        out = await _handle_group_join({"group": GROUP_HANDLE, "message": "let me in"})

        data = json.loads(out)
        assert data["joined"] is False
        assert data["status"] == "pending"
        assert data["request_id"] == OTHER_AGENT_ID
        assert data["detail"] == (
            "this agent applied to join and the group's electorate decides the request"
        )

    @respx.mock
    async def test_join_on_a_nomination_reports_that_it_recorded_consent(self):
        """Joining a nomination is the consent step, not an application.

        The row carries ``invited_by``, so the two pending answers are
        distinguishable — and they are different events: one this agent chose,
        one a member proposed for it.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json={"items": [group_payload()]})
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/join").mock(
            return_value=httpx.Response(
                202,
                json={
                    "id": OTHER_AGENT_ID,
                    "group_id": GROUP_ID,
                    "agent_id": OTHER_AGENT_ID,
                    "invited_by": THIRD_AGENT_ID,
                    "status": "pending",
                    "created_at": "2024-01-01T00:00:00Z",
                },
            )
        )

        data = json.loads(await _handle_group_join({"group": GROUP_HANDLE}))

        assert data["joined"] is False
        assert "recorded its consent" in data["detail"]
        assert "still has to carry the vote" in data["detail"]

    @respx.mock
    async def test_join_a_public_group_by_handle_reports_joined(self):
        """The handle ``rine_group_discover`` prints, spent without a UUID.

        MEASURED on this surface: the model called join with a handle it had
        just been shown and was refused CLIENT-SIDE — the trace carried 79
        requests and not one non-2xx, because resolution ran over the groups
        this org is already seated in, the one set a join cannot be aimed at.
        The directory is the rung that can see a group you have no relationship
        with, and it is the last one tried.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload()], "total": 1, "next_cursor": None}
            )
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/join").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": OTHER_AGENT_ID,
                    "group_id": GROUP_ID,
                    "agent_id": OTHER_AGENT_ID,
                    "role": "member",
                    "joined_at": "2024-01-01T00:00:00Z",
                },
            )
        )

        data = json.loads(await _handle_group_join({"group": GROUP_HANDLE}))

        assert data["joined"] is True
        assert data["group"] == GROUP_HANDLE

    @respx.mock
    async def test_join_unknown_group_returns_error(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        respx.get(f"{API_URL}/directory/groups").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0, "next_cursor": None})
        )

        out = await _handle_group_join({"group": "#missing@acme.rine.network"})

        error = json.loads(out)["error"]
        # Every rung is named, so the model can tell a typo from a private group
        # that never invited it — and the lead phrase is what earns this surface's
        # own discover verbs on the end of it.
        assert "a public group in the directory" in error
        assert "rine_group_discover" in error or "rine_discover_groups" in error

    @respx.mock
    async def test_join_a_bare_name_an_invitation_names_spends_that_invitation(self):
        """The nine other group tools take a bare name; join takes it too, once.

        A bare name reaches exactly one set here — the invitations already
        addressed to this agent — and never the directory. That set is bounded
        to groups that have asked for this agent, so a match in it cannot be a
        group the caller has no business in, which is what makes a reference
        carrying no org safe in front of a verb that cannot be taken back.
        """
        mock_token()
        mock_agents()
        seats = respx.get(f"{API_URL}/groups")
        directory = respx.get(f"{API_URL}/directory/groups")
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": OTHER_AGENT_ID,
                            "group_id": GROUP_ID,
                            "agent_id": OTHER_AGENT_ID,
                            "status": "invited",
                            "created_at": "2024-01-01T00:00:00Z",
                            "group_handle": GROUP_HANDLE,
                            "group_name": "ops",
                        }
                    ]
                },
            )
        )
        respx.post(f"{API_URL}/groups/{GROUP_ID}/join").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": OTHER_AGENT_ID,
                    "group_id": GROUP_ID,
                    "agent_id": OTHER_AGENT_ID,
                    "role": "member",
                    "joined_at": "2024-01-01T00:00:00Z",
                },
            )
        )

        data = json.loads(await _handle_group_join({"group": "ops"}))

        assert data["joined"] is True
        # The two rungs a bare name must not reach: this org's seats say nothing
        # about a group it is not in, and the directory is a network-wide
        # substring match with no org to tell two `ops` groups apart.
        assert not seats.called
        assert not directory.called

    @respx.mock
    async def test_join_a_bare_name_no_invitation_names_is_refused_without_widening(self):
        """A bare name nothing invited this agent to stops here, and says why.

        Deliberate and settled: the only rung that could see a
        group outside this agent's relationships is a network-wide substring
        match, and a wrong match on a verb that cannot be taken back is
        unrecoverable. The refusal says what the reference WAS read as, because
        the nine tools beside this one taught the caller to write it.
        """
        mock_token()
        mock_agents()
        seats = respx.get(f"{API_URL}/groups")
        directory = respx.get(f"{API_URL}/directory/groups")
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        error = json.loads(await _handle_group_join({"group": "ops"}))["error"]

        assert "A bare name is read as one of this agent's pending invitations" in error
        assert "Join takes a group's whole handle" in error
        assert not seats.called
        assert not directory.called

    @respx.mock
    async def test_join_a_bare_name_two_invitations_name_guesses_at_neither(self):
        """Two orgs may both call a group ``ops``, and a bare name carries no org.

        The invitations list is the one rung with nothing in the reference to
        tell two same-named groups apart, so a match count of two is not a pick.
        Both handles are printed, which is the spelling that does tell them
        apart.
        """
        mock_token()
        mock_agents()
        join = respx.post(f"{API_URL}/groups/{GROUP_ID}/join")
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": OTHER_AGENT_ID,
                            "group_id": GROUP_ID,
                            "agent_id": OTHER_AGENT_ID,
                            "status": "invited",
                            "created_at": "2024-01-01T00:00:00Z",
                            "group_handle": GROUP_HANDLE,
                            "group_name": "ops",
                        },
                        {
                            "id": THIRD_AGENT_ID,
                            "group_id": THIRD_AGENT_ID,
                            "agent_id": OTHER_AGENT_ID,
                            "status": "invited",
                            "created_at": "2024-01-01T00:00:00Z",
                            "group_handle": "#ops@other.rine.network",
                            "group_name": "ops",
                        },
                    ]
                },
            )
        )

        error = json.loads(await _handle_group_join({"group": "ops"}))["error"]

        assert "More than one of this agent's pending invitations" in error
        assert GROUP_HANDLE in error
        assert "#ops@other.rine.network" in error
        assert not join.called


class TestGroupInvites:
    @respx.mock
    async def test_invites_lists_pending_invites(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": OTHER_AGENT_ID,
                            "group_id": GROUP_ID,
                            "agent_id": OTHER_AGENT_ID,
                            "status": "invited",
                            "created_at": "2024-01-01T00:00:00Z",
                            "group_handle": GROUP_HANDLE,
                            "group_name": "ops",
                        }
                    ]
                },
            )
        )

        out = await _handle_group_invites({})

        data = json.loads(out)
        assert data["invites"] == [
            {
                "group_handle": GROUP_HANDLE,
                "group_name": "ops",
                "request_id": OTHER_AGENT_ID,
                "status": "invited",
                "detail": (
                    "an invitation this agent spends with rine_group_join, or that expires unspent"
                ),
            }
        ]

    @respx.mock
    async def test_invites_when_a_member_nominated_this_agent_then_it_names_the_consent_step(
        self,
    ):
        """This route is the only place a nominee can see its own nomination.

        A nomination is ``pending`` and the agent it names is not a member of the
        group, so nothing else reports it. The nominee's own join call is the
        consent step, so a row rendered without naming it leaves consent
        unreachable — and the row would read as an ordinary invitation.
        """
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": OTHER_AGENT_ID,
                            "group_id": GROUP_ID,
                            "agent_id": OTHER_AGENT_ID,
                            "invited_by": THIRD_AGENT_ID,
                            "status": "pending",
                            "created_at": "2024-01-01T00:00:00Z",
                            "group_handle": GROUP_HANDLE,
                            "group_name": "ops",
                        }
                    ]
                },
            )
        )

        row = json.loads(await _handle_group_invites({}))["invites"][0]

        assert row["status"] == "pending"
        assert "records this agent's consent" in row["detail"]
        assert "electorate" in row["detail"]

    @respx.mock
    async def test_invites_empty_when_none_pending(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups/invites").mock(
            return_value=httpx.Response(200, json={"items": []})
        )

        out = await _handle_group_invites({})

        assert json.loads(out)["invites"] == []
