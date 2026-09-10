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
import os
from typing import Any

from hermes_rine import _gateway_control
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

#: The two variables that give the gateway a real wake allowlist. Named once because
#: :data:`_OPTIONAL_ENTRY_KWARGS` hands them to the gateway and
#: :func:`_wake_authorization_seed` has to read the same two back.
ALLOWED_HANDLES_ENV = "RINE_ALLOWED_HANDLES"
ALLOW_ALL_ENV = "RINE_ALLOW_ALL_USERS"

_ALLOW_EVERY_SENDER = "*"


def _wake_authorization_seed() -> dict[str, Any]:
    """Authorize rine senders at the gateway when the operator has named no allowlist.

    🔴 Without this, a peer's **second** message is silently dropped. Hermes v2026.9.7
    rewrote the busy-session path — the one that runs when a message arrives while a turn
    is still going — to admit an event early only when it is ``internal`` **and** carries
    ``allow_gateway_control``. This plugin now sets that flag False on every inbound
    message by design, so a rine wake no longer takes the early exit and falls through to
    the gateway's own authorization check, which default-**denies**. First contact works;
    the follow-up vanishes with nothing in the transcript. Measured, not inferred: all four
    cells — cold/busy against the flag True/False — were driven through a real gateway, and
    only busy-plus-False drops. The cold path deliberately does *not* key on the flag, so
    the flag is not the whole story and reading the code alone would have got this wrong.

    🔴 The flag is **not** the cause at the advertised floor, and the seed is not optional
    there either. At v2026.8.31 the busy handler has no early admission at all — it calls
    the same default-denying check as its first statement — so the mid-turn message dropped
    there before this package ever touched the flag, and drops with the flag *set*. Two
    different upstream shapes, one repair: ``env_enablement_fn`` is a ``PlatformEntry``
    field at the floor as well as at the pin, so the seed registers and fixes both. Do not
    rewrite this as a 9.7-only story — that reading was written down once and measured
    false.

    Both apply to ``chat_type="group"`` exactly as to ``"dm"``. There is no second key to
    seed: the group arm falls back to the same ``extra["allowed_users"]`` because this
    entry declares ``allowed_users_env``, proved by clearing that field and watching the
    group verdict flip to dropped and back.

    The seed lands in ``PlatformConfig.extra`` via ``env_enablement_fn`` — the same sweep
    that already auto-enables rine once credentials resolve, so it demonstrably runs with
    no ``rine:`` block in ``config.yaml``. ``apply_yaml_config_fn`` cannot carry it: the
    loader calls that hook only when such a block exists, and our documented install has
    none.

    🔴 It returns **nothing** when either variable is set, and that is what keeps this
    precise rather than a blanket fail-open. An env allowlist takes precedence over
    ``extra`` inside the gateway, so a seed would be inert there anyway — but returning it
    would still overwrite whatever the operator put in ``extra``, because the hook takes no
    arguments and cannot see the config it is about to update.

    The one shape that can still be overwritten is an undocumented hand-written
    ``platforms.rine.extra.allowed_users`` in ``config.yaml`` with neither variable set.
    That is not a widening in effect: with no wake allowlist, the adapter's own gate is
    already allow-all and the cold path admits every sender regardless, so the operator is
    reachable by anyone today and this only makes the busy path agree with the cold one.
    It is named here so it reads as a known edge rather than an oversight.

    Truthiness, not presence — the inverse of ``_gateway_control.apply_yaml_config``, and
    deliberately: a blank ``RINE_ALLOWED_HANDLES`` is not an allowlist of nobody, it is no
    allowlist at all (``adapter._parse_allowed_handles`` reads it as allow-all), so the
    blank case is exactly the case that needs seeding. A blank
    ``RINE_GATEWAY_CONTROL_HANDLES`` means the opposite, which is why that site tests
    presence.

    Returns:
        ``{"allowed_users": "*"}`` when no wake allowlist is configured, else ``{}`` — the
        gateway treats an empty dict as no seed at all.
    """
    if os.getenv(ALLOWED_HANDLES_ENV) or os.getenv(ALLOW_ALL_ENV):
        return {}
    return {"allowed_users": _ALLOW_EVERY_SENDER}


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
#
# ``apply_yaml_config_fn`` is the one value here that is a callable rather than an env var
# NAME, which is why the dict is annotated ``dict[str, Any]``. It lets the two
# gateway-control settings be written in ``config.yaml`` instead of the environment; see
# :mod:`hermes_rine._gateway_control`.
# ``env_enablement_fn`` is the second callable: it seeds the gateway's per-platform
# allowlist so a peer's second message is queued rather than dropped. See
# :func:`_wake_authorization_seed` — that drop is the one thing in this release a code
# reading alone would have shipped.
_OPTIONAL_ENTRY_KWARGS: dict[str, Any] = {
    "allowed_users_env": ALLOWED_HANDLES_ENV,
    "allow_all_env": ALLOW_ALL_ENV,
    "cron_deliver_env_var": "RINE_HOME_CHANNEL",
    "apply_yaml_config_fn": _gateway_control.apply_yaml_config,
    "env_enablement_fn": _wake_authorization_seed,
}


def _supported_entry_kwargs() -> dict[str, Any]:
    """The subset of :data:`_OPTIONAL_ENTRY_KWARGS` the installed ``PlatformEntry`` accepts.

    🔴 ``_gateway_control.optional_event_kwargs`` filters the same way for ``MessageEvent``
    and the two are deliberately NOT one helper. This one fails **closed** — an entry
    kwarg the host does not declare is a ``TypeError`` out of the ``PlatformEntry``
    constructor, which takes the whole plugin down at registration, so the safe answer to
    "I could not introspect" is to offer nothing. That one fails **open**, because the
    suite's ``MessageEvent`` double is a plain ``**kwargs`` class that
    ``dataclasses.fields`` refuses. Consolidating them onto either arm breaks the other
    caller; the reasons are written out at both sites so the divergence reads as a
    decision rather than an oversight.
    """
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
