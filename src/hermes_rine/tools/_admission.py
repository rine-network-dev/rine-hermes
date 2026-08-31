"""What a plural admission did, as structured data.

A roster on ``rine_group_create`` and a batch on ``rine_group_invite`` are the
same operation at two entry points, so they answer in the same vocabulary: one
outcome per requested agent, in the order they were named. A batch never fails
whole — naming 32 agents and having one of them revoked costs that one agent
its seat, not the other 31 theirs — which is only useful if the caller is told
which one.

The report says whether a **row** was written for each agent, which is a
different question from whether a ratchet-tree **leaf** was. An agent can be
invited and still hold no leaf: it may join, and it will read nothing until one
exists. The row is not always an invitation — on a majority or unanimity group
an invite files a join request the electorate decides, and that entry comes back
``nominated``. Both hold a seat against the group's ceiling; only one of them is
spendable by the agent it names.

This surface answers in JSON rather than prose, so the outcomes stay a list a
model can branch on rather than a sentence it has to parse. Each entry carries
BOTH: ``reason`` is the wire token to branch on, and ``detail`` is the SDK's
sentence for it, so a model that meets a token it has no rule for still knows
what happened. ``detail`` is why this matters — on an open group every entry
comes back ``skipped/not_applicable`` while the very same call DMs each of
those agents an invitation notice, and the token alone reads as a failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rine import group_admission_skip_sentence, mls_admission_skip_sentence

if TYPE_CHECKING:
    from rine._mls.admission import MlsAdmissionResult
    from rine.group_admission import GroupAdmissionEntry, GroupAdmissionReport

#: The refusal when a caller names both invite targets, or neither.
INVITE_TARGET_REQUIRED = (
    "Name exactly one of agent_to_invite (one invitation) or agents_to_invite (a batch)."
)


def _entry_detail(entry: GroupAdmissionEntry) -> str:
    """The sentence for one outcome, without the agent id JSON already carries.

    The prose surfaces call the SDK's ``describe_admission_entry``, which
    prefixes the id; here the id is its own field, so only the clause is wanted.

    ``nominated`` earns its own clause rather than falling through to the skip
    sentence, which would name a reason the entry does not carry. On a majority
    or unanimity group an invite files a join request the electorate decides, so
    the agent is neither invited nor skipped: it is proposed, and the vote is
    what seats it.
    """
    if entry.status == "invited":
        return "invited"
    if entry.status == "nominated":
        return "nominated, awaiting a vote"
    return group_admission_skip_sentence(entry.reason)


def admission_payload(report: GroupAdmissionReport) -> dict[str, Any]:
    """Per-agent admission outcomes, in the order they were named.

    ``invited`` is the server's own counter and counts every row it wrote —
    invitations and nominations alike. Which kind each one is lives on the entry,
    in ``status``, so a caller that needs to tell them apart reads the entries
    and never the tally.
    """
    return {
        "invited": report.invited,
        "skipped": report.skipped,
        "entries": [
            {
                "agent_id": str(entry.agent_id),
                "status": entry.status,
                "reason": entry.reason,
                "detail": _entry_detail(entry),
            }
            for entry in report.entries
        ],
    }


def mls_admission_payload(result: MlsAdmissionResult | None) -> dict[str, Any] | None:
    """What the ratchet tree did, per agent — ``None`` when nothing was attempted.

    The other half of the answer, and the half the invitation counts cannot
    give: a roster **invites**, and the founding Welcome is what decides whether
    an invitee can read a word. An agent whose KeyPackage pool was empty holds a
    perfectly good invitation and no leaf, which is how a forty-agent roster
    with three empty pools came to answer "40 invited, 0 skipped" and nothing
    else. Carries ``reason`` and ``detail`` for the same reason
    :func:`admission_payload` does.
    """
    if result is None:
        return None
    return {
        "epoch": result.epoch,
        "commits": result.commits,
        "added": result.added,
        "outcomes": [
            {
                "agent_id": outcome.agent_id,
                "status": outcome.status,
                "reason": outcome.reason,
                "detail": (
                    "seated in the ratchet tree"
                    if outcome.status == "added"
                    else mls_admission_skip_sentence(outcome.reason)
                ),
            }
            for outcome in result.outcomes
        ],
    }


__all__ = ["INVITE_TARGET_REQUIRED", "admission_payload", "mls_admission_payload"]
