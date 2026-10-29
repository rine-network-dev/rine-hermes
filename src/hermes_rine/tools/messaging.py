"""1:1 + group messaging handlers: send, send_and_wait, inbox, read, reply.

Async handlers over the SDK's ``RineClient``. Group messaging is not a separate tool: a
``to`` starting with ``#`` routes through ``_handle_send`` and group mail lands in the
inbox/read path. Every handler wraps SDK calls in try/except and returns
``tool_error(_format_error(err))``, never a raw traceback or ciphertext. ``rine_send``'s
refusal also carries ``sent=False``, the negation of the key its success asserts, because
the model reads only the tool result. The ``tools.registry`` import is fine at module top:
this file is only imported when ``register()`` runs inside a Hermes process.

All five take the optional ``agent`` the group tools take, wired the same way
(:func:`hermes_rine._client.acting_agent` reads it, :func:`settle_actor` settles it, then
both SDK levers). Settling first puts the one-agent warning on the tool result, not in a
log; an unnamed read in a two-agent org is refused rather than answered for the wrong
inbox, and omitted it is the request a single-agent org already sent, byte for byte, which
keeps ``rine_send_and_wait``'s 1:1-only refusal local.

``GET /messages/{id}`` and ``POST /messages/{id}/reply`` authorise by ORG, so either verb
reaches mail addressed to a sibling. Both act as the agent named here: a read moves that
agent's own delivery mark, and a reply is filed from the agent that acted. The SDK refuses
a sibling on ``rine_reply`` before sending, as the reply would arrive from an agent the
original never reached. Name the addressee on both.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from rine import MessageRead, UnsupportedTargetError
from rine.sender_key_words import SenderKeyRefusal
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import (
    _GROUP_ON_WAIT_MESSAGE,
    _format_error,
    acting_agent,
    build_client,
    settle_actor,
)
from hermes_rine._format import (
    _verified_note,
    render_inbox,
    render_message_body,
    render_single_message,
    render_thread,
)
from hermes_rine._tool_answers import TOOL_ANSWERS

logger = logging.getLogger("hermes_rine.messaging")

#: What each tool-layer inbox status sends on the wire. The server and both SDK
#: clients know exactly three statuses; ``all`` is this layer's own word for
#: *send no filter*, so it maps to ``None``. Passing it through would be a 422 at
#: the network, which is why the translation lives here and not in a caller.
_WIRE_STATUS: dict[str, Literal["new", "delivered", "read"] | None] = {
    "new": "new",
    "delivered": "delivered",
    "read": "read",
    "all": None,
}

_STATUS_WORDS = ", ".join(sorted(_WIRE_STATUS))


def _resolved_echo(spelling: str, msg: MessageRead) -> dict[str, str]:
    """The handle a send actually reached, when it is not what was written, in JSON.

    The two connectors whose tools answer in prose write this as a sentence —
    ``Sent {id} to {spelling} → {resolved}``. This tool answers in JSON, where an
    arrow has nowhere to go, so the same rule takes a different shape: ``to``
    keeps the caller's own spelling and the resolved handle arrives beside it
    under ``resolved_to``. 🔴 Never move the resolved handle INTO ``to`` — a host
    parses that key, so rewriting what it holds would be a wire change dressed as
    a copy edit.

    The key is absent when it has nothing to add: the two strings are equal, or
    the server returned no handle at all. ``resolved_to`` echoing ``to`` is a
    doubled string, and ``resolved_to: null`` is the JSON spelling of ``→ None``
    — each is a key a model has to read before it can decide it means nothing.

    The comparison is plain equality against the caller's argument, so a
    reference that resolved to itself says it once. Any difference at all is
    worth a line: a ``#`` the caller omitted or a case that was folded is the
    canonical spelling of the group it just posted to, which is the token it can
    hand back next turn.

    The handle is read off the server's own answer — ``group_handle`` for a group
    post, ``recipient_handle`` for a 1:1 — and is never resolved a second time
    here. A second resolver could disagree with the server's own answer, and a
    UUID-addressed 1:1 has no handle to print, which is exactly the arm that
    falls through to no key.
    """
    resolved = msg.group_handle or msg.recipient_handle
    if not resolved or resolved == spelling:
        return {}
    return {"resolved_to": resolved}


async def _handle_send(args: dict[str, Any], **_: Any) -> str:
    """Send an encrypted 1:1 or group message (``rine_send``).

    ``agent`` names which of the org's agents is sending. It is optional and an
    org with one agent never sets it; an org with more than one has to, because
    the server refuses a send it cannot attribute to a sender. A one-agent org
    that names a value anyway is not refused — the org's agent sends, and
    ``warning`` on the result says the value was ignored.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            msg = await client.send(
                args["to"],
                {"text": args["body"]},
                message_type=args.get("message_type", "rine.v1.task_request"),
                idempotency_key=args.get("idempotency_key"),
                agent=acting,
            )
            TOOL_ANSWERS.note_send(
                str(msg.conversation_id), msg.group_handle, msg.recipient_handle
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        # NEGATE the key the success asserts, don't just omit it. The host does
        # classify ``{"error": …}`` as a failure (``agent/display.py``
        # ``_detect_tool_failure``), but that verdict drives display, telemetry and
        # the repeated-failure guardrail — it never reaches the model's context.
        # All the model reads is this JSON, and a refusal that merely LACKS
        # ``sent`` leaves "did it send?" unanswered rather than answered no.
        # Same shape as the shipped ``tool_result(joined=False, …)`` in groups.py.
        return tool_error(_format_error(err), sent=False)
    return tool_result(
        **note,
        sent=True,
        message_id=str(msg.id),
        to=args["to"],
        **_resolved_echo(args["to"], msg),
        conversation_id=str(msg.conversation_id),
    )


async def _handle_send_and_wait(args: dict[str, Any], **_: Any) -> str:
    """Send and block until a reply arrives or the timeout elapses (``rine_send_and_wait``)."""
    wait_seconds = int(args.get("wait_seconds", 30))
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            result = await client.send_and_wait(
                args["to"],
                {"text": args["body"]},
                timeout=float(wait_seconds),
                message_type=args.get("message_type", "rine.v1.task_request"),
                agent=acting,
            )
            TOOL_ANSWERS.note_send(
                str(result.sent.conversation_id),
                result.sent.group_handle,
                result.sent.recipient_handle,
            )
    except UnsupportedTargetError:
        return tool_error(_GROUP_ON_WAIT_MESSAGE)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    if result.reply is not None:
        # One reply, one render: a fresh refusal explains a missing sender key in full.
        body = render_message_body(result.reply, SenderKeyRefusal())
        verification = _verified_note(result.reply) if result.reply.decrypt_error is None else None
        return tool_result(
            **note,
            sent_id=str(result.sent.id),
            reply=body,
            verification=verification,
        )
    return tool_result(
        **note,
        sent_id=str(result.sent.id),
        reply=None,
        note=f"no reply within {wait_seconds}s",
    )


async def _handle_inbox(args: dict[str, Any], **_: Any) -> str:
    """Read the inbox at a delivery status; ack only on ``new`` (``rine_inbox``).

    ``status`` is a tool-layer value with one more member than the wire has —
    see :data:`_WIRE_STATUS`. An unrecognised one is refused rather than read as
    ``new``: a model that asked for mail it has already been handed and silently
    got the undelivered queue instead has been answered a different question.

    The ack is scoped to ``new`` and stays there. Marking delivered is what
    makes ``new`` a queue that drains; on ``delivered``, ``read`` or ``all`` the
    agent is looking back over mail it has already been handed, and a read that
    silently mutates the state it was asked to report is not a read.
    """
    status = str(args.get("status", "new"))
    if status not in _WIRE_STATUS:
        return tool_error(f"Unknown inbox status '{status}'; use {_STATUS_WORDS}.")
    limit = int(args.get("limit", 20))
    ack_failed = False
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            page = await client.inbox(status=_WIRE_STATUS[status], limit=limit, agent=acting)
            messages = list(page)
            # Ack only ids that decrypted cleanly (record-before-... not applicable here;
            # this is the tool floor, the adapter owns the durable journal). A decrypt
            # failure must NOT be acked — the agent can retry once crypto is available.
            deliverable = (
                [str(m.id) for m in messages if m.decrypt_error is None] if status == "new" else []
            )
            if deliverable:
                # Best-effort: an ack failure must never lose the read — but it IS
                # surfaced (WARNING + a note) so the agent knows these may reappear.
                try:
                    # Same agent as the read: the ack resolves an agent of its own,
                    # so leaving it unnamed would refuse every ack in a two-agent
                    # org and quietly hand the same mail back on the next check.
                    await client.mark_delivered(deliverable, agent=acting)
                except Exception:  # noqa: BLE001 — ack is best-effort, keep the messages
                    ack_failed = True
                    logger.warning(
                        "rine_inbox: mark_delivered failed; %d message(s) may reappear",
                        len(deliverable),
                        exc_info=True,
                    )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    rendered = render_inbox(messages, status=status)
    if ack_failed:
        rendered += " (note: could not mark delivered; these may reappear)"
    return tool_result(
        **note,
        messages=rendered,
        count=len(messages),
        status=status,
        ack_failed=ack_failed,
    )


async def _handle_read(args: dict[str, Any], **_: Any) -> str:
    """Fetch and decrypt a single message by id (``rine_read``)."""
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            msg = await client.read(args["message_id"], agent=acting)
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(**note, message=render_single_message(msg))


async def _handle_reply(args: dict[str, Any], **_: Any) -> str:
    """Reply in-thread to a message (``rine_reply``)."""
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            reply = await client.reply(
                args["message_id"],
                {"text": args["body"]},
                message_type=args.get("message_type", "rine.v1.task_response"),
                agent=acting,
            )
            TOOL_ANSWERS.note_reply(str(args["message_id"]))
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(**note, replied_to=args["message_id"], reply_id=str(reply.id))


async def _handle_thread(args: dict[str, Any], **_: Any) -> str:
    """Fetch a conversation's or a group's decrypted transcript (``rine_thread``).

    A group is named here the way every other group tool takes it — a handle, a
    bare name or a UUID — and reading a group no longer costs a hop through
    another tool's output for an id of a different entity. ``rine_groups`` still
    reports each group's ``conversation_id`` and that spelling still reads the
    group's running thread; naming the group reads the group's own posts,
    including the ones a group made before it had a running thread.

    Which reference was named is decided by the SDK
    (:mod:`rine.thread_words`), not restated here: naming both, naming neither,
    and naming a conversation the wire cannot read as a UUID are one sentence
    each, and a second copy of them on this surface is how three wrappers come
    to answer the same mistake three ways. Each arrives as a ``ConfigError``
    inside this ``try``, so it reaches the model as a tool message with nothing
    sent.
    """
    try:
        named = acting_agent(args)
        async with build_client(agent=named) as client:
            acting, note = await settle_actor(client, named)
            entries = await client.thread(
                args.get("conversation_id"),
                group=args.get("group"),
                limit=args.get("limit"),
                agent=acting,
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(**note, thread=render_thread(entries))


__all__ = [
    "_handle_inbox",
    "_handle_read",
    "_handle_reply",
    "_handle_send",
    "_handle_send_and_wait",
    "_handle_thread",
]
