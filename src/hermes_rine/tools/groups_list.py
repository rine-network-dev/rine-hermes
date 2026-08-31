"""Group-listing handlers: groups, group_roster, discover_groups.

The three read-only group queries, split from ``tools/groups.py`` because that
module is already at the file-size rule. Same house rules as its sibling: async
handlers over ``client.groups.*``, every failure returned as ``tool_error``
rather than a traceback, and the ``tools.registry`` import at module top is fine
because this file is only imported when ``register()`` runs inside a Hermes
process.

``rine_groups`` is the one that makes the rest of the group surface reachable:
every other group tool takes a handle, and until this shipped nothing on this
plugin could enumerate them — a model had to already know the name of a group it
had been invited to.

``rine_discover_groups`` is the only tool here that reaches outside this agent's
own memberships, and it is unauthenticated: it reads the public directory, which
lists every ``public``-visibility group on the server across every org. It
exposes no roster and no private group, and finding a group is not joining one.

🔴 The two authenticated reads here hand the NAMED actor value straight to the
SDK and never resolve it up front, which is what keeps the one-agent warning off
their results. Both are authorised for the calling ORG — ``GET /groups``
and ``GET /groups/{id}/members`` — and ``agent`` narrows neither answer, so
nothing was ignored and no agent "acted": a warning saying which agent acted
would attribute an org-wide answer to one of them and invite a retry that
changes no byte of it. The three verbs that DO act as one agent are elsewhere.
"""

from __future__ import annotations

from typing import Any

from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, acting_agent, build_client
from hermes_rine._format_groups import render_discover_groups, render_groups, render_roster


async def _handle_groups(args: dict[str, Any], **_: Any) -> str:
    """List the groups the calling org's agents belong to (``rine_groups``)."""
    try:
        acting = acting_agent(args)
        async with build_client(agent=acting) as client:
            groups = await client.groups.list(agent=acting)
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    return tool_result(groups=render_groups(groups), count=len(groups))


async def _handle_group_roster(args: dict[str, Any], **_: Any) -> str:
    """List a group's seated members (``rine_group_roster``).

    Seated members only. An agent that was invited or nominated and has not
    joined holds no membership row, so it is absent here while still holding a
    ratchet-tree seat against the group's ceiling — ``rine_group_requests`` is
    what lists those, and reporting the two populations from one tool is how a
    model comes to read a group as smaller than it is.
    """
    try:
        acting = acting_agent(args)
        async with build_client(agent=acting) as client:
            members = await client.groups.members(args["group"], agent=acting)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(group=args["group"], members=render_roster(members), count=len(members))


async def _handle_discover_groups(args: dict[str, Any], **_: Any) -> str:
    """Search the public group directory, no auth (``rine_discover_groups``).

    ``next_cursor`` is reported whenever the server sent one: the directory is
    server-wide, so a query that matches more than one page is the ordinary
    case, and a page rendered without saying more exists reads as the whole
    answer.
    """
    try:
        async with build_client() as client:
            page = await client.discover_groups(
                q=args.get("q"),
                limit=int(args.get("limit", 20)),
                cursor=args.get("cursor"),
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    groups = list(page)
    return tool_result(
        groups=render_discover_groups(groups),
        count=len(groups),
        next_cursor=page.next_cursor,
    )


__all__ = ["_handle_discover_groups", "_handle_group_roster", "_handle_groups"]
