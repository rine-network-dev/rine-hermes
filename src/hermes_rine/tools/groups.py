"""Sender-key group handlers: group_create, group_invite, group_remove, group_inspect.

Async handlers over ``client.groups.*``. Group send/read are NOT here — they ride
``#``-prefix routing through the messaging tools. The SDK hard-codes
``mls_enabled=False`` on create, so this plugin only ever owns sender-key groups;
``group_inspect`` surfaces the MLS latches so an operator can self-diagnose an
unreadable (TS-created MLS) group. Every handler returns ``tool_error`` on failure —
never a raw traceback. The ``tools.registry`` import is fine at module top: this file
is only imported when ``register()`` runs inside a Hermes process.
"""

from __future__ import annotations

from typing import Any

from rine import GroupRead, is_uuid, normalize_handle
from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, build_client
from hermes_rine._format import render_group


async def _handle_group_create(args: dict[str, Any], **_: Any) -> str:
    """Create a sender-key-encrypted coordination group (``rine_group_create``)."""
    try:
        async with build_client() as client:
            group = await client.groups.create(
                args["name"],
                description=args.get("description"),
                enrollment=args.get("enrollment", "closed"),
                visibility=args.get("visibility", "private"),
            )
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    return tool_result(
        created=True,
        handle=group.handle,
        group_id=str(group.id),
        enrollment=group.enrollment_policy,
        encryption="sender-key",
    )


async def _handle_group_invite(args: dict[str, Any], **_: Any) -> str:
    """Invite an agent into a group you administer (``rine_group_invite``)."""
    try:
        async with build_client() as client:
            result = await client.groups.invite(
                args["group"],
                args["agent_to_invite"],
                message=args.get("message"),
            )
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        invited=args["agent_to_invite"],
        group=args["group"],
        status=result.status,
    )


async def _handle_group_remove(args: dict[str, Any], **_: Any) -> str:
    """Remove a member from a group you administer (``rine_group_remove``)."""
    try:
        async with build_client() as client:
            await client.groups.remove_member(args["group"], args["agent_id"])
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        removed=args["agent_id"],
        group=args["group"],
        keys_rotated=True,
    )


async def _handle_group_inspect(args: dict[str, Any], **_: Any) -> str:
    """Show a group's details + sender-key-vs-MLS self-diagnosis (``rine_group_inspect``)."""
    target = args["group"]
    try:
        async with build_client() as client:
            groups = await client.groups.list()
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    match = _find_group(groups, target)
    if match is None:
        # Group-specific guidance: the generic NotFound branch suggests rine_discover,
        # which searches AGENTS, not groups — useless for resolving a group handle.
        return tool_error(
            f"No group '{target}' among the groups this agent belongs to. "
            "Use rine_group_create, or get invited first."
        )
    return tool_result(group=render_group(match))


def _find_group(groups: list[GroupRead], target: str) -> GroupRead | None:
    """Match a group by UUID, full handle, or bare local name.

    A bare name (no ``@``, not a UUID) matches on the handle's local part —
    ``g.handle.split("@")[0].lstrip("#") == target.lstrip("#")`` — for parity with
    ``invite``/``remove``, which the SDK lets accept a bare group name.
    """
    if is_uuid(target):
        return next((g for g in groups if str(g.id) == target), None)
    if "@" not in target:
        want = target.lstrip("#")
        return next((g for g in groups if g.handle.split("@")[0].lstrip("#") == want), None)
    wanted = normalize_handle(target if target.startswith("#") else f"#{target}")
    return next((g for g in groups if normalize_handle(g.handle) == wanted), None)


__all__ = [
    "_handle_group_create",
    "_handle_group_inspect",
    "_handle_group_invite",
    "_handle_group_remove",
]
