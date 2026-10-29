"""Shared config/auth resolver for hermes-rine.

One place every tool + the adapter get their async ``RineClient`` from. The
resolution chain is the SDK's, untouched: ``RINE_CONFIG_DIR`` env → ``~/.config/rine``
→ ``cwd/.rine``, the first directory holding a ``profiles.json``. We do **not**
reimplement that chain — the SDK's ``RineClient`` resolves it when ``config_dir`` /
``api_url`` / ``agent`` are left at their defaults. This module only:

- declares the chain via ``RINE_ENV_VARS`` (metadata for the operator);
- builds an async ``RineClient`` lazily, passing only non-``None`` overrides;
- maps typed ``RineError``s to clear LLM-facing strings (``_format_error``).

Side-effect-free import: nothing here touches the network, reads credentials, or
constructs a client at import time. Unlike the crewai sibling there is no ``BaseTool``
base — Hermes tools are plain async functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rine import (
    GROUP_ROSTER_LEAD,
    NO_GROUPS_IN_ORG,
    AuthenticationError,
    ConfigError,
    NotFoundError,
    RateLimitError,
    RineApiError,
    RineClient,
    RineError,
    WhoAmI,
    is_not_a_member,
    not_a_member_message,
    single_actor_value,
)


@dataclass(frozen=True)
class RineEnvVar:
    """Metadata describing one env var that feeds the SDK's resolution chain."""

    name: str
    description: str
    required: bool = False


# Declared so operators see which vars feed the SDK's resolution chain. All
# optional — the runtime dir may come from any rung. There is no client id or secret to
# set: an agent signs in with a key it holds in that dir. NO
# RINE_TOKEN: the Python SDK has no such concept (Node/MCP-only); never accept it.
RINE_ENV_VARS: list[RineEnvVar] = [
    RineEnvVar(
        name="RINE_CONFIG_DIR",
        description="Override the Rine config dir (default ~/.config/rine then ./.rine).",
    ),
    RineEnvVar(
        name="RINE_API_URL",
        description="Rine API base URL (default https://rine.network).",
    ),
    RineEnvVar(
        name="RINE_AGENT",
        description=(
            "The agent this deployment acts as (name, handle or UUID). A config dir "
            "holds one agent and an agent acts only as itself, so this is never needed "
            "to choose; naming any other agent is refused."
        ),
    ),
]


def build_client(
    *,
    config_dir: str | None = None,
    api_url: str | None = None,
    agent: str | None = None,
) -> RineClient:
    """Build an async ``RineClient``, passing only the non-``None`` overrides.

    Any override left ``None`` is omitted so the SDK resolves it via its own chain
    (``RINE_CONFIG_DIR`` → ``~/.config/rine`` → ``cwd/.rine``). Shared by
    every tool handler and the adapter — one contract, lazily invoked.
    """
    kwargs: dict[str, Any] = {"agent_only": True}
    if config_dir is not None:
        kwargs["config_dir"] = config_dir
    if api_url is not None:
        kwargs["api_url"] = api_url
    if agent is not None:
        kwargs["agent"] = agent
    return RineClient(**kwargs)


def acting_agent(args: dict[str, Any]) -> str | None:
    """The agent a tool call NAMED to act as, or ``None`` to let the SDK choose.

    Every authenticated group tool and ``rine_send`` carries the optional ``agent``
    property (``_schemas_base._ACTING_AGENT``); the same value the two payment tools
    have always taken. Fourteen handlers reading ``args`` by hand would be the same
    line fourteen times, and one of them would eventually read a different key.

    ``as`` is accepted as an undeclared alias for ``agent`` — undeclared on purpose:
    the model is shown one spelling, and a model that has read older prose (or a
    handwritten call) still lands. The two are the **same rung**, so naming both
    with values that disagree refuses rather than letting one win; the refusal is a
    ``ConfigError``, raised inside each handler's ``try`` so it reaches the model as
    a tool message like every other failure here.

    An omitted or empty ``agent`` returns ``None``, which is what makes this purely
    additive: ``None`` is passed to :func:`build_client` (which drops it, leaving the
    SDK's ``NOT_GIVEN`` default) and to the SDK call (whose own default is ``None``),
    so a single-agent org's request is byte-for-byte the one it sent before.

    🔴 This returns what the caller **wrote**, never what it resolves to.
    :func:`settle_actor` is what turns it into a UUID and a warning; every verb
    that acts as one agent goes through that pair, and the three key-scoped
    group reads deliberately do not.

    The value still goes to :func:`build_client` as well, which is not a
    duplicate — the constructor one is what the SDK's ``_resolve_agent_id`` falls
    back to on the MLS and decrypt paths, which never see a per-call argument.
    crewai and langchain pass both, for the same reason.
    """
    return single_actor_value(args.get("agent"), args.get("as"))


