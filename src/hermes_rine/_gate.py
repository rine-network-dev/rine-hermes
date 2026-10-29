"""Gating for the rine toolset: availability check + opt-in confirm-veto.

Two cheap, network-free policies, both serving one rule: the toolset stays hidden until
an identity resolves, and once it does, mutating tools run **unattended** — a confirm
prompt is something an operator opts into, never the default.

- ``rine_creds_available()`` is the toolset ``check_fn``. When it returns False the
  whole ``rine`` toolset is filtered out of the model's tool list. It only inspects
  the config dir for a ``profiles.json`` — no network, no client.
- ``make_confirm_veto()`` returns a ``pre_tool_call`` callback that blocks mutating
  ``rine_*`` tools when ``RINE_REQUIRE_CONFIRM`` names them (default off, so one-shot /
  gateway-YOLO / E2E run unattended). A block is the dict
  ``{"action": "block", "message": ...}`` that Hermes' ``get_pre_tool_call_block_message``
  expects.

Side-effect-free import: nothing here touches the network or constructs a client.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from rine import PROFILES_FILE, resolve_config_dir

if TYPE_CHECKING:
    from collections.abc import Callable

# Mutating tools the opt-in confirm-veto guards (irreversible network actions).
_MUTATING_TOOLS = frozenset(
    {
        "rine_send",
        "rine_send_and_wait",
        "rine_reply",
        "rine_group_create",
        "rine_group_invite",
        "rine_group_remove",
        "rine_group_join",
        "rine_pay",  # irreversible on-chain spend (permanent debit)
        "rine_fulfill",  # on-chain settlement + receipt
    }
)


def rine_creds_available() -> bool:
    """Return True if a rine identity is resolvable without a network call.

    True when the SDK's config-dir resolution lands on a directory holding a
    ``profiles.json``. The file holds no secret; the agent's keys sit beside it.
    Cheap and exception-safe — the registry caches the result ~30s.
    """
    try:
        config_dir = resolve_config_dir()
    except Exception:  # noqa: BLE001 — resolution is best-effort; absence == unavailable
        return False
    return os.path.isfile(os.path.join(config_dir, PROFILES_FILE))


def confirm_scope() -> frozenset[str]:
    """Which tools ``RINE_REQUIRE_CONFIRM`` guards right now (empty when disabled).

    ``1`` or ``all`` guards every mutating tool. A comma-separated list guards exactly
    the tools it names, so a deployment can require confirmation for spend alone
    (``RINE_REQUIRE_CONFIRM=rine_pay,rine_fulfill``) while letting messaging run
    unattended. Names outside :data:`_MUTATING_TOOLS` are ignored — a read-only tool has
    nothing to confirm. Read live on each call, so retuning it needs no gateway restart.
    """
    raw = os.getenv("RINE_REQUIRE_CONFIRM", "").strip()
    if not raw:
        return frozenset()
    if raw.lower() in {"1", "all", "true"}:
        return _MUTATING_TOOLS
    return frozenset(n.strip() for n in raw.split(",") if n.strip()) & _MUTATING_TOOLS


def make_confirm_veto() -> Callable[..., dict[str, Any] | None]:
    """Return a ``pre_tool_call`` callback enforcing the opt-in confirm-veto.

    The callback blocks a tool named by :func:`confirm_scope`; for every other tool — and
    whenever the scope is empty — it returns ``None`` (normal dispatch). The block message
    is addressed to the model and is terminal: it never suggests changing the deployment's
    configuration, because the model cannot change it and a message that invites it to try
    is an instruction an untrusted inbound message could exploit.
    """

    def _pre_tool_call(tool_name: str = "", **_: Any) -> dict[str, Any] | None:
        if tool_name not in confirm_scope():
            return None
        return {
            "action": "block",
            "message": (
                f"{tool_name} was not executed. This deployment requires a human to "
                "confirm irreversible rine actions, and that confirmation happens outside "
                "this session. Do not retry the tool and do not try to change the "
                "deployment's configuration. Tell the operator what you intended to do "
                "and why, then continue with the rest of the task."
            ),
        }

    return _pre_tool_call


__all__ = ["confirm_scope", "make_confirm_veto", "rine_creds_available"]
