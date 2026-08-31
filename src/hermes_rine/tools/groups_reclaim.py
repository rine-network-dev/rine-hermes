"""Retiring the ratchet-tree leaves a group no longer owes anyone.

An invitation takes a leaf the moment its Welcome is minted, and the reaper that
frees the **seat** when the invitation lapses does not take the leaf with it. A
self-leave leaves the same residue: MLS gives nobody a way to commit their own
removal. So a group that churns no-show invitations makes every later Welcome
and every later commit heavier for every real member, without bound.

Reclamation is what bounds the tree — expiry frees the seat only. It is never
automatic, because the server holds no MLS keys and cannot mint the Remove
commit, so a group nobody runs this on keeps accumulating leaves. Posting the
commit takes membership and no role beyond it, so any member may run it; what
holds it back is that each Remove is O(members) and billed to all of them.

The handler seats before it reclaims, which is why it runs the SDK's resume verb
with ``reclaim=True`` rather than a bare reclamation: a pass that finds a member
holding no leaf declines whole rather than reclaim a leaf somebody is entitled
to, and seating first is what clears that case instead of leaving the agent with
a refusal and no remedy.

It lives beside :mod:`hermes_rine.tools.groups_admin` rather than in it so the
three Python connectors stay the same shape file for file — a verb that exists
in two of them and hides inside a third module in the last is how a later sweep
comes to miss one.
"""

from __future__ import annotations

from typing import Any

from rine import describe_mls_admission
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, acting_agent, build_client, settle_actor


async def _handle_group_reclaim(args: dict[str, Any], **_: Any) -> str:
    """Retire the leaves a group no longer owes anyone (``rine_group_reclaim``).

    ``message`` is the SDK's own sentence and reports both halves: what seating
    achieved, and what reclamation found. A pass that ran and found nothing
    still says so — "nobody asked" and "there was nothing there" are different
    facts, and only one of them means the tree is clean.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            result = await client.groups.resume_admission(
                args["group"], reclaim=True, agent=acting
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    # ``reclamation`` is None only when the group was FOUNDED by this call: a
    # group being founded has no orphans, because every leaf in it was minted by
    # the founding commits themselves.
    pass_ = result.reclamation
    declined = None if pass_ is None or pass_.declined is None else pass_.declined.reason
    return tool_result(
        **note,
        group=args["group"],
        seated=result.added,
        epoch=result.epoch,
        orphans_found=None if pass_ is None else pass_.found,
        orphans_reclaimed=None if pass_ is None else pass_.reclaimed,
        declined=declined,
        message=describe_mls_admission(result),
    )


__all__ = ["_handle_group_reclaim"]
