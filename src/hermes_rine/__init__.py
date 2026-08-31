"""hermes-rine — official Hermes Agent plugin for the Rine network.

A single plugin that is simultaneously a **tool set** (25 ``rine_*`` tools), an
**inbound channel** (a gateway platform adapter that wakes the agent on new mail),
and a **bundled skill** (``rine:rine``). All crypto, HTTP, and config-dir resolution
come from the ``rine`` SDK; this package never reimplements them.

**Import discipline (critical).** This module imports *nothing heavy*. ``import
hermes_rine`` succeeds even without ``hermes-agent`` installed and without the SDK
touching the network. The Hermes-internal registrars (``tools.registry``,
``gateway.platforms.base``, ``hermes_cli.plugins``) are imported **lazily inside
``register()``**, which only ever runs inside a live Hermes process. Likewise no
client is constructed, no credential is read, and no listener is started at import
or ``register()`` time — that work is deferred to a tool's first handler call or the
adapter's ``connect()``.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("hermes-rine")
except PackageNotFoundError:  # not installed (e.g. running from a source checkout)
    # 🔴 A SENTINEL, never the real number — it read "0.5.0" through two releases.
    # Nothing gates a fallback against pyproject, so the only safe value is one that
    # is obviously not a version. Matches langchain-rine and crewai-rine.
    __version__ = "0.0.0"


def register(ctx: Any) -> None:
    """Hermes plugin entry point — wire up tools, skill, and platform.

    Called by Hermes with a ``PluginContext`` once the plugin is enabled. Imports
    the registrars lazily so the heavy Hermes-internal modules are only pulled in
    inside a Hermes process (never at ``import hermes_rine`` time).

    Args:
        ctx: The Hermes ``PluginContext`` (``hermes_cli.plugins.PluginContext``).
    """
    from hermes_rine.platform import register_rine_platform
    from hermes_rine.skill_reg import register_rine_skill
    from hermes_rine.tools import register_rine_tools

    register_rine_tools(ctx)
    register_rine_skill(ctx)
    register_rine_platform(ctx)


__all__ = ["__version__", "register"]
