"""1:1 + group messaging handlers: send, send_and_wait, check_inbox, read, reply.

Async handlers over the SDK's async ``RineClient``. Group messaging is not a separate
tool — a ``to`` starting with ``#`` routes through ``_handle_send`` and group mail lands
in the inbox/read path. Every handler wraps SDK calls in try/except and returns
``tool_error(_format_error(err))`` — never a raw traceback, never ciphertext. The
``tools.registry`` import is fine at module top: this file is only imported when
``register()`` runs inside a Hermes process.
"""

from __future__ import annotations

import logging
from typing import Any

from rine import UnsupportedTargetError
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, build_client
from hermes_rine._format import (
    _verified_note,
    render_inbox,
    render_message_body,
    render_single_message,
    render_thread,
)

logger = logging.getLogger("hermes_rine.messaging")


async def _handle_send(args: dict[str, Any], **_: Any) -> str:
    """Send an encrypted 1:1 or group message (``rine_send``)."""
    try:
        async with build_client() as client:
            msg = await client.send(
                args["to"],
                {"text": args["body"]},
                message_type=args.get("message_type", "rine.v1.task_request"),
                idempotency_key=args.get("idempotency_key"),
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    return tool_result(
        sent=True,
        message_id=str(msg.id),
        to=args["to"],
        conversation_id=str(msg.conversation_id),
    )


async def _handle_send_and_wait(args: dict[str, Any], **_: Any) -> str:
    """Send and block until a reply arrives or the timeout elapses (``rine_send_and_wait``)."""
    wait_seconds = int(args.get("wait_seconds", 30))
    try:
        async with build_client() as client:
            result = await client.send_and_wait(
                args["to"],
                {"text": args["body"]},
                timeout=float(wait_seconds),
                message_type=args.get("message_type", "rine.v1.task_request"),
            )
    except UnsupportedTargetError:
        return tool_error("send_and_wait is 1:1 only; use rine_send for groups.")
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    if result.reply is not None:
        body = render_message_body(result.reply)
        verification = _verified_note(result.reply) if result.reply.decrypt_error is None else None
        return tool_result(
            sent_id=str(result.sent.id),
            reply=body,
            verification=verification,
        )
    return tool_result(
        sent_id=str(result.sent.id),
        reply=None,
        note=f"no reply within {wait_seconds}s",
    )


async def _handle_check_inbox(args: dict[str, Any], **_: Any) -> str:
    """Fetch NEW messages and mark readable ones delivered (``rine_check_inbox``)."""
    limit = int(args.get("limit", 20))
    ack_failed = False
    try:
        async with build_client() as client:
            page = await client.inbox(status="new", limit=limit)
            messages = list(page)
            # Ack only ids that decrypted cleanly (record-before-... not applicable here;
            # this is the tool floor, the adapter owns the durable journal). A decrypt
            # failure must NOT be acked — the agent can retry once crypto is available.
            deliverable = [str(m.id) for m in messages if m.decrypt_error is None]
            if deliverable:
                # Best-effort: an ack failure must never lose the read — but it IS
                # surfaced (WARNING + a note) so the agent knows these may reappear.
                try:
                    await client.mark_delivered(deliverable)
                except Exception:  # noqa: BLE001 — ack is best-effort, keep the messages
                    ack_failed = True
                    logger.warning(
                        "rine_check_inbox: mark_delivered failed; %d message(s) may reappear",
                        len(deliverable),
                        exc_info=True,
                    )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    rendered = render_inbox(messages)
    if ack_failed:
        rendered += " (note: could not mark delivered; these may reappear)"
    return tool_result(messages=rendered, count=len(messages), ack_failed=ack_failed)


async def _handle_read(args: dict[str, Any], **_: Any) -> str:
    """Fetch and decrypt a single message by id (``rine_read``)."""
    try:
        async with build_client() as client:
            msg = await client.read(args["message_id"])
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(message=render_single_message(msg))


async def _handle_reply(args: dict[str, Any], **_: Any) -> str:
    """Reply in-thread to a message (``rine_reply``)."""
    try:
        async with build_client() as client:
            reply = await client.reply(
                args["message_id"],
                {"text": args["body"]},
                message_type=args.get("message_type", "rine.v1.task_response"),
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(replied_to=args["message_id"], reply_id=str(reply.id))


async def _handle_thread(args: dict[str, Any], **_: Any) -> str:
    """Fetch the both-sided, decrypted transcript of a conversation (``rine_thread``)."""
    try:
        async with build_client() as client:
            entries = await client.thread(args["conversation_id"], limit=args.get("limit"))
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(thread=render_thread(entries))


__all__ = [
    "_handle_check_inbox",
    "_handle_read",
    "_handle_reply",
    "_handle_send",
    "_handle_send_and_wait",
    "_handle_thread",
]
