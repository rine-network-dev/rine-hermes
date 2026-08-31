"""Naming the member a removal acts on — the handle the roster prints, or a UUID.

Every group-scoped tool on this plugin names its group by handle
(``_GROUP_REF``), and the SDK resolves handle, bare name and UUID for it. One
argument did not follow: ``rine_group_remove``'s ``agent_id`` demanded a UUID,
the SDK interpolates it straight into ``/groups/{id}/members/{agent_id}`` with
no resolution of its own, and ``render_member`` prints a seated member's
**handle** and never its id. So the one thing this surface printed about a
member was the one thing the removal would not take, and removing a member was
the single hermes verb an operator could not complete from what hermes itself
prints.

The fix resolves off the group's **own roster** rather than off the org's agent
list or the public directory: the roster is exactly the population a removal can
act on, it is what ``rine_group_roster`` already showed the model, and matching
there cannot pick an agent that is not in the group. It costs one
``GET /groups/{id}/members`` and only when a handle was named — a UUID still
goes straight through, unresolved and un-fetched, exactly as before.

A **bare name** is refused rather than guessed. The roster prints full handles,
handles are unique per org and not across orgs, and a group holds members from
several orgs — so a bare local part can name two different agents and the wrong
guess is an irreversible eviction.

The group argument on the same call is *resolved* from a bare name rather than
refused (``_resolve_group_sync`` in the SDK), and that asymmetry is deliberate,
not an oversight: ``/groups`` lists this org's groups, so a bare group name has
one candidate here, while a roster spans every org that holds a seat, so a bare
member name has as many candidates as there are orgs. Do not "align" the two —
the populations differ, so the safe answer does too.

Both refusals here are :class:`rine.ConfigError`, whose text ``_format_error``
forwards to the model verbatim, and neither touches the network before it fires,
so both are true when they say nothing was removed.
"""

from __future__ import annotations

from typing import Any

from rine import ConfigError, is_uuid, normalize_handle


def _bare_name_refusal(named: str, group: str) -> ConfigError:
    return ConfigError(
        f"Cannot remove '{named}' from {group}: a member is named by its full handle — "
        f"the agent's name, an at sign, and its org's domain — or by its UUID, never by a "
        f"bare name: a group holds members from several orgs, where one bare name can mean "
        f"two different agents. "
        f"rine_group_roster prints the full handle of every seated member. "
        f"Nothing was removed."
    )


def _unknown_member_refusal(named: str, group: str) -> ConfigError:
    return ConfigError(
        f"No seated member '{named}' in {group}. rine_group_roster lists the group's "
        f"seated members; an agent that was invited or nominated and has not joined "
        f"holds no membership row and cannot be removed — rine_group_requests lists "
        f"those. Nothing was removed."
    )


async def resolve_member_agent_id(
    client: Any, group: str, named: str, *, agent: str | None
) -> str:
    """The UUID of the member ``named`` in ``group``, resolving a handle off the roster.

    Args:
        client: An open async ``RineClient``.
        group: The group as the caller named it — handle or UUID, resolved by the SDK.
        named: The member, as a full handle or a UUID.
        agent: The acting agent, forwarded to the roster read.

    Returns:
        The member's agent UUID, ready for the removal path.

    Raises:
        ConfigError: When ``named`` is a bare name, or names nobody seated in
            the group. Both fire before any removal is attempted.
    """
    if is_uuid(named):
        return named
    if "@" not in named:
        raise _bare_name_refusal(named, group)
    want = normalize_handle(named).lower()
    for member in await client.groups.members(group, agent=agent):
        handle = member.agent_handle
        if handle and normalize_handle(handle).lower() == want:
            return str(member.agent_id)
    raise _unknown_member_refusal(named, group)


__all__ = ["resolve_member_agent_id"]
