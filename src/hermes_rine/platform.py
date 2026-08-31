"""``register_rine_platform(ctx)`` — register the inbound wake channel.

Wires the gateway platform adapter (``RineAdapter``) into Hermes so an enabled,
credential-bearing agent transparently wakes on inbound rine mail. Registration is
side-effect-free: the adapter factory is a lambda — **no** adapter is constructed, **no**
client is built, and **no** listener starts until the gateway calls the factory and the
adapter's ``connect()``. The toolset creds gate (``rine_creds_available``) doubles as the
platform ``check_fn`` so the gateway skips instantiation when no creds resolve.

Registering here also makes ``platform_registry.is_registered("rine")`` True, which is
what lets ``Platform("rine")`` resolve as a pseudo-member when the adapter is later built.

Imported only when ``register()`` runs inside a Hermes process; the heavy ``adapter``
import is deferred into the function body so importing this module stays cheap.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from hermes_rine._gate import rine_creds_available

logger = logging.getLogger("hermes_rine.platform")

# Appended to the model's stable system prompt on every gateway-woken turn, so the rule it
# carries survives a long conversation. The bundled skill says the same thing, but a skill
# is injected once per new session; this is the seam that does not fade.
_PLATFORM_HINT = (
    "You are connected to the rine agent-to-agent network. An inbound rine message wakes "
    "this turn, and your final response is encrypted and sent back to the sender as the "
    "reply, in the same conversation. Do not call rine_send or rine_reply to answer it — "
    "that sends the answer a second time, in a second conversation. Load skill rine:rine "
    "for usage."
)

# Gateway-side registration extras that older Hermes releases may not know. ``PlatformEntry``
# is a dataclass, so an unrecognised key raises TypeError out of its constructor and takes
# the whole plugin down with it; each key is offered only when the installed dataclass
# declares a field for it.
#
# ``allowed_users_env``/``allow_all_env`` let the gateway enforce the rine allowlist one
# layer above the adapter, which is what makes the global GATEWAY_ALLOW_ALL_USERS
# unnecessary. ``RINE_ALLOWED_HANDLES`` is then read twice — by the gateway and again by
# ``_poll._dispatch`` — deliberately: the gateway gate covers paths the adapter never sees
# (a message arriving while a turn is already running), and the adapter gate covers the
# transports. Neither is redundant.
#
# ``cron_deliver_env_var`` names rine's home-channel variable so rine is a legal
# cron-delivery target rather than falling through to a derived default.
_OPTIONAL_ENTRY_KWARGS = {
    "allowed_users_env": "RINE_ALLOWED_HANDLES",
    "allow_all_env": "RINE_ALLOW_ALL_USERS",
    "cron_deliver_env_var": "RINE_HOME_CHANNEL",
}


def _supported_entry_kwargs() -> dict[str, str]:
    """The subset of :data:`_OPTIONAL_ENTRY_KWARGS` the installed ``PlatformEntry`` accepts."""
    try:
        from gateway.platform_registry import (  # type: ignore[import-not-found]
            PlatformEntry,
        )

        known = {f.name for f in dataclasses.fields(PlatformEntry)}
    except Exception:  # noqa: BLE001 — absent or renamed: register without the extras
        return {}
    return {k: v for k, v in _OPTIONAL_ENTRY_KWARGS.items() if k in known}


def register_rine_platform(ctx: Any) -> None:
    """Register the rine inbound wake channel with the Hermes gateway.

    Args:
        ctx: The Hermes ``PluginContext``.
    """
    from hermes_rine.adapter import RineAdapter

    ctx.register_platform(
        name="rine",
        label="rine",
        adapter_factory=lambda cfg: RineAdapter(cfg),
        check_fn=rine_creds_available,
        required_env=["RINE_CLIENT_ID", "RINE_CLIENT_SECRET"],
        install_hint="pip install hermes-rine",
        platform_hint=_PLATFORM_HINT,
        emoji="📡",
        **_supported_entry_kwargs(),
    )
    logger.debug("rine platform adapter registered")


__all__ = ["register_rine_platform"]
