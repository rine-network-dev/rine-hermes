"""The schema builders both ``_schemas*`` modules share.

``_schemas.py`` was the only place these lived until the group-listing tools
needed a second schema module of their own (the first one is already far past
the file-size rule). Importing them back out of ``_schemas.py`` would make the
two modules import each other, so the builders moved down here instead: one
definition, no cycle, and the tool contracts on either side still read as a
table. Pure, declarative, side-effect-free.
"""

from __future__ import annotations

from typing import Any

from rine import JOIN_REFERENCE_RULE

#: Appended to every tool description. The schemas are what the model reads
#: first, and the skill is where the full usage lives.
_LOAD_SKILL = " For full usage, load skill rine:rine."


def _p(typ: str, desc: str, **extra: Any) -> dict[str, Any]:
    """Build one JSON-schema property (type + description + optional bounds/default)."""
    return {"type": typ, "description": desc, **extra}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a JSON-schema ``object`` parameters block."""
    return {"type": "object", "properties": props, "required": required}


def _schema(
    name: str, description: str, props: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    """Build one OpenAI-function tool schema."""
    return {
        "name": name,
        "description": description + _LOAD_SKILL,
        "parameters": _obj(props, required),
    }


#: How every group-scoped tool names its subject.
#:
#: 🔴 It names the BARE NAME too, because the SDK resolves one
#: (``_resolve_group_async`` → ``GET /groups``, matched on local part) and
#: because the sentence six lines above it on ``rine_thread``
#: (``TRANSCRIPT_REFERENCE_REQUIRED``, from ``rine.thread_words``) already
#: promises "a group handle, name or UUID". A schema that understates what the
#: runtime accepts, in the same tool contract that overstates it, teaches a
#: model to spend a hop it did not need — and this is the surface the group-QoL
#: harness drives.
_GROUP_REF = _p(
    "string",
    "Group handle, bare name, or UUID. Examples: #logistics@acme.rine.network, or just logistics.",
)

#: How ``rine_group_join`` names its subject, and the one group tool that is not
#: :data:`_GROUP_REF`.
#:
#: 🔴 **The rule itself is the SDK's, imported and never retyped.** ``join``'s
#: reference rules are not the shared ones — a handle walks four rungs, and a
#: bare name is read against this agent's pending invitations and nowhere wider,
#: deliberately refusing a network-wide search that would feed an irreversible
#: verb. All five connectors advertise the same rule, so it lives in
#: :data:`rine.JOIN_REFERENCE_RULE` on the same terms as
#: :data:`rine.VOTE_ELECTORATE_RULE`; only the surface's own discovery verb is
#: added here, because which tool prints a directory row the SDK cannot know.
_GROUP_JOIN_REF = _p(
    "string",
    f"{JOIN_REFERENCE_RULE} An id is what a rine_discover_groups row prints beside a handle.",
)

#: How every tool that acts on this org's behalf names WHICH agent is acting.
#: One constant rather than one string per contract: the group tools, ``rine_send``
#: and the two payment tools all mean the same thing by it, and two wordings for
#: one concept inside one plugin is how a model comes to fill it on one tool and
#: leave it off the next.
#:
#: The description says when it is needed because that is the whole failure mode:
#: an org with one agent never has to name it, and an org with more than one gets
#: its request refused as ambiguous if it does not. A property whose description
#: only says what it is leaves the model no way to know which case it is in.
#:
#: It carries a worked value (``kofi``) rather than a bracketed slot, because a
#: slot is what a model fills with the slot's own text: the measured failure was
#: a call that passed the literal ``"null"`` into this property. The one-agent
#: sentence is now literally true rather than merely permissive — a value that
#: names nobody no longer refuses there, the org's single agent acts, and the
#: tool result carries the warning saying so.
#:
#: "any call that acts or reads as one agent", not "every call": the three
#: key-scoped group reads carry this property for uniformity and are never refused
#: for ambiguity (``GET /groups`` and ``GET /groups/{id}/members`` authorise the
#: calling key and take no acting-agent narrowing). Each of those three says so in
#: its own description, which is where the exception belongs — naming them here
#: would put a list that goes stale inside a constant twenty-one tools share.
#: Those three carry no one-agent warning either: they answer for the whole org,
#: so nothing was narrowed and nothing was ignored, and a line saying "kofi acted"
#: over an org-wide answer describes an attribution the answer does not have.
_ACTING_AGENT = _p(
    "string",
    "Acting agent name, handle or UUID — for example kofi. Omit it while this org "
    "has a single agent: that agent acts, and a value naming anything else is "
    "ignored with a warning on the result rather than refused. Once the org holds "
    "more than one active agent, any call that acts or reads as one agent must "
    "name it or is refused as "
    "ambiguous; a tool whose own description says it answers for the whole org is "
    "never refused and is not narrowed by this. rine_whoami lists this org's agent "
    "handles, and rine_groups reports which of them hold a seat in each group "
    "(your_agents).",
)

__all__ = [
    "_ACTING_AGENT",
    "_GROUP_JOIN_REF",
    "_GROUP_REF",
    "_LOAD_SKILL",
    "_obj",
    "_p",
    "_schema",
]
