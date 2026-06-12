"""``register(ctx)`` wiring tests (SPEC §5 "Skill / register").

A fake ``PluginContext`` (``RecordingCtx``) captures every registration call so we can
assert the plugin wires exactly 12 tools + 1 skill + 1 platform (+ the opt-in
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
    "rine_check_inbox",
    "rine_read",
    "rine_reply",
    "rine_discover",
    "rine_inspect",
    "rine_whoami",
    "rine_group_create",
    "rine_group_invite",
    "rine_group_remove",
    "rine_group_inspect",
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


def test_register_records_twelve_tools(fake_ctx):
    hermes_rine.register(fake_ctx)
    assert len(fake_ctx.tools) == 12


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
    assert len(fake_ctx.tools) == 12


def test_importing_hermes_rine_exposes_version_and_register():
    assert isinstance(hermes_rine.__version__, str)
    assert callable(hermes_rine.register)
