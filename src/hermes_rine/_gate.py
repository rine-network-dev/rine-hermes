"""Gating for the rine toolset: availability check + opt-in confirm-veto.

Two cheap, network-free policies (D-GATE):

- ``rine_creds_available()`` is the toolset ``check_fn``. When it returns False the
  whole ``rine`` toolset is filtered out of the model's tool list. It only inspects
  env vars and the config dir for a ``credentials.json`` — no network, no client.
- ``make_confirm_veto()`` returns a ``pre_tool_call`` callback that blocks mutating
  ``rine_*`` tools **only** when ``RINE_REQUIRE_CONFIRM=1`` (default off, so one-shot /
  gateway-YOLO / E2E run unattended). A block is the dict
  ``{"action": "block", "message": ...}`` that Hermes' ``get_pre_tool_call_block_message``
  expects.

Side-effect-free import: nothing here touches the network or constructs a client.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from rine import resolve_config_dir

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
    }
)


def rine_creds_available() -> bool:
    """Return True if rine credentials are resolvable without a network call.

    True when either ``RINE_CLIENT_ID``+``RINE_CLIENT_SECRET`` are both set, or the
    SDK's config-dir resolution lands on a directory holding a ``credentials.json``.
    Cheap and exception-safe — the registry caches the result ~30s.
    """
    if os.getenv("RINE_CLIENT_ID") and os.getenv("RINE_CLIENT_SECRET"):
        return True
    try:
        config_dir = resolve_config_dir()
    except Exception:  # noqa: BLE001 — resolution is best-effort; absence == unavailable
        return False
    return os.path.isfile(os.path.join(config_dir, "credentials.json"))


def make_confirm_veto() -> Callable[..., dict[str, Any] | None]:
    """Return a ``pre_tool_call`` callback enforcing the opt-in confirm-veto.

    The callback blocks a mutating ``rine_*`` tool **only** when
    ``RINE_REQUIRE_CONFIRM=1`` (read live each call so the operator can toggle it
    without a restart). Otherwise — and for every non-mutating or non-rine tool — it
    returns ``None`` (normal dispatch).
    """

    def _pre_tool_call(tool_name: str = "", **_: Any) -> dict[str, Any] | None:
        if os.getenv("RINE_REQUIRE_CONFIRM") != "1":
            return None
        if tool_name not in _MUTATING_TOOLS:
            return None
        return {
            "action": "block",
            "message": (
                f"{tool_name} performs an irreversible rine network action and "
                "RINE_REQUIRE_CONFIRM=1 is set. Confirm with the operator, then unset "
                "RINE_REQUIRE_CONFIRM (or rerun without it) to proceed."
            ),
        }

    return _pre_tool_call


__all__ = ["make_confirm_veto", "rine_creds_available"]
