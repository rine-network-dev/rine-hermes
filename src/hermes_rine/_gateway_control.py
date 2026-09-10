"""Whether an inbound rine peer may drive the operator's Hermes gateway.

Hermes' ``MessageEvent.allow_gateway_control`` defaults **True**, and its own field
comment says a proactive plugin event should set it False so untrusted payload text stays
conversational input. This package never set it, so a peer's ``/new``, ``/stop`` or
``/model …`` arriving as a rine DM ran as a gateway command, a plaintext "restart hermes"
was coerced into one, and a peer's "yes" resolved a pending approval prompt the operator
was asked.

It now defaults **off**, and control is granted **iff the channel switch is on AND the
sender is named**:

===========================  ==========================  ===========================
``RINE_ALLOW_GATEWAY_CONTROL``  ``RINE_GATEWAY_CONTROL_HANDLES``  Result
===========================  ==========================  ===========================
unset                        unset                       off for every sender
``1``                        unset / blank               off — a switch alone grants
                                                         nothing
``1``                        ``a@x,b@y``                 control for those two only
``1``                        ``*``                       control for every sender the
                                                         wake allowlist admits — which
                                                         is EVERY agent on rine unless
                                                         ``RINE_ALLOWED_HANDLES`` names
                                                         the peers
unset / ``0``                ``a@x,b@y``                 off — the list is inert
===========================  ==========================  ===========================

The two half-configured rows are logged once at connect: a user who set one of the two
expects it to work, and silence in the safe direction is still a surprise.

🔴 ``*`` is **not** a bound. ``RINE_ALLOWED_HANDLES`` is applied first
(``_poll._dispatch`` step 2), so the control set is a subset of the senders that can wake
the agent — but that parser returns ``None`` (allow-all) when the variable is unset, which
is the out-of-the-box state and the one the docs recommend for waking at all. So unless
``RINE_ALLOWED_HANDLES`` actually names peers, ``*`` here means every agent on rine.
:func:`log_configuration` takes the wake allowlist's own state so the warning can say
which of the two it is.

The same two settings are reachable from ``config.yaml`` through
:func:`apply_yaml_config`, which Hermes dispatches as the platform's
``apply_yaml_config_fn``::

    rine:
      allow_gateway_control: true
      gateway_control_handles: ["ops@acme.rine.network"]

This module lives at the top of the package rather than under ``tools/`` deliberately:
``tools/`` is a registered tool-module surface for the docs-fact gate, which would then
read every string here as tool-definition prose.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

logger = logging.getLogger("hermes_rine.gateway_control")

#: The channel switch. ``"1"`` enables; anything else, including unset, disables.
#: Matches ``RINE_REQUIRE_VERIFIED``'s convention rather than inventing a second
#: truthiness rule inside one package.
ALLOW_ENV = "RINE_ALLOW_GATEWAY_CONTROL"

#: The named-peer allowlist: comma-separated handles, or ``*`` for every sender the wake
#: allowlist admits — which is every agent on rine unless ``RINE_ALLOWED_HANDLES`` names peers.
HANDLES_ENV = "RINE_GATEWAY_CONTROL_HANDLES"

_ALL = "*"


def switch_on(raw: str | None) -> bool:
    """True when the channel switch is set to ``"1"``."""
    return raw == "1"


def parse_control_handles(raw: str) -> frozenset[str] | None:
    """Handles allowed to drive the gateway. ``None`` means every admitted sender (``*``).

    🔴 Deliberately **not** ``adapter._parse_allowed_handles``, and not a generalisation
    of it. That parser returns ``None`` for unset *and* for ``*``, because the wake
    allowlist it serves defaults **permissive**: an operator who names nobody wants every
    peer to be able to wake the agent. This knob defaults **deny**, so a blank value has
    to come back as an empty set. Reusing the other parser here would have made
    gateway control allow-all by default — the exact inverse of the setting's purpose,
    arrived at silently, with no test able to tell the two ``None``s apart.
    """
    handles = frozenset(h.strip() for h in raw.split(",") if h.strip())
    if not handles:
        return frozenset()
    return None if _ALL in handles else handles


def grants_control(*, enabled: bool, handles: frozenset[str] | None, sender: str) -> bool:
    """Whether ``sender`` may resolve gateway commands and control prompts.

    Args:
        enabled: The channel switch, as :func:`switch_on` read it.
        handles: The allowlist, as :func:`parse_control_handles` parsed it — ``None``
            for ``*``, an empty set for "nobody named".
        sender: The inbound message's sender handle.

    Returns:
        True only when the switch is on **and** the sender is named (or ``*``).

    🔴 ``bool(sender)`` is tested BEFORE the ``*`` arm, not after. ``_sender_handle``
    returns ``""`` for a frame carrying neither a sender handle nor a from-agent id, and
    an operator cannot have trusted a peer they cannot name — so an unattributable sender
    is denied under ``*`` exactly as it is under a named list. Ordering it the other way
    round made ``*`` the one posture that executes a gateway command nobody can audit.
    """
    if not enabled or not sender:
        return False
    if handles is None:
        return True
    return sender in handles


def log_configuration(
    *, enabled: bool, handles: frozenset[str] | None, wake_allowlist_named: bool
) -> None:
    """Warn once about a half-configured knob; say nothing when it is coherently off.

    ``wake_allowlist_named`` is True when ``RINE_ALLOWED_HANDLES`` actually names peers
    (``adapter.allowed_handles is not None``). It changes only how loudly ``*`` is
    reported: with no wake allowlist, ``*`` grants every agent on rine.
    """
    if not enabled:
        if handles:
            logger.warning(
                "[rine] handles are named in %s but %s is not '1', so gateway control "
                "stays off for every sender",
                HANDLES_ENV,
                ALLOW_ENV,
            )
        return
    if handles is None and not wake_allowlist_named:
        logger.warning(
            "[rine] gateway control is open to EVERY AGENT ON RINE (%s=* with no "
            "RINE_ALLOWED_HANDLES): any peer that can address this agent can run "
            "gateway commands and answer approval prompts. Name the trusted peers.",
            HANDLES_ENV,
        )
    elif handles is None:
        logger.warning(
            "[rine] gateway control is open to EVERY sender RINE_ALLOWED_HANDLES admits "
            "(%s=*); name the trusted peers instead to narrow it",
            HANDLES_ENV,
        )
    elif not handles:
        logger.warning(
            "[rine] %s is on but no handles are named, so gateway control stays off for "
            "every sender; set %s to the trusted peers",
            ALLOW_ENV,
            HANDLES_ENV,
        )
    else:
        logger.info("[rine] gateway control enabled for %d named peer(s)", len(handles))


def optional_event_kwargs(event_cls: type, **optional: object) -> dict[str, object]:
    """Filter ``optional`` to the kwargs ``event_cls`` actually declares.

    ``MessageEvent`` is a dataclass, so a kwarg it does not declare is a ``TypeError``
    raised inside ``_dispatch`` — which stops the plugin waking on mail at all. The field
    does not exist on the Hermes releases below the advertised floor, so the guard is what
    lets the plugin keep running there (with today's behaviour, since the host has no such
    concept to switch off).

    🔴 ``platform._supported_entry_kwargs`` does the same filtering for ``PlatformEntry``
    and the two are deliberately NOT one helper: their failure arms point in **opposite**
    directions and both directions are load-bearing. This one fails **open** (see the note
    below); that one fails **closed**, because an entry kwarg the host does not declare
    takes the whole plugin down at registration. Consolidating them onto either arm breaks
    the other caller — do not.

    🔴 The ``except TypeError`` arm is load-bearing. The test suite's ``MessageEvent``
    double is a plain ``**kwargs`` class, not a dataclass, so ``dataclasses.fields``
    raises on it; a naive guard would drop the kwarg under the double, every unit test
    asserting the flag would fail against correct code, and the obvious "fix" — asserting
    the flag's absence — would pin the defect as correct.
    """
    try:
        known = {f.name for f in dataclasses.fields(event_cls)}
    except TypeError:
        return dict(optional)
    return {k: v for k, v in optional.items() if k in known}


def apply_yaml_config(
    yaml_cfg: dict[str, Any], platform_cfg: dict[str, Any]
) -> dict[str, Any] | None:
    """Bridge the two ``rine:`` gateway-control keys into env. Never raises.

    Registered as ``PlatformEntry.apply_yaml_config_fn``. Hermes resolves the block from a
    top-level ``rine:``, then ``gateway.platforms.rine``, then ``platforms.rine``, and
    skips the hook entirely when none of the three is a dict — safe here precisely because
    the default is off.

    Env wins over YAML, which is the contract the field's own docstring prescribes: each
    key is seeded only when the variable is not **present**.

    🔴 "Present", not "truthy". ``export RINE_GATEWAY_CONTROL_HANDLES=""`` is this
    setting's revoke gesture — ``parse_control_handles("")`` is an empty set, which denies
    everyone — so treating the empty string as absent would write the YAML list back over
    a deliberate revoke and hand a named peer full control again, silently. (Upstream's
    own bridges guard with ``not os.getenv(...)``; for a knob whose blank value means
    "nobody", that idiom is wrong.)

    🔴 It must not raise. Hermes catches a raising hook and logs it at **debug**, so the
    failure would be silent; this catches and logs at WARNING itself.

    Returns:
        ``None`` — the settings are carried in env, not in ``PlatformConfig.extra``.
    """
    try:
        if platform_cfg.get("allow_gateway_control") and os.getenv(ALLOW_ENV) is None:
            os.environ[ALLOW_ENV] = "1"
        handles = platform_cfg.get("gateway_control_handles")
        if handles and os.getenv(HANDLES_ENV) is None:
            # A YAML list is the documented shape; a bare string is accepted because
            # ``gateway_control_handles: ops@acme.rine.network`` is what a user writes
            # for a single peer, and reading it as a truthy non-list would otherwise
            # silently enable a handle nobody named.
            os.environ[HANDLES_ENV] = (
                handles if isinstance(handles, str) else ",".join(str(h) for h in handles)
            )
    except Exception:  # noqa: BLE001 — a raising hook is swallowed at DEBUG upstream
        logger.warning("[rine] gateway-control YAML bridge failed; env-only", exc_info=True)
    return None


__all__ = [
    "ALLOW_ENV",
    "HANDLES_ENV",
    "apply_yaml_config",
    "grants_control",
    "log_configuration",
    "optional_event_kwargs",
    "parse_control_handles",
    "switch_on",
]
