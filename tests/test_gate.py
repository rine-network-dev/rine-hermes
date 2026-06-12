"""Tests for the rine toolset gate + opt-in confirm-veto (SPEC §5 "Gate").

The creds ``check_fn`` and the confirm-veto are cheap, network-free policies. We never
construct a client or touch the network here — only env vars + a config dir on disk.
"""

from __future__ import annotations

import pytest

from hermes_rine._gate import make_confirm_veto, rine_creds_available


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Isolate every gate test from ambient rine creds.

    Clears the env credentials AND points ``RINE_CONFIG_DIR`` at an empty dir so the
    SDK's config-dir resolution lands somewhere with no ``credentials.json`` — the
    "no creds" baseline. Tests opt back in by setting env vars / writing the file.
    """
    monkeypatch.delenv("RINE_CLIENT_ID", raising=False)
    monkeypatch.delenv("RINE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("RINE_REQUIRE_CONFIRM", raising=False)
    monkeypatch.setenv("RINE_CONFIG_DIR", str(tmp_path))


class TestCredsAvailable:
    def test_no_creds_returns_false(self):
        assert rine_creds_available() is False

    def test_env_client_id_and_secret_returns_true(self, monkeypatch):
        monkeypatch.setenv("RINE_CLIENT_ID", "cid")
        monkeypatch.setenv("RINE_CLIENT_SECRET", "csecret")
        assert rine_creds_available() is True

    def test_only_client_id_set_returns_false(self, monkeypatch):
        monkeypatch.setenv("RINE_CLIENT_ID", "cid")
        # Secret missing → not resolvable from env.
        assert rine_creds_available() is False

    def test_credentials_file_in_config_dir_returns_true(self, tmp_path, monkeypatch):
        from rine._config import save_credentials

        save_credentials(str(tmp_path), {"default": {"client_id": "c", "client_secret": "s"}})
        monkeypatch.setenv("RINE_CONFIG_DIR", str(tmp_path))
        assert rine_creds_available() is True


class TestConfirmVeto:
    def test_veto_off_by_default_returns_none_for_mutating_tool(self):
        veto = make_confirm_veto()
        # RINE_REQUIRE_CONFIRM unset → unattended dispatch, even for a mutating tool.
        assert veto(tool_name="rine_send") is None

    def test_veto_blocks_mutating_tool_when_enabled(self, monkeypatch):
        monkeypatch.setenv("RINE_REQUIRE_CONFIRM", "1")
        veto = make_confirm_veto()

        result = veto(tool_name="rine_send")

        assert result is not None
        assert result["action"] == "block"
        assert "rine_send" in result["message"]

    def test_veto_enabled_does_not_block_read_only_tool(self, monkeypatch):
        monkeypatch.setenv("RINE_REQUIRE_CONFIRM", "1")
        veto = make_confirm_veto()
        # rine_read / rine_discover / rine_check_inbox are not mutating.
        assert veto(tool_name="rine_read") is None
        assert veto(tool_name="rine_discover") is None
        assert veto(tool_name="rine_check_inbox") is None

    def test_veto_enabled_ignores_non_rine_tool(self, monkeypatch):
        monkeypatch.setenv("RINE_REQUIRE_CONFIRM", "1")
        veto = make_confirm_veto()
        assert veto(tool_name="terminal_run") is None

    def test_veto_read_live_so_toggle_takes_effect_without_rebuild(self, monkeypatch):
        """The env var is read on each call — an operator can toggle without a restart."""
        veto = make_confirm_veto()
        assert veto(tool_name="rine_group_remove") is None
        monkeypatch.setenv("RINE_REQUIRE_CONFIRM", "1")
        assert veto(tool_name="rine_group_remove") is not None
