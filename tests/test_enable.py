"""Tests for the config-based enable helper (``hermes_rine.enable``).

Drives a temp ``config.yaml`` through ``enable_rine`` and asserts it adds ``rine`` to
``plugins.enabled``, is idempotent, and preserves pre-existing config keys/entries.
"""

from __future__ import annotations

import yaml

from hermes_rine.enable import enable_rine


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
