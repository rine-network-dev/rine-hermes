"""``register(ctx)`` wiring tests.

A fake ``PluginContext`` (``RecordingCtx``) captures every registration call so we can
assert the plugin wires exactly 25 tools + 1 skill + 1 platform (+ the opt-in
``pre_tool_call`` hook), and that nothing touches the network at import or register
time. ``register()`` only runs inside a Hermes process, so the Hermes-internal modules
it imports are resolved from the rig (or the conftest doubles on a bare host).
"""

from __future__ import annotations

import socket

import pytest

import hermes_rine
from hermes_rine import _gateway_control
from hermes_rine import platform as rine_platform

_EXPECTED_TOOLS = [
    "rine_send",
    "rine_send_and_wait",
    "rine_inbox",
    "rine_read",
    "rine_reply",
    "rine_thread",
    "rine_discover",
    "rine_discover_groups",
    "rine_inspect",
    "rine_whoami",
    "rine_groups",
    "rine_group_create",
    "rine_group_invite",
    "rine_group_remove",
    "rine_group_inspect",
    "rine_group_roster",
    "rine_group_join",
    "rine_group_invites",
    "rine_group_requests",
    "rine_group_vote",
    "rine_group_leave",
    "rine_group_sync",
    "rine_group_reclaim",
    "rine_pay",
    "rine_fulfill",
]


