"""Tests for the config-based enable helper (``hermes_rine.enable``).

Drives a temp ``config.yaml`` through ``enable_rine`` and asserts it adds ``rine`` to
``plugins.enabled``, is idempotent, and preserves pre-existing config keys/entries.

``resolve_config_path``'s fallback ladder is tested with Hermes forced un-importable,
because in the rig the authoritative first rung always wins and the fallback — the rung
that runs on the machine of anyone who has not installed Hermes into this venv — would
otherwise never execute here at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from hermes_rine.enable import enable_rine, resolve_config_path


def _read(path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def test_enable_adds_rine_to_empty_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model: somemodel\n")
    changed = enable_rine(cfg)
    assert changed is True
    data = _read(cfg)
    assert data["plugins"]["enabled"] == ["rine"]
    assert data["model"] == "somemodel"  # existing keys preserved


def test_enable_creates_config_when_missing(tmp_path):
    cfg = tmp_path / "nested" / "config.yaml"  # parent dir does not exist yet
    changed = enable_rine(cfg)
    assert changed is True
    assert cfg.exists()
    assert _read(cfg)["plugins"]["enabled"] == ["rine"]


def test_enable_is_idempotent(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  enabled:\n    - rine\n")
    changed = enable_rine(cfg)
    assert changed is False  # already enabled → no change
    assert _read(cfg)["plugins"]["enabled"] == ["rine"]


def test_enable_preserves_other_enabled_plugins(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("plugins:\n  enabled:\n    - other\n")
    changed = enable_rine(cfg)
    assert changed is True
    assert _read(cfg)["plugins"]["enabled"] == ["other", "rine"]


# --- the fallback ladder, with Hermes' own resolver out of reach ---------------------


@pytest.fixture
def without_hermes_cli(monkeypatch, tmp_path):
    """Force ``resolve_config_path`` past its authoritative rung, and pin ``~``."""
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    return tmp_path


def test_hermes_home_wins_on_every_platform(without_hermes_cli, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(without_hermes_cli / "explicit"))
    monkeypatch.setattr(sys, "platform", "win32")

    assert resolve_config_path() == without_hermes_cli / "explicit" / "config.yaml"


def test_windows_falls_back_to_localappdata(without_hermes_cli, monkeypatch):
    # 🔴 Not ``~/.hermes``: on Windows Hermes keeps its home under LOCALAPPDATA, so the
    # POSIX default would write a config the loader never reads and report success.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(without_hermes_cli / "AppData" / "Local"))

    assert (
        resolve_config_path()
        == without_hermes_cli / "AppData" / "Local" / "hermes" / "config.yaml"
    )


def test_windows_without_localappdata_uses_the_conventional_path(without_hermes_cli, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    assert resolve_config_path() == (
        without_hermes_cli / "home" / "AppData" / "Local" / "hermes" / "config.yaml"
    )


def test_posix_still_falls_back_to_dot_hermes(without_hermes_cli, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert resolve_config_path() == without_hermes_cli / "home" / ".hermes" / "config.yaml"
