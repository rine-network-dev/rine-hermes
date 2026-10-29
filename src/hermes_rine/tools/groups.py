"""Group handlers: create, invite, remove, inspect, join, invites.

Async handlers over ``client.groups.*``. Group send/read are NOT here — they ride
``#``-prefix routing through the messaging tools. New groups default to post-quantum
MLS (the X-Wing suite); ``enable_mls=false`` on create and open enrollment each put a
group on sender keys instead. Either way
this plugin reads and posts to the groups it owns; ``group_inspect`` reports which
encryption a group uses. Every handler returns ``tool_error`` on failure — never a raw
traceback. The ``tools.registry`` import is fine at module top: this file is only
imported when ``register()`` runs inside a Hermes process.

``visibility`` carries no default and never will. Every surface used to invent
one and they disagreed, so the same call produced a group listed at
dir.rine.network from one and an unlisted one from another — and the listing also
flips the member-joined signal, so the two differed in who gets told about a join
as well as in who can find the group. It is required in the tool schema, so the
model is told at the point of choice.

Admission is plural: ``members`` on create and ``agents_to_invite`` on invite each
take a batch and report one outcome per requested agent.

An invite does not always invite. On a majority- or unanimity-enrollment group it
files a join request the group's electorate decides, so the row it wrote
is a **nomination** and nobody is seated by it. A founding roster is carved out and
mints real invitations under every policy, because at founding the creator is the
only member. Each handler that answers one of those rows carries a ``detail``
clause beside the wire token for the same reason the admission entries do: the
token is what a model branches on, the clause is what it understands.
"""

from __future__ import annotations

from typing import Any

from rine import GroupRead
from rine._agent_handles import label_own_agents_async
from rine.format import describe_group_encryption, group_encryption
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, acting_agent, build_client, settle_actor
from hermes_rine._format import UNFOUNDED_REPAIR, render_group
from hermes_rine._members import resolve_member_agent_id
from hermes_rine.tools._admission import (
    INVITE_TARGET_REQUIRED,
    admission_payload,
    mls_admission_payload,
)

#: What a model writes when a boolean schema slot says "false".
#:
#: Not a parser for user input — it is the fallback for a JSON args dict that
#: reached this plugin without a validating model in front of it. Anything not
#: named here is the default, which is the MLS group.
_MLS_REFUSED_WORDS = frozenset({"false", "no", "off", "0"})


def _enable_mls_arg(raw: object) -> bool | None:
    """What the caller asked about MLS, as a real tri-state.

    🔴 The SDK gates the wire field on ``enable_mls is False`` — an IDENTITY test
    and not a truthiness one (``rine-sdk/src/rine/_resources/group_ops.py``), so
    a falsy value that is not the ``False`` singleton reaches it as *unset*, no
    ``mls_enabled`` goes on the wire, the server applies its ``True`` default,
    and the caller is handed a post-quantum MLS group after asking for a
    broadcast one — with no error anywhere. This tool is reached from a
    model-filled JSON args dict, which is the input class that produces
    ``"false"`` and ``0`` for a boolean slot, and crewai and langchain both
    coerce through a pydantic model before the SDK sees the value. Hermes is the
    one Python surface with no model in front of it, and this is that model.

    Args:
        raw: Whatever the args dict held under ``enable_mls``.

    Returns:
        ``None`` when nothing was asked — the unset case, which this plugin
        passes through rather than defaulting, because the server owns the
        default. Otherwise the boolean the caller meant.
    """
    if isinstance(raw, bool) or raw is None:
        return raw
    if isinstance(raw, str):
        word = raw.strip().lower()
        return None if not word else word not in _MLS_REFUSED_WORDS
    return bool(raw)


