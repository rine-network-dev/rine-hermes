"""What ``_format_error`` says a 404 means, and which tool it sends the model to.

Every tool handler renders its refusals through ``_client._format_error``, so the
remedy sentence it appends is the last thing the model reads before its next
call — and a model follows the last sentence. These tests pin the three kinds of
404 apart: an unknown **acting agent** (answered by the roster the refusal
already carries), a missing **group** (answered by the group directory), and
anything else (answered by the agent directory).

The two actor refusals are built by the SDK's own functions rather than retyped,
so a change to either lead sentence fails here instead of silently restoring a
hint that cannot answer the question.
"""

from __future__ import annotations

import pytest
from rine._actor import unknown_actor_refusal, unknown_handle_refusal
from rine._group_ref import match_group_by_local_name
from rine.errors import NotFoundError

from hermes_rine._client import _format_error

from .conftest import AGENT_ID

_AGENTS = [{"name": "ops", "handle": "ops@acme.rine.network", "id": AGENT_ID}]

#: An org that holds a seat somewhere, so its group refusals carry a roster.
_GROUPS = [{"id": AGENT_ID, "handle": "#planning@acme.rine.network", "name": "Planning"}]


class TestNotFoundRemedies:
    def test_unknown_acting_agent_404_names_no_search_verb(self):
        """Both spellings of an unknown actor answer with the roster and nothing else.

        ``rine_discover`` reads the unauthenticated, org-agnostic public
        directory, so it cannot say which of the caller's own agents it may act
        as. The list already inside the refusal is the answer.
        """
        for detail in (
            unknown_actor_refusal(_AGENTS, "logistics-bot"),
            unknown_handle_refusal(_AGENTS, "logistics-bot@acme.rine.network"),
        ):
            out = _format_error(NotFoundError(detail))
            assert "ops@acme.rine.network" in out
            assert "rine_discover" not in out

    def test_unknown_acting_agent_in_an_empty_org_ends_on_one_full_stop(self):
        """The zero-agent arm already punctuates itself; the wrapper must not do it twice.

        This arm is the whole sentence a caller reads now that no remedy verb
        follows it, so a trailing ``..`` is at the very end of the string.
        """
        out = _format_error(NotFoundError(unknown_actor_refusal([], "logistics-bot")))
        assert out == "Not found: No agent named 'logistics-bot'. No active agents in this org."

    def test_server_group_404_names_the_membership_list_before_the_public_directory(self):
        """A group 404 that carries no roster gets ``rine_groups`` first.

        The public directory answers only ``visibility == "public"``, so naming it
        alone sends a member of a PRIVATE group to a search that structurally
        cannot resolve their group. ``rine_groups`` lists every group the caller's
        credential can see, public or private, which is what a mistyped handle on
        a send almost always is. The ORDER is deliberate, so it is asserted.

        The detail here is the server's own bare literal — the one 404 that still
        arrives with nothing but the lead.
        """
        out = _format_error(NotFoundError("Group not found: ops"))
        assert out == (
            "Not found: Group not found: ops. "
            "Try rine_groups to find the right group handle, "
            "or rine_discover_groups to search the public directory."
        )
        assert out.index("rine_groups") < out.index("rine_discover_groups")

    def test_group_404_carrying_the_roster_names_only_the_public_directory(self):
        """``rine_groups`` is not offered as a remedy for a message that already ran it.

        The SDK's refusal lists the org's groups inline, so repeating the verb
        that produces that list tells the caller to fetch what it is reading.
        Only the public directory can still find something new.

        Asserted whole rather than by substring: the last roster row is the one
        place this function can put punctuation a caller would copy back into a
        handle, so what that row ends in is the assertion.
        """
        with pytest.raises(NotFoundError) as excinfo:
            match_group_by_local_name(_GROUPS, "ops")

        out = _format_error(excinfo.value)
        assert out == (
            "Not found: Group not found: ops. Name one of these groups:\n"
            "  - #planning@acme.rine.network (Planning)\n"
            "Try rine_discover_groups to search the public directory."
        )
        assert "rine_groups" not in out

    def test_group_404_in_an_org_with_no_groups_names_only_the_public_directory(self):
        """The empty arm carries no roster and still must not name ``rine_groups``.

        An org that holds a seat nowhere gets an empty list back from that verb,
        so the sentence would send the caller to a dead end. This arm is one
        line, so the remedy joins it with a space rather than a newline.
        """
        with pytest.raises(NotFoundError) as excinfo:
            match_group_by_local_name([], "ops")

        out = _format_error(excinfo.value)
        assert out == (
            "Not found: Group not found: ops. This org is in no groups yet. "
            "Try rine_discover_groups to search the public directory."
        )

    def test_other_404_still_points_at_the_agent_directory(self):
        """A handle naming somebody else's agent is exactly what the directory is for."""
        out = _format_error(NotFoundError("no agent xyz"))
        assert out == "Not found: no agent xyz. Try rine_discover to find the right handle."