async def settle_actor(client: RineClient, named: str | None) -> tuple[str | None, dict[str, str]]:
    """Settle what :func:`acting_agent` read into the UUID to act as, and a note.

    Returns the ``agent=`` a verb should pass down, and the fields its
    ``tool_result`` should splat — ``{}`` on every call that needs no comment, so
    a correct call is never complained at.

    This is the whole of that behaviour here. An ``agent`` naming nobody, on an
    org holding one agent, resolves to that agent instead of refusing; the
    ignored value is reported rather than swallowed. The fallback itself is
    minted deep inside the SDK, where the only channel left is a
    ``logger.warning`` an operator reads and a model never does — so the verb
    settles it *here*, while ``tool_result`` is still in reach, and hands the
    resolved UUID down so the inner path short-circuits and cannot mint the same
    warning twice.

    🔴 Nothing named, nothing settled. A call that left ``agent`` out gets
    ``(None, {})`` without touching the network: no value was ignored, so there
    is no warning to carry, and the SDK resolves the sole agent itself exactly as
    it did before. Resolving unconditionally would be worse than useless — it
    puts a ``GET /agents`` in front of refusals that are currently local and
    free, so ``rine_send_and_wait`` addressed to a group would answer *"Rine auth
    failed"* instead of *"rine_send_and_wait is 1:1 only; use rine_send for
    groups."*
    A surface that degrades its own clearest refusal to buy a warning nobody
    needed has made the measurement worse, not better.

    🔴 The three key-scoped group reads — ``rine_groups``, ``rine_group_roster``
    and ``rine_group_inspect`` — do not call this at all; they pass the raw
    :func:`acting_agent` value straight down. They are authorised for the
    calling key and ``agent`` narrows nothing they return, so nothing was
    ignored and no agent "acted": a warning there would attribute a key-wide answer to one
    agent and invite a retry that changes no byte of it.
    """
    if not named:
        return None, {}
    actor = await client.resolve_actor(named)
    return actor.agent_id, actor.note_fields()


#: The lead sentences ``rine`` opens an unknown **acting agent** refusal with
#: (``rine._actor.unknown_actor_refusal`` and ``unknown_handle_refusal`` — the
#: only two, and reached from the actor rung alone).
_UNKNOWN_ACTOR_LEADS = ("No agent named '", "No agent with handle '")

#: The lead the server opens EVERY group 404 with, restated here to branch on.
#:
#: Hand-typed on both sides — the server raises it as a bare literal from four
#: modules and no constant — so ``tests/test_client-server-contract_group0.py``
#: pins this copy against those, the way ``NOT_A_MEMBER_CLAUSE`` is pinned. A
#: reword there with no gate here silently sends every group 404 on five
#: published surfaces back to the AGENT directory, with every suite green.
_GROUP_NOT_FOUND_LEAD = "Group not found"

