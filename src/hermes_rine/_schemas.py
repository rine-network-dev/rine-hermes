"""OpenAI-function ``dict`` schemas for the 25 ``rine_*`` tools.

Each schema is ``{"name", "description", "parameters": {json-schema}}`` — the exact
shape ``PluginContext.register_tool(schema=...)`` expects. The ``description`` here is
the canonical tool description the model sees; ``tools/__init__.py`` reuses it. Pure,
declarative data — side-effect-free. ``_p`` / ``_obj`` (from
:mod:`hermes_rine._schemas_base`) keep the repeated property and object boilerplate out
of each schema (DRY) so the contracts read as a table.

``SCHEMAS`` is the whole registry: the three group-listing contracts are written in
:mod:`hermes_rine._schemas_groups`, which this module folds in, so a registration
still has exactly one dict to read.
"""

from __future__ import annotations

from typing import Any

from rine import (
    GROUP_READERS_NOTICE,
    TRANSCRIPT_REFERENCE_REQUIRED,
    VOTE_DURATION_MAX_HOURS,
    VOTE_DURATION_MIN_HOURS,
    VOTE_DURATION_RULE,
    VOTE_ELECTORATE_RULE,
)

from hermes_rine._schemas_base import (
    _ACTING_AGENT,
    _GROUP_JOIN_REF,
    _GROUP_REF,
    _p,
    _schema,
)
from hermes_rine._schemas_groups import GROUP_LIST_SCHEMAS

_TO_GROUP = _p(
    "string",
    "Who this goes to: an agent handle or UUID, a group handle, or a group's own "
    "name. Examples: kofi@acme.rine.network for one agent; "
    "#logistics@acme.rine.network, or just logistics, for a group.",
)
_BODY = _p("string", "Message text (sent as the payload's 'text' field).")
_MTYPE_REQ = _p("string", "Rine message type.", default="rine.v1.task_request")
_MTYPE_RESP = _p("string", "Rine message type.", default="rine.v1.task_response")

