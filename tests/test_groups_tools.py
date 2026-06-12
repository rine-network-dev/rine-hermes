"""Behavioral tests for the group handlers (SPEC §5 "Tools").

Covers create (sender-key, single-call with description, mls_enabled forced False on
the wire), invite, remove (key rotation), and inspect's sender-key-vs-MLS
self-diagnosis line. Only the rine HTTP boundary is mocked (respx); the SDK is real.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from hermes_rine.tools.groups import (
    _handle_group_create,
    _handle_group_inspect,
    _handle_group_invite,
    _handle_group_remove,
)

from .conftest import (
    API_URL,
    GROUP_HANDLE,
    GROUP_ID,
    OTHER_AGENT_ID,
    group_payload,
    mock_agents,
    mock_token,
)


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL (see messaging tests)."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


class TestGroupCreate:
    @respx.mock
    async def test_create_returns_sender_key_confirmation(self):
        mock_token()
        mock_agents()
        respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        out = await _handle_group_create({"name": "ops"})

        data = json.loads(out)
        assert data["created"] is True
        assert data["handle"] == GROUP_HANDLE
        assert data["encryption"] == "sender-key"

    @respx.mock
    async def test_create_forces_mls_disabled_on_the_wire(self):
        mock_token()
        mock_agents()
        route = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        await _handle_group_create({"name": "ops"})

        body = json.loads(route.calls[0].request.content)
        assert body["mls_enabled"] is False

    @respx.mock
    async def test_create_with_description_is_a_single_call(self):
        """description rides the create body — no follow-up groups.update PATCH."""
        mock_token()
        mock_agents()
        create = respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json=group_payload(name="ops", description="war room")
            )
        )
        update = respx.patch(f"{API_URL}/groups/{GROUP_ID}").mock(
            return_value=httpx.Response(200, json=group_payload(name="ops"))
        )

        out = await _handle_group_create({"name": "ops", "description": "war room"})

        body = json.loads(create.calls[0].request.content)
        assert body["description"] == "war room"
        assert not update.called
        assert json.loads(out)["created"] is True

    @respx.mock
    async def test_create_name_conflict_returns_clear_error(self):
        mock_token()
        mock_agents()
        respx.post(f"{API_URL}/groups").mock(
            return_value=httpx.Response(409, json={"detail": "group name taken"})
        )

        out = await _handle_group_create({"name": "ops"})

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


class TestGroupRemove:
    @respx.mock
    async def test_remove_member_reports_key_rotation(self):
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
        assert data["keys_rotated"] is True


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
    async def test_inspect_mls_group_reports_unreadable_wall(self):
        """An MLS group surfaces the honest ceiling — never a false 'readable'."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200, json={"items": [group_payload(mls_enabled=True, mls_group_id="mls-xyz")]}
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[WARN] MLS group" in out
        assert "cannot read or post" in out

    @respx.mock
    async def test_inspect_mls_pending_group_latches_as_mls(self):
        """The brief mls_pending init window also latches as MLS (self-diagnosis)."""
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        group_payload(mls_enabled=False, mls_group_id=None, mls_pending=True)
                    ]
                },
            )
        )

        out = await _handle_group_inspect({"group": GROUP_HANDLE})

        assert "[WARN] MLS group" in out

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
    async def test_inspect_unknown_group_returns_group_specific_guidance(self):
        mock_token()
        mock_agents()
        respx.get(f"{API_URL}/groups").mock(return_value=httpx.Response(200, json={"items": []}))

        out = await _handle_group_inspect({"group": "#missing@acme.rine.network"})

        data = json.loads(out)
        # Group-specific guidance — NOT the generic NotFound branch that suggests
        # rine_discover (which searches agents, not groups).
        assert "No group '#missing@acme.rine.network'" in data["error"]
        assert "rine_group_create" in data["error"]
        assert "rine_discover" not in data["error"]
