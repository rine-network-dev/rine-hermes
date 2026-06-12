"""``register_rine_skill(ctx)`` — register the bundled ``rine:rine`` skill.

The skill becomes resolvable as ``rine:rine`` via ``skill_view``. It is **not**
auto-listed in ``<available_skills>`` (plugin skills are opt-in explicit loads), so the
tool descriptions and the inbound ``auto_skill="rine:rine"`` point the agent at it.

Only imported when ``register()`` runs inside a Hermes process.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SKILL_PATH = Path(__file__).parent / "skill" / "rine" / "SKILL.md"


def register_rine_skill(ctx: Any) -> None:
    """Register the bundled rine skill so it is loadable as ``rine:rine``.

    Args:
        ctx: The Hermes ``PluginContext``.
    """
    ctx.register_skill(
        "rine",
        _SKILL_PATH,
        description="Use rine A2A messaging from Hermes (send/receive/discover/groups).",
    )


__all__ = ["register_rine_skill"]
