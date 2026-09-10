"""``python -m hermes_rine.enable`` — idempotently enable the ``rine`` plugin.

Hermes' plugin **loader** finds a pip entry-point plugin like this one — it scans
``importlib.metadata`` at startup — and activates whatever ``plugins.enabled`` lists.
``hermes plugins enable rine`` writes that same key, and works. It asks one question
**each time it runs** — being already enabled does not skip it — about whether the plugin
may replace built-in tools (rine never does, so the answer is no), and waits on stdin for
the answer unless ``--no-allow-tool-override`` answers it up front. This helper makes the
same config edit with no prompt at all and no Hermes CLI on the path: it adds ``rine``
to ``plugins.enabled``, idempotently, so the user never has to hand-edit YAML.

Config-path resolution (first that works):
  1. ``hermes_cli.config.get_config_path()`` (authoritative; honours ``HERMES_HOME`` and
     profiles) — used when running inside the Hermes venv;
  2. ``$HERMES_HOME/config.yaml`` if ``HERMES_HOME`` is set;
  3. ``%LOCALAPPDATA%\\hermes\\config.yaml`` on Windows, ``~/.hermes/config.yaml``
     everywhere else.

YAML round-trips through ``ruamel.yaml`` (preserves comments/formatting) when available,
else ``pyyaml``. Import is side-effect-free: no path resolution, no file reads, no YAML
library import happens until ``enable_rine`` / ``main`` is called.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def resolve_config_path() -> Path:
    """Resolve the Hermes ``config.yaml`` path, degrading gracefully off the Hermes venv."""
    try:  # inside the Hermes venv this is authoritative (honours HERMES_HOME + profiles).
        from hermes_cli.config import get_config_path

        return Path(get_config_path())
    except Exception:  # noqa: BLE001 — Hermes not importable: fall back to the env / default.
        pass
    import os

    home = os.getenv("HERMES_HOME")
    if home:
        base = Path(home)
    elif sys.platform == "win32":
        # Hermes puts its home under LOCALAPPDATA on Windows, so a fallback to
        # ``~/.hermes`` there would write a second config the loader never reads and
        # report success. Only this branch is a guess-free copy of that rule; the
        # authoritative resolution above is used whenever Hermes is importable.
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
    else:
        base = Path.home() / ".hermes"
    return base / "config.yaml"


def _load(path: Path) -> tuple[dict[str, Any], object | None]:
    """Load the config as a dict, returning ``(data, ruamel_yaml_or_None)``.

    When ``ruamel.yaml`` is available a round-trip loader is returned alongside the data
    so ``_dump`` can preserve comments/formatting; otherwise ``None`` (pyyaml path).
    A missing or empty file yields an empty dict (we create it).
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(text) if text.strip() else None
        return (data if isinstance(data, dict) else {}), yaml
    except ImportError:
        import yaml as pyyaml

        data = pyyaml.safe_load(text) if text.strip() else None
        return (data if isinstance(data, dict) else {}), None


def _dump(path: Path, data: dict[str, Any], ruamel: object | None) -> None:
    """Write the config back, via ruamel (round-trip) or pyyaml."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if ruamel is not None:
        import io

        buf = io.StringIO()
        ruamel.dump(data, buf)  # type: ignore[attr-defined]
        path.write_text(buf.getvalue(), encoding="utf-8")
        return
    import yaml as pyyaml

    path.write_text(pyyaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def enabled_plugins(config_path: Path | None = None) -> list[str]:
    """The ``plugins.enabled`` list from the Hermes config (empty when absent)."""
    data, _ = _load(config_path or resolve_config_path())
    plugins = data.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    return [str(p) for p in enabled] if isinstance(enabled, list) else []


def enable_rine(config_path: Path | None = None, *, plugin: str = "rine") -> bool:
    """Add ``plugin`` to ``plugins.enabled`` in the Hermes config, idempotently.

    Args:
        config_path: Override the config path (defaults to :func:`resolve_config_path`).
        plugin: Plugin id to enable.

    Returns:
        True if the config was changed, False if the plugin was already enabled.
    """
    path = config_path or resolve_config_path()
    data, ruamel = _load(path)

    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
        plugins["enabled"] = enabled

    if plugin in enabled:
        return False
    enabled.append(plugin)
    _dump(path, data, ruamel)
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``python -m hermes_rine.enable``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m hermes_rine.enable",
        description="Enable the rine plugin by adding it to plugins.enabled in config.yaml.",
    )
    parser.add_argument(
        "--config", default=None, help="Path to config.yaml (default: Hermes resolution)."
    )
    args = parser.parse_args(argv)

    try:
        path = Path(args.config) if args.config else resolve_config_path()
        changed = enable_rine(path)
    except Exception as err:  # noqa: BLE001 — top-level CLI: print + exit non-zero.
        print(f"Could not enable the rine plugin: {err}", file=sys.stderr)
        return 1
    if changed:
        print(f"Enabled the rine plugin in {path}. Run `hermes gateway run` to start.")
    else:
        print(f"The rine plugin is already enabled in {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
