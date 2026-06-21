"""OpenAI-function ``dict`` schemas for the 12 ``rine_*`` tools.

Each schema is ``{"name", "description", "parameters": {json-schema}}`` — the exact
shape ``PluginContext.register_tool(schema=...)`` expects. The ``description`` here is
the canonical tool description the model sees; ``tools/__init__.py`` reuses it. Pure,
declarative data — side-effect-free. ``_p`` / ``_obj`` keep the repeated property and
object boilerplate out of each schema (DRY) so the contracts read as a table.
"""

from __future__ import annotations

from typing import Any

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


_TO_GROUP = _p("string", "Recipient handle/UUID, or '#group@org' for a group.")
_BODY = _p("string", "Message text (sent as the payload's 'text' field).")
_MTYPE_REQ = _p("string", "Rine message type.", default="rine.v1.task_request")
_MTYPE_RESP = _p("string", "Rine message type.", default="rine.v1.task_response")
_GROUP_REF = _p("string", "Group handle (#group@org) or UUID.")

SCHEMAS: dict[str, dict[str, Any]] = {
    s["name"]: s
    for s in (
        _schema(
            "rine_send",
            "Send an encrypted message to another agent (or a group) on the rine network. "
            "Performs a real, irreversible network send. Use to='handle@org' for 1:1 or "
            "to='#group@org' for a group.",
            {
                "to": _TO_GROUP,
                "body": _BODY,
                "message_type": _MTYPE_REQ,
                "idempotency_key": _p(
                    "string", "Optional idempotency key to dedupe retried sends."
                ),
            },
            ["to", "body"],
        ),
        _schema(
            "rine_send_and_wait",
            "Send a message to another agent and block until they reply or the timeout "
            "elapses. The delegate-and-await primitive. Performs a real network send. "
            "1:1 only (not groups).",
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
            },
            ["to", "body"],
        ),
        _schema(
            "rine_check_inbox",
            "Check the rine inbox for NEW (undelivered) messages and return their decrypted "
            "contents. Marks readable ones delivered so the next check returns only newer mail.",
            {
                "limit": _p(
                    "integer",
                    "Max new messages to return (1-100).",
                    minimum=1,
                    maximum=100,
                    default=20,
                ),
            },
            [],
        ),
        _schema(
            "rine_read",
            "Fetch and decrypt a single rine message by id.",
            {"message_id": _p("string", "UUID of the message to fetch and decrypt.")},
            ["message_id"],
        ),
        _schema(
            "rine_reply",
            "Reply in-thread to a rine message. Performs a real, irreversible network send.",
            {
                "message_id": _p("string", "UUID of the message to reply to."),
                "body": _p("string", "Reply text (sent as the payload's 'text' field)."),
                "message_type": _MTYPE_RESP,
            },
            ["message_id", "body"],
        ),
        _schema(
            "rine_thread",
            "Fetch the both-sided, decrypted transcript of a conversation by its UUID. "
            "Returns every turn (yours and the peer's) oldest-to-newest, role-tagged. "
            "A turn you cannot decrypt renders [unavailable].",
            {
                "conversation_id": _p(
                    "string", "UUID of the conversation to fetch the transcript of."
                ),
                "limit": _p(
                    "integer",
                    "Max most-recent turns to return (1-100).",
                    minimum=1,
                    maximum=100,
                ),
            },
            ["conversation_id"],
        ),
        _schema(
            "rine_discover",
            "Search the public rine agent directory (no auth required) to find agents to "
            "message. Returns matching agents with their handles.",
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
            "Get the full public profile of one rine agent by handle or id.",
            {"handle_or_id": _p("string", "Agent handle (name@org) or UUID to inspect.")},
            ["handle_or_id"],
        ),
        _schema(
            "rine_whoami",
            "Show this agent's own rine identity — org, handle(s), and trust tier. Use to "
            "confirm credentials resolve and learn your own handle.",
            {},
            [],
        ),
        _schema(
            "rine_group_create",
            "Create a new encrypted coordination group (sender-key E2EE). Performs a real, "
            "irreversible network action. You own and administer the group. The group is always "
            "sender-key (MLS is not exposed; the Python side cannot operate MLS groups).",
            {
                "name": _p("string", "Group name (1-63 chars, DNS-safe)."),
                "enrollment": _p(
                    "string",
                    "Enrollment policy: open, closed, majority, or unanimity.",
                    default="closed",
                ),
                "visibility": _p("string", "Visibility: public or private.", default="private"),
                "description": _p("string", "Optional group description."),
            },
            ["name"],
        ),
        _schema(
            "rine_group_invite",
            "Invite an agent into a group you administer. Performs a real, irreversible "
            "network action.",
            {
                "group": _GROUP_REF,
                "agent_to_invite": _p("string", "Handle or UUID of the agent to invite."),
                "message": _p("string", "Optional invitation message."),
            },
            ["group", "agent_to_invite"],
        ),
        _schema(
            "rine_group_remove",
            "Remove a member from a group you administer. Triggers a sender-key rotation "
            "(forward secrecy). Performs a real, irreversible network action.",
            {
                "group": _GROUP_REF,
                "agent_id": _p("string", "UUID of the member to remove."),
            },
            ["group", "agent_id"],
        ),
        _schema(
            "rine_group_inspect",
            "Show a group's details and encryption status, so you can tell whether you can "
            "read/post it (sender-key) or not (MLS).",
            {"group": _GROUP_REF},
            ["group"],
        ),
    )
}

__all__ = ["SCHEMAS"]
