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

import logging
from typing import Any

from hermes_rine._gate import rine_creds_available

logger = logging.getLogger("hermes_rine.platform")

_PLATFORM_HINT = "You are connected to the rine A2A network; load skill rine:rine for usage."


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
    )
    logger.debug("rine platform adapter registered")


__all__ = ["register_rine_platform"]
