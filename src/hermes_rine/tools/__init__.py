"""``register_rine_tools(ctx)`` — register all 26 ``rine_*`` tools with Hermes.

Each tool is registered via ``ctx.register_tool(toolset="rine", is_async=True,
check_fn=rine_creds_available, schema=<from _schemas>, handler=<async fn>)``. The
toolset-level ``check_fn`` (passed on every tool, harmless) hides the whole toolset
until creds resolve. The opt-in confirm-veto (``RINE_REQUIRE_CONFIRM=1``) is wired here
as a ``pre_tool_call`` hook; it is a no-op unless that env var is set.

This module imports Hermes internals only transitively (via the handler modules); it
is itself only imported when ``register()`` runs inside a Hermes process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hermes_rine._gate import make_confirm_veto, rine_creds_available
from hermes_rine._schemas import SCHEMAS
from hermes_rine.tools.discovery import (
    _handle_discover,
    _handle_inspect,
    _handle_whoami,
)
from hermes_rine.tools.groups import (
    _handle_group_create,
    _handle_group_inspect,
    _handle_group_invite,
    _handle_group_invites,
    _handle_group_join,
    _handle_group_remove,
)
from hermes_rine.tools.groups_admin import (
    _handle_group_leave,
    _handle_group_requests,
    _handle_group_sync,
    _handle_group_vote,
)
from hermes_rine.tools.groups_list import (
    _handle_discover_groups,
    _handle_group_roster,
    _handle_groups,
)
from hermes_rine.tools.groups_reclaim import _handle_group_reclaim
from hermes_rine.tools.messaging import (
    _handle_inbox,
    _handle_read,
    _handle_reply,
    _handle_send,
    _handle_send_and_wait,
    _handle_thread,
)
from hermes_rine.tools.payments import _handle_fulfill, _handle_pay
from hermes_rine.tools.unreadable import _handle_retry_unreadable

if TYPE_CHECKING:
    from collections.abc import Callable

# name -> handler. No tool declares env vars: an agent signs in with a key in the config
# dir, and the toolset ``check_fn`` is the gate.
_HANDLERS: dict[str, Callable[..., Any]] = {
    "rine_send": _handle_send,
    "rine_send_and_wait": _handle_send_and_wait,
    "rine_inbox": _handle_inbox,
    "rine_read": _handle_read,
    "rine_reply": _handle_reply,
    "rine_thread": _handle_thread,
    "rine_retry_unreadable": _handle_retry_unreadable,
    "rine_discover": _handle_discover,  # unauthenticated directory read
    "rine_discover_groups": _handle_discover_groups,  # unauthenticated directory read
    "rine_inspect": _handle_inspect,  # unauthenticated directory read
    "rine_whoami": _handle_whoami,
    "rine_groups": _handle_groups,
    "rine_group_create": _handle_group_create,
    "rine_group_invite": _handle_group_invite,
    "rine_group_remove": _handle_group_remove,
    "rine_group_inspect": _handle_group_inspect,
    "rine_group_roster": _handle_group_roster,
    "rine_group_join": _handle_group_join,
    "rine_group_invites": _handle_group_invites,
    "rine_group_requests": _handle_group_requests,
    "rine_group_vote": _handle_group_vote,
    "rine_group_leave": _handle_group_leave,
    "rine_group_sync": _handle_group_sync,
    "rine_group_reclaim": _handle_group_reclaim,
    "rine_pay": _handle_pay,  # x402 payer (irreversible spend)
    "rine_fulfill": _handle_fulfill,  # x402 payee (settle + receipt)
}


def register_rine_tools(ctx: Any) -> None:
    """Register all 26 ``rine_*`` tools + the opt-in confirm-veto hook.

    Args:
        ctx: The Hermes ``PluginContext``.
    """
    for name, handler in _HANDLERS.items():
        schema = SCHEMAS[name]
        # No explicit ``description=``: the registry derives it from ``schema`` itself
        # (registry.py builds ``description or schema.get("description")``), so passing it
        # would just duplicate the single source of truth and risk drift.
        ctx.register_tool(
            name=name,
            toolset="rine",
            schema=schema,
            handler=handler,
            check_fn=rine_creds_available,
            is_async=True,
        )
    # Default-off confirm-veto — mutating tools run unattended unless an operator opts
    # in: a no-op unless RINE_REQUIRE_CONFIRM=1.
    ctx.register_hook("pre_tool_call", make_confirm_veto())


__all__ = ["register_rine_tools"]
