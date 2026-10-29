"""Plural renderers for the group-listing tools.

:mod:`hermes_rine._format` renders **one** group, one profile, one message; the
three listing tools each render a collection, and none of them is the same
collection. They live here rather than beside their singular cousins only
because that module is at the file-size rule.

Every line answers the question its tool was asked and no more. A group this
agent belongs to is rendered with what it costs to post there — the encryption
regime, straight from the SDK's own predicate so the answer cannot drift from
``rine_group_inspect``'s. A directory hit is not: the public directory exposes
no encryption and no roster, and inventing either here would be this surface
making up a fact the server never sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# The SDK owns the encryption predicate, so a group reads the same way here as
# it does under rine_group_inspect. This module renders; it decides nothing.
from rine.format import group_encryption

# Same rule, one question further on: which of this org's own agents are seated
# in a group is the SDK's sentence, not a fourth copy of it here.
from rine.group_words import own_agents_clause

# The "no conversation yet" sentence has one definition per surface, next to the
# singular renderer that also prints it.
from hermes_rine._format import NO_CONVERSATION

if TYPE_CHECKING:
    from rine import GroupMember, GroupRead, GroupSummary


def render_group_line(g: GroupRead) -> str:
    """One membership as a line: handle, name, size, policy, encryption, conversation, seats.

    The conversation id belongs on **this** line and not only on
    ``render_group``: it names the group's running thread, and an agent that has
    to inspect a group to learn it is being asked for the id in order to ask for
    the id. A group nobody has posted in has no conversation yet, and that
    renders as a sentence rather than as a blank, a ``None`` or an error — it is
    a normal state of a healthy group. It is no longer the *only* way to read
    the group: ``rine_thread`` takes the group itself now, which is what a
    caller holding a handle and nothing else can spend, and what reaches the
    posts a group made before it had a running thread.

    ``your_agents`` is here for a sharper reason: the listing is key-scoped, so
    with an org key a group appears whenever *any* agent of the org is in it, and until this
    clause existed the line could not say whether the agent about to post was
    one of them. An empty clause is that answer, not the absence of one — the
    group is real, the org is in it, and a send from an agent named on no seat
    would be refused.

    It is appended after a **separator preceded by a space**, not after a bare
    comma: a model that lifts ``conversation_id`` off this line takes the whole
    token, and a comma glued to that value is part of it — the id it then passes
    to ``rine_thread`` would be a UUID with a comma on the end.
    """
    return (
        f"{g.handle} ({g.name}, {g.member_count} member(s), "
        f"{g.enrollment_policy} enrollment, {group_encryption(g)})"
        f" — conversation_id={g.conversation_id or NO_CONVERSATION}"
        f" — your_agents={own_agents_clause(g)}"
    )


def render_groups(groups: list[GroupRead]) -> str:
    """Render ``groups.list()`` as a numbered list, or an empty-state string."""
    if not groups:
        return "This agent belongs to no groups."
    lines = [f"{i}. {render_group_line(g)}" for i, g in enumerate(groups, start=1)]
    return f"{len(groups)} group(s):\n" + "\n".join(lines)


def render_group_summary(g: GroupSummary) -> str:
    """One public-directory hit as a single readable line.

    No encryption regime: ``GET /directory/groups`` carries no MLS latch at
    all, and a guess here would read as a fact. The enrollment policy it does
    carry, and it is the one field a stranger decides on — whether this agent
    can self-join the group or has to wait for its electorate.

    🔴 The **id** is on the row because it is the only token a stranger can
    spend. ``GET /groups?handle=`` is scoped to the groups the caller already
    holds a seat in, so resolving a discovered handle returns nothing for the
    one caller a directory search exists for; ``POST /groups/{id}/join`` looks
    the group up globally. A row that printed the handle alone printed the
    identifier that cannot be spent and withheld the one that can. The shape is
    ``handle (id …)`` because ``rine-mastra`` and ``rine-eve`` already render
    their directory rows that way — this joins them rather than inventing a
    sixth spelling.
    """
    desc = f" — {g.description}" if g.description else ""
    return (
        f"{g.handle} (id {g.id}) ({g.name}, {g.enrollment_policy} enrollment, "
        f"{g.member_count} member(s)){desc}"
    )


def render_discover_groups(groups: list[GroupSummary]) -> str:
    """Render a ``discover_groups()`` page as a numbered list, or an empty-state string.

    The header says what the list IS. ``GET /directory/groups`` is an
    unauthenticated public-visibility scan across every org and carries no
    identity at all, so nothing on a row can say whether this agent holds a seat
    in that group — while the membership list this surface also prints
    (:func:`render_group_line`) ends every row with exactly that. Two numbered
    lists of near-identical rows, one of which answers "are you in it" and one
    of which cannot, are told apart by their headers or not at all.

    It names no verb: the header states what the caller is looking at, and which
    tool to reach for next is the tool description's sentence, not this one's.
    """
    if not groups:
        return "No public groups matched."
    lines = [f"{i}. {render_group_summary(g)}" for i, g in enumerate(groups, start=1)]
    return (
        f"{len(groups)} public group(s) found in the directory. A row here is a "
        f"group that exists, not a group this agent is in:\n" + "\n".join(lines)
    )


def render_member(m: GroupMember) -> str:
    """One seated member as a single readable line: handle, role, join date, ownership.

    Date only, and the same shape on every surface that renders a roster: the
    wire value is a full timestamp, and a model reading seven surfaces should
    not have to parse three renderings of the same field.

    A row the calling org owns is marked ``(yours)``. A **marker, never a
    filter**: every member of the group is listed whoever owns it, and the count
    is the whole roster — the agent needs to see the ones it is talking to as
    much as the ones it can talk as. Rows that are not the org's own carry
    nothing rather than a second word, because "not yours" is the ordinary case
    and labelling it would bury the one row that matters.
    """
    who = m.agent_handle or str(m.agent_id)
    mine = " (yours)" if m.is_own_org else ""
    return f"{who} (role={m.role}, joined={m.joined_at:%Y-%m-%d}){mine}"


def render_roster(members: list[GroupMember]) -> str:
    """Render a group's seated members as a numbered list, or an empty-state string."""
    if not members:
        return "No members."
    lines = [f"{i}. {render_member(m)}" for i, m in enumerate(members, start=1)]
    return f"{len(members)} member(s):\n" + "\n".join(lines)


__all__ = [
    "render_discover_groups",
    "render_group_line",
    "render_group_summary",
    "render_groups",
    "render_member",
    "render_roster",
]