async def _handle_group_create(args: dict[str, Any], **_: Any) -> str:
    """Create an end-to-end-encrypted coordination group (``rine_group_create``).

    Groups are post-quantum MLS by default; the SDK founds the group's MLS state as
    part of this call. Two things skip that founding and neither is a failure:
    ``enable_mls=False``, which asks for a sender-key group outright and travels to
    the server so the row records it as one, and open enrollment — the server runs
    those on sender keys and refuses MLS there.

    ``visibility`` is required and is passed straight through: the SDK owns the
    refusal, and a second copy of that sentence here would be one more
    place for the surfaces to disagree — which is what the obligation exists to
    stop. ``members`` is a roster, and a roster **invites**: it seats nobody, so
    the group this call returns still has exactly one member.

    A founding roster mints real invitations under every enrollment policy,
    including the two where a later invite nominates instead: at
    founding the creator is the only member, so the vote would be a formality
    cast against itself, and putting the founding batch behind one is what makes
    a large group unreachable. Every roster entry therefore comes back
    ``invited``, never ``nominated``.

    ``description`` and ``vote_duration_hours`` both ride the create body, and
    neither is followed by a PATCH: a group that existed for a moment under the
    server's default vote deadline could carry a join request decided under it,
    and a house-rules description that arrives after the first arrival is not
    the rule that arrival read. ``description`` is server-visible and not
    end-to-end encrypted — the tool schema says so, and this handler passes it
    through without a second copy of that sentence.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            group = await client.groups.create(
                args["name"],
                description=args.get("description"),
                enrollment=args.get("enrollment", "closed"),
                visibility=args.get("visibility"),
                members=args.get("members"),
                vote_duration_hours=args.get("vote_duration_hours"),
                enable_mls=_enable_mls_arg(args.get("enable_mls")),
                agent=acting,
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    # The regime is read off the group the SDK returned, not guessed from the
    # enrollment policy: an MLS init that FAILED leaves a group that did not get
    # the MLS it was created for — not a group holding no keys, which is what
    # this comment used to say and what the sentence it describes used to print —
    # and the old guess reported that group as a healthy MLS one.
    roster = None if group.roster is None else admission_payload(group.roster)
    return tool_result(
        **note,
        created=True,
        handle=group.handle,
        group_id=str(group.id),
        enrollment=group.enrollment_policy,
        encryption=group_encryption(group),
        encryption_detail=describe_group_encryption(group, UNFOUNDED_REPAIR),
        roster=roster,
        mls_admission=mls_admission_payload(group.mls_admission),
    )


#: What the singular invite's answer means to the agent that CAST it. ``invited``
#: is a voucher the named agent spends; ``pending`` is a nomination that seats
#: nobody, and reporting it under a key named ``invited`` without saying so is how
#: a model comes to believe an agent is on its way in when the electorate has yet
#: to decide.
_INVITE_OUTCOME: dict[str, str] = {
    "invited": "an invitation the named agent spends by joining, or that expires unspent",
    "pending": (
        "a nomination the group's electorate decides — your invite counts as one approval, "
        "and nobody is seated until the vote carries"
    ),
}

#: The same two rows read from the OTHER side, by the agent they name. The facts
#: differ because the actions do: an invitation is spent, a nomination is
#: consented to. One map for both readers would have handed a nominee the
#: nominator's sentence and left the consent step unnamed.
_OFFER_OUTCOME: dict[str, str] = {
    "invited": "an invitation this agent spends with rine_group_join, or that expires unspent",
    "pending": (
        "a nomination a member filed for this agent — calling rine_group_join on it records "
        "this agent's consent; the group's electorate still decides whether it is seated"
    ),
}

#: A wire status neither map has a clause for. Better than silence: a model that
#: meets an unknown token at least knows this surface did not understand it.
_UNKNOWN_OUTCOME = "the server answered a status this plugin has no clause for"


async def _handle_group_invite(args: dict[str, Any], **_: Any) -> str:
    """Invite one agent, or a batch, into a group you administer (``rine_group_invite``).

    ``agent_to_invite`` and ``agents_to_invite`` are exclusive. The batch answers
    one outcome per requested agent and drops what it cannot admit rather than
    refusing everyone, so one revoked agent costs its own seat and nobody else's.

    ``status`` says which kind of row was written — an invitation on a closed
    group, a nomination on a vote-gated one — and ``detail`` says what that means
    for the agent named.
    """
    one = args.get("agent_to_invite")
    batch = args.get("agents_to_invite") or []
    if (one is None) == (not batch):
        return tool_error(INVITE_TARGET_REQUIRED)
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            if one is not None:
                result = await client.groups.invite(
                    args["group"], one, message=args.get("message"), agent=acting
                )
                return tool_result(
                    **note,
                    invited=one,
                    group=args["group"],
                    status=result.status,
                    detail=_INVITE_OUTCOME.get(result.status, _UNKNOWN_OUTCOME),
                )
            report = await client.groups.invite_many(
                args["group"], batch, message=args.get("message"), agent=acting
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        group=args["group"],
        **admission_payload(report),
        mls_admission=mls_admission_payload(getattr(report, "mls", None)),
    )


async def _handle_group_remove(args: dict[str, Any], **_: Any) -> str:
    """Remove a member from a group you administer, or leave it (``rine_group_remove``).

    Nothing is rotated here, and this used to say otherwise. On an MLS group the
    SDK posts a real Remove commit that takes the member's ratchet-tree leaf with
    it; on an open group there is no cryptographic eviction at all.

    Which of those happened is *reported*, never described in the abstract: the
    flags and ``message`` are the SDK's answer for THIS removal and this handler
    composes none of it. The true sentence differs between both group types and
    both verbs, and one string for all four was false for every open group.

    ``agent_id`` takes the handle ``rine_group_roster`` prints as readily as a
    UUID (:mod:`hermes_rine._members` says why, and does the resolving). The
    reported ``removed`` stays the spelling the caller used, so a model that
    named a handle is answered about the handle it named.
    """
    member = args["agent_id"]
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            agent_id = await resolve_member_agent_id(client, args["group"], member, agent=acting)
            result = await client.groups.remove_member(args["group"], agent_id, agent=acting)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        removed=member,
        group=args["group"],
        left=result.left,
        encryption=result.encryption,
        mls_removal_epoch=result.mls_removal_epoch,
        local_state_retired=result.local_state_retired,
        message=result.message,
    )


async def _handle_group_inspect(args: dict[str, Any], **_: Any) -> str:
    """Show a group's details + its encryption mode (``rine_group_inspect``).

    One of the three key-scoped reads, so it does NOT resolve the acting agent
    up front and carries no one-agent warning: it answers about a group any
    agent in the key's scope may be seated in, ``agent`` narrows nothing in it, and a
    line saying which agent "acted" would attribute a key-wide answer to one of
    them. The value still reaches the wire as a header for uniformity, exactly
    as it did before.

    The reference is resolved by the SDK's :meth:`RineClient.resolve_group` —
    the one entry point every group verb resolves through — and the row that
    resolve already fetched is what renders here. The resolve has already paid for
    that row; reading it again to render what it returned would pay twice for one
    answer, which is the shape :class:`rine.GroupTarget` exists to remove. The
    seated ladder sets ``row`` on every rung, the UUID one included, so there
    is no rung to fall back from.

    Two refusals moved with the resolver, and both are the contract's rather
    than this file's: an unknown reference now carries the SDK's group refusal
    (the groups your key can see, by handle, then ``rine_discover_groups``) instead of a
    sentence composed here, and a bare name answering to two groups is refused
    by name instead of resolving to whichever row ``GET /groups`` happened to
    list first.

    🔴 A row off :class:`rine.GroupTarget` is NOT a labelled group. ``groups.list``
    and ``groups.get`` mint their models and then attach the org's handle map to
    them, which is what lets ``your_agents`` print handles; a record the resolve
    handed back has been through no such step, so rendering it raw prints the
    seated agents as bare UUIDs. The label is re-attached here, through the SDK's
    own labeller rather than a second group read: the map comes off the memoised
    ``GET /agents`` this call may already have paid for, and no group is fetched
    twice. ``crewai-rine`` reaches the same function for the same reason.
    """
    try:
        acting = acting_agent(args)
        async with build_client(agent=acting) as client:
            target = await client.resolve_group(args["group"], agent=acting)
            group = GroupRead.model_validate(target.row)
            await label_own_agents_async(client, [group])
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(group=render_group(group))


async def _handle_group_join(args: dict[str, Any], **_: Any) -> str:
    """Join a group, or request to join if it is vote-gated (``rine_group_join``).

    A pending answer is one of two different events, told apart by ``invited_by``
    on the request row: this agent applied, or a member nominated it and this call
    recorded its consent. Both leave the electorate to decide, but only
    the second one was somebody else's idea, and a model that cannot tell them
    apart cannot explain to its operator why it is waiting.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            result = await client.groups.join(
                args["group"], message=args.get("message"), agent=acting
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    if result.status == "pending":
        return tool_result(
            **note,
            joined=False,
            status="pending",
            group=args["group"],
            request_id=str(result.request.id),
            detail=(
                "a member nominated this agent and this call recorded its consent; the "
                "group's electorate still has to carry the vote"
                if result.request.invited_by is not None
                else "this agent applied to join and the group's electorate decides the request"
            ),
        )
    return tool_result(**note, joined=True, status="joined", group=args["group"])


async def _handle_group_invites(args: dict[str, Any], **_: Any) -> str:
    """List the open offers addressed to this agent (``rine_group_invites``).

    Two kinds of row, told apart by ``status``: an ``invited`` row is a voucher
    this agent spends by joining, and a ``pending`` one is a nomination a member
    filed on its behalf. This route is the only place a nomination is visible to
    the agent it names, and calling ``rine_group_join`` on one is how that agent
    consents to it — so a row rendered without saying which kind it is leaves the
    consent step unreachable.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            invites = await client.groups.list_invites(agent=acting)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        **note,
        invites=[
            {
                "group_handle": inv.group_handle,
                "group_name": inv.group_name,
                "request_id": str(inv.id),
                "status": inv.status,
                "detail": _OFFER_OUTCOME.get(inv.status, _UNKNOWN_OUTCOME),
            }
            for inv in invites
        ],
    )


__all__ = [
    "_handle_group_create",
    "_handle_group_inspect",
    "_handle_group_invite",
    "_handle_group_invites",
    "_handle_group_join",
    "_handle_group_remove",
]
