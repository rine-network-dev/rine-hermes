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
    entry_fields = {"name", "label", "allowed_users_env", "allow_all_env", "cron_deliver_env_var"}
    _patch_platform_entry_fields(monkeypatch, entry_fields)

    hermes_rine.register(fake_ctx)

    platform = fake_ctx.platforms[0]
    assert platform["allowed_users_env"] == "RINE_ALLOWED_HANDLES"
    assert platform["allow_all_env"] == "RINE_ALLOW_ALL_USERS"
    assert platform["cron_deliver_env_var"] == "RINE_HOME_CHANNEL"


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
