"""Group administration handlers: requests, vote, leave, sync.

The four verbs an agent needs once it is *inside* a group rather than getting
into one. They live beside :mod:`hermes_rine.tools.groups` rather than in it
because that module is already at the file-size limit, and they answer a
different question: who is waiting, whether to admit them, how to get out, and
how to catch up when this agent's local encryption state has fallen behind the
group's.

``rine_group_leave`` is a **distinct** tool, not a mode of ``rine_group_remove``.
A leave destroys this host's key material irreversibly; a kick acts on a third
party. One verb differentiated by one argument means a single wrong argument
turns "I'll step out" into "I'll evict a colleague". ``rine_group_remove`` keeps
its existing "name your own agent to leave" behaviour unchanged.

Every handler returns ``tool_error`` on failure — never a raw traceback — and
reports the SDK's own answer rather than composing one. The removal sentence and
the recovery sentence both differ by group type, and a surface that writes its
own is how the surfaces came to disagree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rine import describe_vote_eligibility, may_vote, vote_eligibility_reason
from rine.format import describe_mls_recovery
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, acting_agent, build_client, settle_actor
from hermes_rine._format import UNFOUNDED_REPAIR

if TYPE_CHECKING:
    from rine.types import JoinRequestRead


async def _handle_group_requests(args: dict[str, Any], **_: Any) -> str:
    """List what a group still owes an answer on (``rine_group_requests``).

    ``pending`` is the vote queue and the only population ``rine_group_vote`` can
    act on — agents that applied themselves and agents a member nominated, told
    apart by ``invited_by`` on the server's row; ``invited`` is the group's
    unaccepted invitations, which nothing else reports; ``live`` is both, and
    members plus live is the whole ratchet tree the group's seat ceiling counts.

    Each pending row carries the whole voting picture, so deciding whether to
    vote takes no second call: the members the group had when the request was
    filed, how many of them are still in it, the approvals and denials that
    count, how many more of each would decide it, and whether this agent's own
    vote would count. ``eligibility`` is the SDK's clause for that last one and
    is the same sentence every other surface prints — the fields around it are
    the machine-readable form of it.
    """
    outstanding = args.get("outstanding", "pending")
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            rows = await client.groups.list_requests(
                args["group"], outstanding=outstanding, agent=acting
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        group=args["group"],
        outstanding=outstanding,
        requests=[_request_fields(row) for row in sorted(rows, key=lambda r: r.created_at)],
    )


def _request_fields(row: JoinRequestRead) -> dict[str, Any]:
    """One outstanding request, as fields rather than as a rendered row.

    Hermes hands its model structured ``tool_result`` payloads rather than
    prose, so the row every string-returning surface prints is decomposed here
    — same facts, same vocabulary, one clause of rendered English for the fact
    that is a judgement rather than a number.

    ``you_may_vote`` is resolved rather than passed through. A server older than
    the electorate sends the field as ``null`` and lets any current member vote,
    so the raw value handed a model ``you_may_vote: null`` beside
    ``you_may_vote_reason: "electorate_not_recorded"`` and an ``eligibility`` of
    "you can vote" — a payload that contradicts itself, on the one surface whose
    reader is a language model.

    **Both bars are here, and both may be null.** ``denials_needed`` is the
    denial side of ``approvals_needed`` and moves on the same shrinking
    electorate, so a request can be refused by a denial cast when it fell short.
    A ``null`` bar means no bar is reachable — never that the number is zero; the
    two are different facts and a surface that renders one as the other tells a
    model a request can be decided when nothing can decide it.

    🔴 A field absent from this dict never reaches the model. The prose surfaces
    render the same row through the SDK and inherit a new field for free; this
    one does not.
    """
    return {
        "request_id": str(row.id),
        "agent_id": str(row.agent_id),
        "status": row.status,
        "expires_at": None if row.expires_at is None else row.expires_at.isoformat(),
        "quorum_total_members": row.quorum_total_members,
        "electorate_size": row.electorate_size,
        "approvals": row.approvals,
        "denials": row.denials,
        "approvals_needed": row.approvals_needed,
        "denials_needed": row.denials_needed,
        "your_vote": row.your_vote,
        "you_may_vote": may_vote(row),
        "you_may_vote_reason": vote_eligibility_reason(row),
        "eligibility": describe_vote_eligibility(row),
    }


#: What each answer a vote can come back with means, as a clause beside the token.
#:
#: ``invited`` is the one no client anticipated. A member may nominate an agent
#: into a vote-gated group, and the electorate approving that nomination is not
#: the same event as the agent agreeing to be in the group — so a carried vote on
#: an agent that has not consented resolves the request to a spendable invitation
#: rather than to a seat. Reporting that as "approved" would tell a model the
#: agent is in when it is not; reporting it as an unknown token would tell it
#: nothing at all. Same reason the admission entries carry ``detail``: the token
#: is what a model branches on, the clause is what it understands.
_VOTE_OUTCOME: dict[str, str] = {
    "approved": "the electorate approved this request and the applicant is now a member",
    "invited": (
        "the electorate approved this request, and the agent it names was nominated by a "
        "member rather than having applied itself, so it now holds a spendable invitation "
        "instead of a seat — it becomes a member by accepting that invitation, and the "
        "vote seats the member, which is what grants the group's keys"
    ),
    "denied": "the electorate refused this request; it is closed and takes no further votes",
    "pending": "this vote is recorded and the request is still open — neither bar is reached yet",
}


async def _handle_group_vote(args: dict[str, Any], **_: Any) -> str:
    """Vote on a pending join request (``rine_group_vote``).

    An approve that crosses the threshold admits the applicant AND mints their
    ratchet-tree leaf and Welcome inside the same SDK call — the server holds no
    MLS keys, so the deciding voter is the only client present that can. That
    holds for an agent that applied itself, and for a nominated agent that has
    already consented; a carried vote on a nomination nobody has consented to
    answers ``invited`` instead, and seats nobody. Nothing about that is composed
    here beyond ``detail``; ``status`` is the server's own answer.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            result = await client.groups.vote(
                args["group"], args["request_id"], args["vote"], agent=acting
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        group=args["group"],
        request_id=str(result.request_id),
        your_vote=result.your_vote,
        status=result.status,
        detail=_VOTE_OUTCOME.get(
            result.status, "the server answered a request status this plugin has no clause for"
        ),
    )


