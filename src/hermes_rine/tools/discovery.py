"""Discovery + identity handlers: discover, inspect, whoami.

``discover``/``inspect`` are unauthenticated directory reads; ``whoami`` confirms the
agent's own identity (auth). Each wraps SDK calls in try/except and returns
``tool_error(_format_error(err))`` — never a raw traceback. The ``tools.registry``
import is fine at module top: this file is only imported when ``register()`` runs
inside a Hermes process.
"""

from __future__ import annotations

from typing import Any

from tools.registry import tool_error, tool_result  # type: ignore[import-not-found]

from hermes_rine._client import _format_error, build_client, live_handles
from hermes_rine._format import render_discover, render_profile

# discover() accepts these directory filters; passed straight through to the SDK.
_DISCOVER_KEYS = (
    "q",
    "category",
    "tag",
    "language",
    "jurisdiction",
    "verified",
    "pricing_model",
)


async def _handle_discover(args: dict[str, Any], **_: Any) -> str:
    """Search the public agent directory, no auth (``rine_discover``)."""
    kwargs: dict[str, Any] = {k: args[k] for k in _DISCOVER_KEYS if args.get(k) is not None}
    kwargs["limit"] = int(args.get("limit", 10))
    try:
        async with build_client() as client:
            page = await client.discover(**kwargs)
    except Exception as err:  # noqa: BLE001 — surface as a tool message, never crash
        return tool_error(_format_error(err))
    agents = list(page)
    return tool_result(agents=render_discover(agents), count=len(agents))


async def _handle_inspect(args: dict[str, Any], **_: Any) -> str:
    """Get one agent's full public profile (``rine_inspect``)."""
    try:
        async with build_client() as client:
            profile = await client.inspect(args["handle_or_id"])
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(profile=render_profile(profile))


async def _handle_whoami(_args: dict[str, Any], **_: Any) -> str:
    """Show this agent's own org + handle(s) + trust tier (``rine_whoami``)."""
    try:
        async with build_client() as client:
            me = await client.whoami()
    except Exception as err:  # noqa: BLE001
        return tool_error(_format_error(err))
    return tool_result(
        org=me.org.name,
        org_slug=me.org.slug,
        trust_tier=me.trust_tier,
        handles=live_handles(me),
    )


__all__ = ["_handle_discover", "_handle_inspect", "_handle_whoami"]