#: The SECOND lead, and the one a BARE recipient name now carries.
#:
#: The bare-name miss stopped leading with a group verdict — by the time the SDK
#: refuses, the agent namespace has declined the spelling on syntax and no agent
#: of this org carries the name, so both halves have answered no and the
#: sentence now says so. The REMEDY below is unchanged, and that is the whole
#: reason this constant exists here: dropping the first lead without adding this
#: one sends every bare-name miss down the final ``return`` of
#: :func:`_not_found_message`, which names ``rine_discover`` — the public AGENT
#: directory, which cannot resolve a group handle. That is the old misdirection
#: restored, with every suite green.
#:
#: 🔴 Matched by CONTAINMENT, not ``startswith``: the sentence OPENS with the
#: caller's own spelling (``'kofi' names neither …``), which is the point of the
#: reword. Pinned against ``rine-core``, ``rine-sdk`` and the four other reading
#: surfaces by ``NEITHER_NAMESPACE_LEAD_COPIES``
#: (``tests/helpers_client_paths.py``).
_NEITHER_NAMESPACE_LEAD = "names neither an agent nor a group you can reach"

#: What ``rine_send_and_wait`` answers a group target with.
#:
#: The SDK raises ``UnsupportedTargetError`` before any request — ``POST
#: /messages/sync`` is 1:1 only — so this sentence is the whole result of the
#: turn and the server never sees the send. That locality is the reason
#: :func:`settle_actor` touches nothing when no agent was named, spelled out in
#: its own docstring above.
#:
#: 🔴 It names ``rine_send_and_wait``, the id this surface registers, and
#: ``rine_send``, the one that can carry the message. A model reads a refusal
#: literally and calls the verb the sentence names, so a bare ``send_and_wait``
#: here spends the next turn on a tool-not-found.
#:
#: A constant rather than a literal for the reason the two leads above are one:
#: five published surfaces say this sentence, nothing on the wire carries it,
#: and a reword on one of them fails in no suite. Pinned against the other four
#: by ``GROUP_ON_WAIT_MESSAGE_COPIES`` (``tests/helpers_client_paths.py``).
_GROUP_ON_WAIT_MESSAGE = "rine_send_and_wait is 1:1 only; use rine_send for groups."


def _not_found_message(detail: str) -> str:
    """A 404, plus the tool that can answer it — and no verb when none can.

    An unknown **acting agent** needs no search: the refusal already lists this
    org's agents by name, and that list is the answer. ``rine_discover`` reads
    the unauthenticated, org-agnostic public directory, so it cannot say which of
    the caller's own agents it may act as. A missing **group** is handed to
    ``rine_groups`` first: it lists every group the key can see, public
    and private alike, and a mistyped handle on a send is almost always a group
    the caller is already in. ``rine_discover_groups`` is named second because it
    searches the public directory only — the one place that finds a public group
    this org has never joined, and the one place a private group can never
    appear. Anything else names somebody else's agent, which is what
    ``rine_discover`` is for.

    ``rine_groups`` is dropped when the refusal already carries that verb's own
    answer. The SDK lists the groups the key can see inside its group refusals —
    :data:`rine.GROUP_ROSTER_LEAD` introduces the roster, and
    :data:`rine.NO_GROUPS_IN_ORG` is the arm for an org that holds a seat
    nowhere — so naming ``rine_groups`` underneath one of those tells a caller
    to go and fetch what it is already reading. That is the ``rine_discover``
    defect inverted, and it costs more here than a wasted call: models were
    measured parroting this hint back as their answer instead of acting on it.
    ``rine_discover_groups`` is then the only verb that can still find something
    new, so it is the only one named. A **server** group 404 carries no roster
    and keeps both verbs. The clause is recognised by the SDK's own exported
    constants rather than a retyped copy, so a reword there cannot leave a stale
    literal here quietly restoring the loop.

    The lead is punctuated once, not twice: the zero-agent arm of an actor
    refusal already ends in a full stop (*"No active agents in this org."*), and
    a second one is what a caller reads at the very end of the sentence. The
    roster arm is the one detail that is punctuated **not at all**: it ends on a
    list row, and a full stop there belongs to that row rather than to the
    sentence.
    """
    lead = f"Not found: {detail.rstrip('.')}."
    if detail.startswith(_UNKNOWN_ACTOR_LEADS):
        return lead
    if detail.startswith(_GROUP_NOT_FOUND_LEAD) or _NEITHER_NAMESPACE_LEAD in detail:
        search_the_directory = "Try rine_discover_groups to search the public directory."
        if GROUP_ROSTER_LEAD in detail:
            # The roster arm ends on a LIST ROW, not on a sentence. A clause
            # appended with a space reads as part of that last group's entry —
            # and so does ``lead``'s own full stop, which ``rstrip`` cannot take
            # back off a row ending in ``)``. ``#eng@acme.rine.network (Eng).``
            # is a group nobody can name back, so neither is appended here.
            return f"Not found: {detail}\n{search_the_directory}"
        if NO_GROUPS_IN_ORG in detail:
            return f"{lead} {search_the_directory}"
        return (
            f"{lead} Try rine_groups to find the right group handle, "
            "or rine_discover_groups to search the public directory."
        )
    return f"{lead} Try rine_discover to find the right handle."


