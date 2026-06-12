"""Behavioral tests for the discovery + identity handlers (SPEC §5 "Tools").

``rine_discover``/``rine_inspect`` hit the UNAUTHENTICATED directory endpoints (no
OAuth token fetched — asserted by NOT mocking ``/oauth/token`` and confirming the
calls succeed with no Authorization header). ``rine_whoami`` confirms the agent's own
identity (authenticated). Only the rine HTTP boundary is mocked (respx).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from hermes_rine.tools.discovery import _handle_discover, _handle_inspect, _handle_whoami

from .conftest import API_URL, OTHER_AGENT_ID, mock_agents, mock_org, mock_token


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK chain at the test config dir + mock API URL (see messaging tests)."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


class TestDiscover:
    @respx.mock
    async def test_discover_with_matches_returns_numbered_list_unauthenticated(self):
        route = respx.get(f"{API_URL}/directory/agents").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": OTHER_AGENT_ID,
                            "name": "Translator",
                            "handle": "translator@acme.rine.network",
                            "category": "language",
                            "verified": True,
                            "trust_tier": 2,
                            "description": "Translates documents.",
                        }
                    ],
                    "total": 1,
                },
            )
        )

        out = await _handle_discover({"q": "translate"})

        assert route.called
        assert "1 agent(s) found" in out
        assert "translator@acme.rine.network" in out
        # Unauthenticated: the discover GET carries no Authorization header.
        assert "authorization" not in {k.lower() for k in route.calls[0].request.headers}

    @respx.mock
    async def test_discover_unverified_agent_renders_unverified_not_verified(self):
        respx.get(f"{API_URL}/directory/agents").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": OTHER_AGENT_ID,
                            "name": "Sketchy",
                            "handle": "sketchy@acme.rine.network",
                            "verified": False,
                            "trust_tier": None,
                        }
                    ],
                    "total": 1,
                },
            )
        )

        out = await _handle_discover({"q": "x"})

        assert "unverified)" in out
        assert ", verified)" not in out
        assert "trust_tier=" not in out
        assert "category=" not in out

    @respx.mock
    async def test_discover_no_matches_returns_empty_state(self):
        respx.get(f"{API_URL}/directory/agents").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        out = await _handle_discover({"q": "nothing"})

        assert "No agents matched." in out

    @respx.mock
    async def test_discover_forwards_filters_as_query_params(self):
        route = respx.get(f"{API_URL}/directory/agents").mock(
            return_value=httpx.Response(200, json={"items": [], "total": 0})
        )

        await _handle_discover({"category": "finance", "verified": True, "limit": 5})

        query = route.calls[0].request.url.query
        assert b"category=finance" in query
        assert b"limit=5" in query


class TestInspect:
    @respx.mock
    async def test_inspect_existing_agent_returns_profile_unauthenticated(self):
        route = respx.get(f"{API_URL}/directory/agents/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": OTHER_AGENT_ID,
                    "name": "Translator",
                    "handle": "translator@acme.rine.network",
                    "description": "Translates documents.",
                    "category": "language",
                    "verified": True,
                    "human_oversight": True,
                    "trust_tier": 2,
                },
            )
        )

        out = await _handle_inspect({"handle_or_id": OTHER_AGENT_ID})

        assert "handle: translator@acme.rine.network" in out
        assert "name: Translator" in out
        assert "category: language" in out
        assert "verified: True" in out
        # No OAuth token was mocked; the public endpoint still succeeded.
        assert "authorization" not in {k.lower() for k in route.calls[0].request.headers}

    @respx.mock
    async def test_inspect_unverified_renders_false_and_none_placeholders(self):
        respx.get(f"{API_URL}/directory/agents/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": OTHER_AGENT_ID,
                    "name": "Sketchy",
                    "handle": "sketchy@acme.rine.network",
                    "verified": False,
                    "human_oversight": True,
                },
            )
        )

        out = await _handle_inspect({"handle_or_id": OTHER_AGENT_ID})

        assert "verified: False" in out
        assert "verified: True" not in out
        assert "description: (none)" in out
        assert "category: (none)" in out
        assert "trust_tier: (none)" in out

    @respx.mock
    async def test_inspect_missing_returns_not_found_string(self):
        respx.get(f"{API_URL}/directory/agents/{OTHER_AGENT_ID}").mock(
            return_value=httpx.Response(404, json={"detail": "no such agent"})
        )

        out = await _handle_inspect({"handle_or_id": OTHER_AGENT_ID})

        data = json.loads(out)
        assert "Not found" in data["error"]
        assert "no such agent" in data["error"]


class TestWhoami:
    @respx.mock
    async def test_whoami_returns_org_and_live_handles(self):
        mock_token()
        mock_agents()
        mock_org()

        out = await _handle_whoami({})

        data = json.loads(out)
        assert data["org"] == "My Org"
        assert data["org_slug"] == "myorg"
        assert data["trust_tier"] == 2
        assert "test-agent@myorg.rine.network" in data["handles"]

    @respx.mock
    async def test_whoami_auth_failure_returns_actionable_error(self):
        respx.post(f"{API_URL}/oauth/token").mock(
            return_value=httpx.Response(401, json={"detail": "bad creds"})
        )
        respx.get(f"{API_URL}/agents").mock(
            return_value=httpx.Response(401, json={"detail": "unauthorized"})
        )

        out = await _handle_whoami({})

        data = json.loads(out)
        assert "error" in data
        # The handler must return a tool_error string, never raise a traceback.
        assert isinstance(out, str)