async def _handle_group_leave(args: dict[str, Any], **_: Any) -> str:
    """Leave a group this agent belongs to (``rine_group_leave``).

    No Remove commit is posted — MLS gives nobody a way to commit their own
    removal — so this agent's leaf stays in the ratchet tree until some member
    runs the reclamation pass. What the leave does take is this host's key
    material for the group, which is what ``message`` says in the SDK's words.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            result = await client.groups.leave(args["group"], agent=acting)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        group=args["group"],
        left=result.left,
        encryption=result.encryption,
        local_state_retired=result.local_state_retired,
        message=result.message,
    )


async def _handle_group_sync(args: dict[str, Any], **_: Any) -> str:
    """Catch this agent's group encryption state up (``rine_group_sync``).

    A rine group runs one of two encryptions, so this verb has two answers and
    they share no field beyond ``regime``. An MLS group walks an epoch ladder,
    and which rung ran is the whole report: the cheap one replays stored commits
    and posts nothing itself, the expensive one posts a single external commit
    that is O(members) and billed to every member of the group. Either rung ends
    by moving this agent's own leaf to its current key, which can post one
    path-only commit, and the report says where that leaf now stands. A
    sender-key group has no epoch chain — what a member can be missing there is
    other members' keys, waiting as distribution DMs in this agent's own inbox —
    so the report is how many were installed, and nothing was committed.

    🔴 The branch is ``regime`` and never which fields the answer carries. An
    agent reading these keys gets the ones its group's encryption actually has,
    rather than five nulls standing in for an epoch chain the group has not got.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            # `rine_group_reclaim` is this surface's spelling of the verb that
            # founds a group whose ratchet tree was never founded — it wraps the
            # SDK's `resume_admission`, which this plugin has no tool for.
            result = await client.groups.sync(args["group"], agent=acting, repair=UNFOUNDED_REPAIR)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    message = describe_mls_recovery(args["group"], result)
    if result.regime == "sender-key":
        # 🔴 Spread rather than passed, so the key is ABSENT rather than null on
        # a group that has the encryption it was created for — an agent reading
        # these keys gets the ones its group's state actually has. And it is not
        # called `warning`: `note` carries that key, and `tool_result(**note,
        # warning=...)` is a `TypeError`, not a merge.
        warns = (
            {"mls_never_founded_note": result.mls_never_founded_note}
            if result.mls_never_founded_note
            else {}
        )
        # Same absent-rather-than-null rule, and the same reason it is a field
        # and not only a sentence: an agent that branches on keys cannot tell
        # "nothing was waiting" from "the scan read a quarter of what is waiting"
        # out of `ingested: 0` alone. `rine-mcp` spreads the whole result and the
        # CLI prints it, so a projection that stops short of this key is the same
        # question answered differently by three surfaces.
        shortfall = {"unscanned": result.unscanned} if result.unscanned else {}
        return tool_result(
            **note,
            group=args["group"],
            regime=result.regime,
            ingested=result.ingested,
            retired=result.retired,
            **shortfall,
            **warns,
            message=message,
        )
    return tool_result(
        **note,
        group=args["group"],
        regime=result.regime,
        rung=result.rung,
        from_epoch=result.from_epoch,
        epoch=result.epoch,
        behind=result.behind,
        processed=result.processed,
        evicted=result.evicted,
        message=message,
    )


__all__ = [
    "_handle_group_leave",
    "_handle_group_requests",
    "_handle_group_sync",
    "_handle_group_vote",
]