@pytest.fixture
def no_network(monkeypatch):
    """Trip an error if anything opens a socket during import / register."""
    original = socket.socket.connect

    def _blocked(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("register()/import must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    yield
    monkeypatch.setattr(socket.socket, "connect", original)


def test_register_records_twenty_five_tools(fake_ctx):
    hermes_rine.register(fake_ctx)
    assert len(fake_ctx.tools) == 25


def test_register_tool_names_match_the_contract(fake_ctx):
    hermes_rine.register(fake_ctx)
    names = [t["name"] for t in fake_ctx.tools]
    assert sorted(names) == sorted(_EXPECTED_TOOLS)


def test_register_records_one_skill_named_rine(fake_ctx):
    hermes_rine.register(fake_ctx)
    assert len(fake_ctx.skills) == 1
    assert fake_ctx.skills[0]["name"] == "rine"


def test_register_records_one_platform_named_rine(fake_ctx):
    hermes_rine.register(fake_ctx)
    assert len(fake_ctx.platforms) == 1
    assert fake_ctx.platforms[0]["name"] == "rine"


def test_platform_hint_tells_the_model_its_answer_is_the_reply(fake_ctx):
    # The bundled skill says the same thing, but a skill is injected once per new session
    # and this rides the stable system prompt — it is what still says it on turn ten.
    hermes_rine.register(fake_ctx)
    hint = fake_ctx.platforms[0]["platform_hint"]
    assert "final response" in hint
    assert "rine_send" in hint and "rine_reply" in hint


def test_registration_offers_the_rine_scoped_allowlist_variables(monkeypatch, fake_ctx):
    # Without these the gateway falls back to its global allow-all, which the README used
    # to instruct — one variable that opens every channel, to admit one.
    entry_fields = {
        "name",
        "label",
        "allowed_users_env",
        "allow_all_env",
        "cron_deliver_env_var",
        "apply_yaml_config_fn",
    }
    _patch_platform_entry_fields(monkeypatch, entry_fields)

    hermes_rine.register(fake_ctx)

    platform = fake_ctx.platforms[0]
    assert platform["allowed_users_env"] == "RINE_ALLOWED_HANDLES"
    assert platform["allow_all_env"] == "RINE_ALLOW_ALL_USERS"
    assert platform["cron_deliver_env_var"] == "RINE_HOME_CHANNEL"


def test_registration_offers_the_yaml_bridge_for_the_gateway_control_keys(monkeypatch, fake_ctx):
    # The only entry kwarg that is a callable rather than an env var NAME, and the only
    # way ``allow_gateway_control:`` in config.yaml reaches the adapter at all. Identity,
    # not truthiness: a bridge registered under the wrong name silently never runs, and
    # the YAML block would then be read by nobody while config.yaml looked configured.
    _patch_platform_entry_fields(monkeypatch, {"name", "label", "apply_yaml_config_fn"})

    hermes_rine.register(fake_ctx)

    assert fake_ctx.platforms[0]["apply_yaml_config_fn"] is _gateway_control.apply_yaml_config


def test_registration_offers_the_wake_authorization_seed(monkeypatch, fake_ctx):
    # 🔴 This is what stops a peer's SECOND message being silently dropped. Hermes
    # v2026.9.7 admits a busy-session event early only when it is internal AND carries
    # allow_gateway_control, which this plugin now sets False by design — so the wake
    # falls through to the gateway's default-denying authorization check unless the
    # platform's own allowlist authorizes it. Identity, not truthiness: a seed registered
    # under the wrong name never runs and the drop comes back with nothing in the
    # transcript to explain it.
    _patch_platform_entry_fields(monkeypatch, {"name", "label", "env_enablement_fn"})

    hermes_rine.register(fake_ctx)

    assert fake_ctx.platforms[0]["env_enablement_fn"] is rine_platform._wake_authorization_seed


def test_wake_seed_authorizes_every_sender_when_the_operator_named_no_allowlist(monkeypatch):
    # No wake allowlist means the adapter's own gate is already allow-all and the cold
    # path admits everyone regardless, so seeding "*" makes the busy path agree with the
    # posture the operator already has — it does not widen it.
    monkeypatch.delenv(rine_platform.ALLOWED_HANDLES_ENV, raising=False)
    monkeypatch.delenv(rine_platform.ALLOW_ALL_ENV, raising=False)

    assert rine_platform._wake_authorization_seed() == {"allowed_users": "*"}


def test_wake_seed_is_empty_when_handles_are_named(monkeypatch):
    # 🔴 The load-bearing case. An env allowlist wins over `extra` inside the gateway, so
    # a seed would be inert — but `extra.update(seed)` overwrites, and the hook takes no
    # arguments, so returning one anyway would clobber whatever the operator configured.
    # Returning nothing is what keeps this precise instead of a blanket fail-open.
    monkeypatch.setenv(rine_platform.ALLOWED_HANDLES_ENV, "ops@acme.rine.network")
    monkeypatch.delenv(rine_platform.ALLOW_ALL_ENV, raising=False)

    assert rine_platform._wake_authorization_seed() == {}


def test_wake_seed_is_empty_when_allow_all_is_set(monkeypatch):
    # The gateway already authorizes every sender via allow_all_env; a seed adds nothing
    # and would overwrite for the same reason as above.
    monkeypatch.delenv(rine_platform.ALLOWED_HANDLES_ENV, raising=False)
    monkeypatch.setenv(rine_platform.ALLOW_ALL_ENV, "true")

    assert rine_platform._wake_authorization_seed() == {}


def test_wake_seed_treats_a_blank_handle_list_as_no_allowlist(monkeypatch):
    # 🔴 Truthiness here, presence in _gateway_control.apply_yaml_config, and the
    # asymmetry is deliberate. A blank RINE_ALLOWED_HANDLES is not an allowlist of
    # nobody — adapter._parse_allowed_handles reads it as allow-all — so it is exactly
    # the case that needs seeding. A blank RINE_GATEWAY_CONTROL_HANDLES means the
    # opposite, which is why that site must not copy this test's rule.
    monkeypatch.setenv(rine_platform.ALLOWED_HANDLES_ENV, "")
    monkeypatch.delenv(rine_platform.ALLOW_ALL_ENV, raising=False)

    assert rine_platform._wake_authorization_seed() == {"allowed_users": "*"}


def test_registration_omits_variables_the_host_does_not_declare(monkeypatch, fake_ctx):
    # PlatformEntry is a dataclass: an unrecognised key raises TypeError out of its
    # constructor and takes the whole plugin down, so each key is offered only when the
    # installed Hermes declares a field for it.
    _patch_platform_entry_fields(monkeypatch, {"name", "label"})

    hermes_rine.register(fake_ctx)

    platform = fake_ctx.platforms[0]
    assert "allowed_users_env" not in platform
    assert "allow_all_env" not in platform
    assert "cron_deliver_env_var" not in platform
    # 🔴 Both callables are dropped by the same rule as the strings. Hermes 0.16.0's
    # PlatformEntry has no such field, and an undeclared kwarg is a TypeError out of the
    # dataclass constructor — the plugin would not register at all. The cost of dropping
    # env_enablement_fn is the busy-path drop on such a host; the cost of NOT dropping it
    # is no plugin at all, which is worse.
    assert "apply_yaml_config_fn" not in platform
    assert "env_enablement_fn" not in platform


def _patch_platform_entry_fields(monkeypatch, names: set[str]) -> None:
    """Stand in a ``PlatformEntry`` dataclass declaring exactly ``names``.

    The probe reads the installed dataclass, so a host whose ``PlatformEntry`` predates a
    field is reproduced by handing it one that does not declare that field.
    """
    import dataclasses
    import sys
    import types

    entry = dataclasses.make_dataclass(
        "PlatformEntry", [(n, object, dataclasses.field(default=None)) for n in sorted(names)]
    )
    module = types.ModuleType("gateway.platform_registry")
    module.PlatformEntry = entry
    module.platform_registry = types.SimpleNamespace(
        is_registered=lambda _name: True, register=lambda _entry: None
    )
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", module)


def test_register_records_pre_tool_call_hook(fake_ctx):
    hermes_rine.register(fake_ctx)
    assert len(fake_ctx.hooks) == 1
    assert fake_ctx.hooks[0]["hook_name"] == "pre_tool_call"


def test_every_tool_is_async_and_gated_by_creds_check_fn(fake_ctx):
    hermes_rine.register(fake_ctx)
    for tool in fake_ctx.tools:
        assert tool["is_async"] is True
        assert tool["toolset"] == "rine"
        # The creds gate rides on every tool so the whole toolset hides without creds.
        assert tool["check_fn"].__name__ == "rine_creds_available"


def test_each_tool_schema_is_an_openai_function_dict(fake_ctx):
    hermes_rine.register(fake_ctx)
    for tool in fake_ctx.tools:
        schema = tool["schema"]
        assert set(schema) >= {"name", "description", "parameters"}
        assert schema["name"] == tool["name"]
        assert schema["parameters"]["type"] == "object"


def test_register_touches_no_network(fake_ctx, no_network):
    # The socket guard raises if register() opens a connection.
    hermes_rine.register(fake_ctx)
    assert len(fake_ctx.tools) == 25


def test_importing_hermes_rine_exposes_version_and_register():
    assert isinstance(hermes_rine.__version__, str)
    assert callable(hermes_rine.register)