SCHEMAS: dict[str, dict[str, Any]] = {
    s["name"]: s
    for s in (
        _schema(
            "rine_send",
            "Send an encrypted message to an agent or a group. "
            "Performs a real, irreversible network send. Name a group by its own name — "
            "to='logistics' — or by its full handle, to='#logistics@acme.rine.network'. "
            "Use to='kofi@acme.rine.network' for 1:1.",
            {
                "to": _TO_GROUP,
                "body": _BODY,
                "message_type": _MTYPE_REQ,
                "idempotency_key": _p(
                    "string", "Optional idempotency key to dedupe retried sends."
                ),
                "agent": _ACTING_AGENT,
            },
            ["to", "body"],
        ),
        _schema(
            "rine_send_and_wait",
            "Send to one agent (never a group) and await the reply. "
            "It blocks until the reply arrives or the timeout elapses. "
            "The delegate-and-await primitive. Performs a real network send.",
            {
                "to": _p("string", "Recipient handle/UUID (1:1 only — not a group)."),
                "body": _BODY,
                "wait_seconds": _p(
                    "integer",
                    "Seconds to block for a reply (1-300).",
                    minimum=1,
                    maximum=300,
                    default=30,
                ),
                "message_type": _MTYPE_REQ,
                "agent": _ACTING_AGENT,
            },
            ["to", "body"],
        ),
        _schema(
            "rine_inbox",
            "Read new rine mail; this marks what it reads delivered. "
            "Returns the messages' decrypted contents. Defaults to "
            "status='new' — the undelivered mail — and on that path only, marks the ones it "
            "could read delivered, so the next check returns only newer mail. Ask for "
            "'delivered', 'read' or 'all' to look back over mail this agent has already "
            "been handed; those paths mark nothing and change nothing.",
            {
                "status": _p(
                    "string",
                    "Which messages to return: 'new' (the default — undelivered mail, and "
                    "the only status that marks what it reads delivered), 'delivered', "
                    "'read', or 'all' for every status at once.",
                    enum=["new", "delivered", "read", "all"],
                    default="new",
                ),
                "limit": _p(
                    "integer",
                    "Max messages to return (1-100).",
                    minimum=1,
                    maximum=100,
                    default=20,
                ),
                "agent": _ACTING_AGENT,
            },
            [],
        ),
        _schema(
            "rine_read",
            "Fetch and decrypt a single rine message by id. The read is authorised for the "
            "ORG, so name the agent the message is addressed to: any other agent of the org "
            "is accepted and cannot decrypt it. The delivery mark follows whoever reads — "
            "naming a sibling leaves the addressee's new mail untouched.",
            {
                "message_id": _p("string", "UUID of the message to fetch and decrypt."),
                "agent": _ACTING_AGENT,
            },
            ["message_id"],
        ),
        _schema(
            "rine_reply",
            "Reply in-thread to a rine message. Performs a real, irreversible network send. "
            "The reply is filed from the agent that acts, so name the agent the original was "
            "addressed to: naming another agent of the org is refused before anything is "
            "sent, because that reply would arrive from an agent the message never reached.",
            {
                "message_id": _p("string", "UUID of the message to reply to."),
                "body": _p("string", "Reply text (sent as the payload's 'text' field)."),
                "message_type": _MTYPE_RESP,
                "agent": _ACTING_AGENT,
            },
            ["message_id", "body"],
        ),
        _schema(
            "rine_thread",
            "Fetch a conversation's or group's decrypted transcript. "
            f"Both-sided, addressed by reference. {TRANSCRIPT_REFERENCE_REQUIRED} "
            "Returns every turn (yours and the peer's) oldest-to-newest, role-tagged. "
            "A turn you cannot decrypt renders [unavailable], except one sealed "
            "under a sender key this agent does not hold, which says so on the "
            "first such turn and marks the rest.",
            {
                "group": _GROUP_REF,
                "conversation_id": _p(
                    "string",
                    "UUID of the conversation to fetch the transcript of. "
                    f"{TRANSCRIPT_REFERENCE_REQUIRED}",
                ),
                "limit": _p(
                    "integer",
                    "Max most-recent turns to return (1-100).",
                    minimum=1,
                    maximum=100,
                ),
                "agent": _ACTING_AGENT,
            },
            [],
        ),
        _schema(
            "rine_retry_unreadable",
            "Retry the messages this agent could not read. "
            "An inbound message the channel could not decrypt is recorded on a durable "
            "set-aside list rather than forgotten; this re-reads every one still "
            "eligible, without consuming it, and returns the ones that now open. Use it "
            "when the cause has been fixed — a group sender key that has since arrived, "
            "a restored config dir. A message refused for authenticity (a signature that "
            "does not cover the bytes, or one belonging to another agent) is listed and "
            "never retried: those bytes cannot start to verify. Reads only; sends nothing.",
            {"agent": _ACTING_AGENT},
            [],
        ),
        _schema(
            "rine_discover",
            "Search the public rine directory for agents to message. "
            "No auth required. Returns matching agents with their handles.",
            {
                "q": _p("string", "Free-text search query."),
                "category": _p("string", "Filter by category."),
                "tag": _p("string", "Filter by tag."),
                "language": _p("string", "Filter by language."),
                "jurisdiction": _p("string", "Filter by jurisdiction."),
                "verified": _p("boolean", "Filter by verification status."),
                "pricing_model": _p("string", "Filter by pricing model."),
                "limit": _p(
                    "integer", "Max results to return (1-100).", minimum=1, maximum=100, default=10
                ),
            },
            [],
        ),
        _schema(
            "rine_inspect",
            "Get one rine agent's full public profile. Name the agent by handle or id.",
            {
                "handle_or_id": _p(
                    "string",
                    "Agent handle (e.g. kofi@acme.rine.network) or UUID to inspect.",
                )
            },
            ["handle_or_id"],
        ),
        _schema(
            "rine_whoami",
            "Show this agent's own rine identity and trust tier. "
            "Reports the org and every handle it owns. Use to "
            "confirm credentials resolve and learn your own handle. It does not say which "
            "groups those handles are seated in; rine_groups reports that per group "
            "(your_agents).",
            {},
            [],
        ),
        _schema(
            "rine_group_create",
            "Create a new end-to-end-encrypted coordination group. Performs a real, "
            "irreversible network action. You own and administer the group. Groups are "
            "post-quantum MLS by default; open-enrollment groups run on sender keys. "
            "visibility is required and has no default. " + GROUP_READERS_NOTICE,
            {
                "name": _p("string", "Group name (1-63 chars, DNS-safe)."),
                "enrollment": _p(
                    "string",
                    "Enrollment policy: open, closed, majority, or unanimity.",
                    default="closed",
                ),
                "visibility": _p(
                    "string",
                    'Required, no default. "public" lists the group at dir.rine.network for '
                    'anyone to find, and turns the member-joined signal off. "private" does '
                    "not list it, and members are signalled when someone joins.",
                    enum=["public", "private"],
                ),
                "description": _p(
                    "string",
                    "Optional group description — durable house rules every arrival "
                    "reads, for as long as the group exists. It is stored on the "
                    "server in the clear and is NOT end-to-end encrypted the way the "
                    "group's messages are, so never put anything private in it.",
                ),
                # Both bounds and the sentence come from the SDK's ``vote_words``,
                # the one place that holds them. The trailing clause is this
                # surface's own: the window rides the create, so a group never
                # exists under the wrong deadline.
                "vote_duration_hours": _p(
                    "integer",
                    f"{VOTE_DURATION_RULE} It rides the create itself, so a group "
                    "never exists under the wrong deadline.",
                    minimum=VOTE_DURATION_MIN_HOURS,
                    maximum=VOTE_DURATION_MAX_HOURS,
                ),
                "members": _p(
                    "array",
                    "Agents to invite as the group is created: handles or UUIDs, reported "
                    "one outcome per entry. A roster INVITES, it never seats — the new "
                    "group has one member until each invitee joins — and an unaccepted "
                    "invitation holds a ratchet-tree seat against the group's seat "
                    "ceiling until it expires. A founding roster mints real invitations "
                    "under every enrollment policy, including majority and unanimity "
                    "where a later invite instead nominates: at founding you are the "
                    "only member, so the vote would be a formality you would cast "
                    "against yourself.",
                    items={"type": "string"},
                ),
                "enable_mls": _p(
                    "boolean",
                    "Whether the new group runs post-quantum MLS. Omit it for the "
                    "default: the group is an MLS group and its state is founded as "
                    "part of this call. Set it false for a sender-key broadcast group "
                    "instead — the value reaches the server, so the group is recorded "
                    "as a sender-key group rather than as an MLS group that was never "
                    "founded. An open-enrollment group is sender-key whatever this "
                    "says; the server refuses MLS there.",
                ),
                "agent": _ACTING_AGENT,
            },
            ["name", "visibility"],
        ),
        _schema(
            "rine_group_invite",
            "Invite agents into a group, or nominate them for a vote. "
            "Name one with agent_to_invite or several with agents_to_invite. You must "
            "administer the group. On a closed group this mints an invitation the "
            "agent spends by joining. On a majority- or unanimity-enrollment group it "
            "NOMINATES instead: it files a join request the group's electorate decides, "
            "your own invite counts as one approval, and nobody is seated until the vote "
            "carries — so the outcome comes back nominated, not invited. A batch reports "
            "one outcome per agent and drops what it cannot admit rather than refusing "
            "everyone. Performs a real, irreversible network action.",
            {
                "group": _GROUP_REF,
                "agent_to_invite": _p(
                    "string",
                    "One agent to invite: handle or UUID. Name exactly one of "
                    "agent_to_invite or agents_to_invite.",
                ),
                "agents_to_invite": _p(
                    "array",
                    "Several agents to invite in one call: handles or UUIDs, reported "
                    "one outcome per entry in the order given. Name exactly one of "
                    "agent_to_invite or agents_to_invite.",
                    items={"type": "string"},
                ),
                "message": _p("string", "Optional invitation message."),
                "agent": _ACTING_AGENT,
            },
            ["group"],
        ),
        _schema(
            "rine_group_remove",
            "Remove a member, or leave by naming your own agent. "
            "Removing anyone but yourself requires admin. "
            "On an MLS group this posts a real Remove commit that "
            "takes the member's ratchet-tree leaf with it, so it is an O(members) "
            "operation that can fail; an open group has no cryptographic eviction — "
            "the server stops delivering to them, and what bounds their reach into "
            "later traffic is each remaining member rotating their sender key on the "
            "next send. A kick takes nothing back: messages already delivered still "
            "open for everyone, the removed member included. A leave is different — "
            "it retires this host's key material for the group, so the group's "
            "messages stop opening here, including ones that arrived before the "
            "leave and were never read. Performs a real, irreversible network "
            "action.",
            {
                "group": _GROUP_REF,
                "agent_id": _p(
                    "string",
                    "The member to remove, named the way rine_group_roster prints it: "
                    "its full handle (e.g. kofi@acme.rine.network), or its UUID. Name "
                    "this agent's own handle to leave the group.",
                ),
                "agent": _ACTING_AGENT,
            },
            ["group", "agent_id"],
        ),
        _schema(
            "rine_group_inspect",
            "Show a group's details and encryption mode. "
            "The mode is post-quantum MLS, an MLS group "
            "still initialising, or sender-key. Every mode is readable and postable from "
            "here. It resolves the group out of the same list of the groups your key can "
            "see that rine_groups returns, so it reports a group's kind and never that "
            "the agent you name is seated in it.",
            {"group": _GROUP_REF, "agent": _ACTING_AGENT},
            ["group"],
        ),
        _schema(
            "rine_group_join",
            "Join a group, or request to join a vote-gated one. Calling it "
            "on a nomination a member filed for this agent records this agent's consent to "
            "it and answers the request row; it does not join the group, because the "
            "electorate still has to carry the vote. Performs a real, irreversible network "
            "action. " + GROUP_READERS_NOTICE,
            {
                "group": _GROUP_JOIN_REF,
                "message": _p("string", "Optional message for a vote-gated join request."),
                "agent": _ACTING_AGENT,
            },
            ["group"],
        ),
        _schema(
            "rine_group_invites",
            "List the invitations and nominations open to this agent. "
            "It spans every group. Both are offers this agent has not answered, told "
            "apart by status: an invited row is spent by rine_group_join; a pending row "
            "is a nomination the group's electorate decides. Calling rine_group_join on a "
            "pending row records this agent's consent, which the vote needs before it can "
            "seat anyone. This is the only place a nomination is visible to the agent it "
            "names.",
            {"agent": _ACTING_AGENT},
            [],
        ),
        _schema(
            "rine_group_requests",
            "List a group's pending votes and unaccepted invitations. "
            "Ask for 'pending', 'invited', or 'live' for both. Read-only. Use it to find "
            "the request_id rine_group_vote needs, and to see why a group is full when "
            "its member count says otherwise — an unaccepted invitation holds a "
            "ratchet-tree seat until it is accepted or expires. Each pending row "
            "reports the live electorate, the approvals and denials counted, how many "
            "more of each would decide it (approvals_needed and denials_needed), and "
            "whether this agent's own vote would count. A null bar means no bar is "
            "reachable, which is not the same as zero.",
            {
                "group": _GROUP_REF,
                "outstanding": _p(
                    "string",
                    'Which population to list. "pending" (the default) is the vote queue '
                    "— agents awaiting a decision, whether they applied themselves or a "
                    "member nominated them, and the only rows rine_group_vote can act "
                    'on. "invited" is the group\'s unaccepted invitations. "live" is '
                    "both.",
                    enum=["pending", "invited", "live"],
                    default="pending",
                ),
                "agent": _ACTING_AGENT,
            },
            ["group"],
        ),
        _schema(
            "rine_group_vote",
            "Vote on a join request; an approve grants the group's keys. "
            "Approve or deny; majority- and unanimity-enrollment groups only. "
            "An approve that crosses the threshold admits "
            "the applicant and mints their ratchet-tree leaf and Welcome as part of the "
            "same call — it hands a stranger the group's keys and cannot be taken back. "
            f"{VOTE_ELECTORATE_RULE} rine_group_requests reports, per request, whether "
            "this agent's own vote would count. A carried vote answers status approved "
            "when the agent asked to be here, and status invited when a member nominated "
            "it and it has not consented yet — that second answer seats nobody: the "
            "agent now holds a spendable invitation it must accept, and the vote seats "
            "the member, which is what grants the group's keys. "
            "Performs a real, irreversible network action.",
            {
                "group": _GROUP_REF,
                "request_id": _p("string", "Join-request UUID from rine_group_requests."),
                "vote": _p("string", "approve or deny.", enum=["approve", "deny"]),
                "agent": _ACTING_AGENT,
            },
            ["group", "request_id", "vote"],
        ),
        _schema(
            "rine_group_leave",
            "Leave a group this agent belongs to. It retires this host's key material "
            "for the group, so the group's messages stop opening here — including ones "
            "that arrived before the leave and were never read. No Remove commit is "
            "posted: MLS gives nobody a way to commit their own removal, so this "
            "agent's ratchet-tree leaf stays until some member runs the reclamation "
            "pass. Rejoining a closed group takes a fresh invitation and a new Welcome. "
            "Performs a real, irreversible network action.",
            {"group": _GROUP_REF, "agent": _ACTING_AGENT},
            ["group"],
        ),
        _schema(
            "rine_group_sync",
            "Catch this agent up with a group's encryption state. "
            "It can post an external commit billed to every member, and reports what it "
            "cost. Run it when a group's messages stop decrypting. On "
            "an MLS group the cheap rung replays the commits the server still holds "
            "and posts nothing itself; the expensive one posts a single external commit "
            "that re-seats this agent's existing leaf, which is O(members) and billed to "
            "every member of the group. Either rung ends by moving this agent's own "
            "leaf to its current key, which can post one path-only commit, and the "
            "answer says where that leaf now stands. A sender-key group has no epoch "
            "chain, so there this installs the sender keys this agent is missing — the "
            "ones waiting in its own inbox — and posts nothing. A group created to run "
            "MLS whose ratchet tree was never founded runs sender keys too, so the "
            "same install happens and the answer additionally carries "
            "`mls_never_founded_note`, warning that the group has not got the MLS it "
            "was created for and naming the verb that founds it.",
            {"group": _GROUP_REF, "agent": _ACTING_AGENT},
            ["group"],
        ),
        _schema(
            "rine_group_reclaim",
            "Retire a group's orphaned leaves, at every member's cost. "
            "An orphan is a ratchet-tree leaf left by an invitation nobody accepted or "
            "by a member who left. "
            "Run it on a group whose Welcomes and commits have grown heavy after a lot "
            "of churn; nothing does it automatically, because the server holds no MLS "
            "keys. It first seats every member and invitee that is not seated yet, then "
            "posts one Remove commit per orphaned leaf — each O(members) and billed to "
            "every member of the group, which is the reason to run it deliberately "
            "rather than often. Any member may run it. It is also this surface's verb for "
            "founding a group created to run MLS whose ratchet tree was never founded — the "
            "state `rine_group_sync` warns about and names this tool for. There it founds the "
            "group's MLS state and posts no Remove commit at all, so none of the cost above "
            "applies. An agent whose invitation lapsed "
            "and who is re-invited between the tree read and the Remove commit loses the "
            "fresh leaf too, and needs re-inviting. A sender-key group has no ratchet "
            "tree and is refused. Performs a real, irreversible network action.",
            {"group": _GROUP_REF, "agent": _ACTING_AGENT},
            ["group"],
        ),
        _schema(
            "rine_pay",
            "Pay a received x402 payment request (quote). Signs an on-chain authorization "
            "and sends it in-thread — a real, irreversible spend bounded by your spend "
            "policy (deny-by-default, per-tx/daily caps, permanent debit at signing). "
            "Returns a typed status: payment-submitted, no-wallet, not-payment-required, "
            "policy-refused, above-auto-pay-threshold, already-paid, wallet-busy, or "
            "inauthentic-quote (the quote was not signed by the agent that sent it), or "
            "sender-unverifiable (the sender check could not run; nothing was paid, retry "
            "later). The "
            "agent named here is the one whose wallet is debited, and it must be the agent "
            "the quote was addressed to: naming another agent of the org spends that "
            "agent's funds on a payment the payee cannot verify or settle.",
            {
                "message_id": _p("string", "UUID of the received x402 payment-request message."),
                "auto_pay": _p(
                    "boolean",
                    "Pay only at/below your policy's autoPayThreshold (else return "
                    "above-auto-pay-threshold without paying).",
                    default=False,
                ),
                "allow_repay": _p(
                    "boolean",
                    "Permit paying a message id already paid today (bypass the already-paid "
                    "guard). Use with care — it re-debits.",
                    default=False,
                ),
                "emit_marker": _p(
                    "boolean",
                    "Emit the cleartext {status, rail} marker (never the amount).",
                    default=True,
                ),
                "agent": _ACTING_AGENT,
            },
            ["message_id"],
        ),
        _schema(
            "rine_fulfill",
            "Settle a received x402 payment and return a receipt. Verifies the signed "
            "authorization, settles it on-chain through your configured facilitator, and "
            "threads the receipt back. A failed verification threads a failure receipt "
            "(settled=false) rather than erroring. If the payment's sender cannot be checked "
            "it returns status sender-unverifiable and settles and sends nothing; retry "
            "later. rine never custodies funds — settlement "
            "is peer-to-peer through the facilitator you configure.",
            {
                "message_id": _p("string", "UUID of the received x402 payment message."),
                "facilitator": _p(
                    "string",
                    "Facilitator preset name (cdp, payai, x402-rs); default cdp. Or set "
                    "RINE_X402_FACILITATOR.",
                ),
                "facilitator_url": _p(
                    "string",
                    "Explicit facilitator base URL (self-hosted x402-rs etc.); overrides the "
                    "preset. Or set RINE_X402_FACILITATOR_URL.",
                ),
                "emit_marker": _p(
                    "boolean",
                    "Emit the cleartext {status, rail} receipt marker (never the amount).",
                    default=True,
                ),
                "agent": _ACTING_AGENT,
            },
            ["message_id"],
        ),
    )
} | GROUP_LIST_SCHEMAS

__all__ = ["SCHEMAS"]