def live_handles(me: WhoAmI) -> list[str]:
    """Every non-revoked agent handle on this identity, in the server's own order.

    A revoked agent still appears in ``whoami`` — it existed and its messages did — so
    every caller that means "an agent this org can act as" has to filter, and the filter
    had been written out four times.
    """
    return [a.handle for a in me.agents if a.revoked_at is None]


def primary_handle(me: WhoAmI) -> str:
    """The handle this plugin keys per-agent local state by: the first live agent, or "".

    🔴 Three places have to arrive at the same string and none of them can see the
    others: ``RineAdapter.connect`` writes the consumed-id journal and the set-aside rows
    under it, ``rine_retry_unreadable`` reads those rows back, and ``rine doctor`` reports
    on them. A different convention in any one of them lists an EMPTY set-aside list on a
    perfectly healthy install — which reads as "nothing was ever parked", not as a bug —
    so the convention is one function rather than three agreeing copies.

    Empty string, not None, because it is a sqlite key: the store would take ``None`` and
    key a whole parallel set of rows nothing ever reads back.
    """
    live = live_handles(me)
    return live[0] if live else ""


def _format_error(err: Exception) -> str:
    """Map a Rine error to a clear, LLM-facing string.

    Tool handlers return ``tool_error(_format_error(err))`` so the agent retries or
    continues rather than seeing a raw traceback. Never leaks ciphertext. Anything
    without a branch falls through to ``str(err)``, which is where a crypto failure such
    as ``NoMlsGroupStateError`` lands: the SDK's own message already tells the agent what
    to do about it, so restating it here would only give the two places to drift apart.
    The 404 arm defers to :func:`_not_found_message`, which reads *what* was not
    found before naming a remedy.

    The one refusal that is not forwarded whole is the server's "this agent holds
    no seat in that group". It names the sibling to act as — which nothing here
    could work out — and then tells the caller to set ``X-Rine-Agent``, which is
    the wire spelling of a choice this surface makes with its ``agent`` argument
    or ``RINE_AGENT``; the SDK writes the header itself. The SDK's own sentence
    is appended rather than substituted, so the half only the server knows
    survives and the half only this stack knows gets said.
    """
    if isinstance(err, AuthenticationError):
        return (
            "Rine auth failed — onboard (`python -m hermes_rine.onboard`). "
            "A host clock more than 30 s off also fails sign-in."
        )
    if isinstance(err, NotFoundError):
        return _not_found_message(err.detail)
    if isinstance(err, RateLimitError):
        return f"Rate-limited; retry after {err.retry_after}s."
    if isinstance(err, ConfigError):
        return str(err)
    if isinstance(err, RineApiError) and is_not_a_member(err.detail):
        return f"{err.status}: {not_a_member_message(err.detail)}"
    if isinstance(err, RineApiError):
        return f"{err.status}: {err.detail}"
    return str(err)


__all__ = [
    "RINE_ENV_VARS",
    "RineEnvVar",
    "RineError",
    "_format_error",
    "acting_agent",
    "build_client",
    "live_handles",
    "primary_handle",
    "settle_actor",
]
