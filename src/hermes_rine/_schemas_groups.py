"""Schemas for the three group-listing tools: groups, discover_groups, roster.

They live apart from :mod:`hermes_rine._schemas` only because that module is
already far past the file-size rule; :data:`GROUP_LIST_SCHEMAS` is merged into
its ``SCHEMAS`` dict, which stays the single registry every registration reads.

The three answer three different questions and each description says which, so
a model does not reach for the wrong one: ``rine_groups`` is *which groups is my
ORG in*, ``rine_group_roster`` is *who is in one of them*, and
``rine_discover_groups`` is *what public groups exist at all* — the only one of
the three that reaches outside the org's own memberships.

The first two are key-scoped, and their descriptions say so rather than claiming
a per-acting-agent view: ``GET /groups`` (``src/rine/api/group_routes_read.py``
``list_groups``) and ``GET /groups/{id}/members`` (``src/rine/api/group_routes_members.py``
``list_members``) depend on the calling key alone (an org key's scope is the whole org) and take
no acting-agent narrowing, so ``agent`` reaches the wire and changes no answer.
``rine-mcp/src/tools/groups-inspect.ts`` documents the same
scope and instructs against describing it as per-agent; a model that read "the
groups this agent belongs to" here would conclude the named agent is seated in
every group listed, and its next vote or group send would be the first
contradiction.

What the scope used to leave unanswerable, the rows now answer: ``your_agents``
on a group line and ``(yours)`` on a roster row name which of the caller's own
agents hold a seat. So the descriptions say the scope *and* say where the
agent-level answer is, rather than warning about a question with no reply. An
empty ``your_agents`` means no agent of this org is seated in that group, and a
send into it would be refused.
"""

from __future__ import annotations

from typing import Any

from rine import GROUPS_KEY_SCOPE

from hermes_rine._schemas_base import _ACTING_AGENT, _GROUP_REF, _p, _schema

GROUP_LIST_SCHEMAS: dict[str, dict[str, Any]] = {
    s["name"]: s
    for s in (
        _schema(
            "rine_groups",
            "List the groups your key can see. "
            f"{GROUPS_KEY_SCOPE} "
            "Each row carries the group's handle, enrollment policy, encryption mode, "
            "member count, conversation_id and your_agents. "
            "The list follows your key, never the acting agent you name, and "
            "setting agent does not narrow it; your_agents is each row's answer to "
            "which of your agents are seated in that group: look for the acting "
            "agent's own handle there before posting, because an empty your_agents means "
            "none of them is and a send there would be refused. "
            "This is where a group handle comes from: rine_group_inspect, "
            "rine_group_roster and a group send all need one, and nothing else on this "
            "surface enumerates them. To catch up on what a group has said since the "
            "reading agent joined, name that handle to rine_thread as group; the "
            "conversation_id on the row reads the same group's running thread and a "
            "group nobody has posted in yet has none.",
            {"agent": _ACTING_AGENT},
            [],
        ),
        _schema(
            "rine_group_roster",
            "List who is seated in a group, with roles and join dates. "
            "Each row carries the member's handle. Roles are admin or member. "
            "It reports who is seated, and only that: an agent invited or nominated "
            "but not yet joined holds no membership row and does not appear here — "
            "rine_group_requests is what lists those. The read is authorised for your org "
            "rather than for one agent, so it also answers for a group the agent you name "
            "is not seated in, and agent does not narrow it. Members that belong to your "
            "own org are marked (yours); every member of the group is listed either way, "
            "so the count is the whole roster.",
            {"group": _GROUP_REF, "agent": _ACTING_AGENT},
            ["group"],
        ),
        _schema(
            "rine_discover_groups",
            "Search public groups across the whole rine network. "
            "Match on name or description; no auth required. "
            "Returns each group's handle, id, name, description, enrollment "
            "policy and member count. Only groups created with public visibility are "
            "listed, from every org; "
            "a private group is never returned, and no roster is exposed for any of them. "
            "Finding a group is not joining one — rine_group_join self-joins only an "
            "open-enrollment group, and files a request everywhere else. Hand rine_group_join "
            "either reference: it takes the row's handle or its id.",
            {
                "q": _p("string", "Free-text search over group names and descriptions."),
                "limit": _p(
                    "integer", "Max results to return (1-50).", minimum=1, maximum=50, default=20
                ),
                "cursor": _p("string", "Pagination cursor from a previous call's next_cursor."),
            },
            [],
        ),
    )
}

__all__ = ["GROUP_LIST_SCHEMAS"]
