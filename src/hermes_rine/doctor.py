"""``python -m hermes_rine.doctor`` — find out why the plugin is not working.

Every way this plugin goes quiet is silent by design: the toolset hides itself when no
identity resolves, Hermes logs an entry-point miss at DEBUG, and an inbox that nothing
is polling looks exactly like an inbox with no mail. This walks the same five conditions
the plugin itself checks, in the order they gate each other, and says what to do about the
first one that fails.

Runs standalone — ``hermes-agent`` need not be installed and the plugin need not be
enabled. Only the last check touches the network; ``--offline`` skips it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rine import PROFILES_FILE

_OK = "ok"
_FAIL = "FAIL"
_WARN = "warn"


@dataclass(frozen=True)
class Check:
    """One diagnosis: what was looked at, what was found, and what to do about it."""

    name: str
    status: str
    detail: str
    fix: str = ""


def _credentials(config_dir: str | None) -> Check:
    """Report whether an identity resolves at all — nothing else matters until it does."""
    if config_dir is None:
        return Check(
            "credentials",
            _FAIL,
            "the rine SDK could not resolve a config directory",
            "Run `python -m hermes_rine.onboard --email you@yourdomain.com --org-slug "
            "myorg --org-name 'My Org' --agent-name assistant`, or set RINE_CONFIG_DIR.",
        )
    path = Path(config_dir) / PROFILES_FILE
    if path.is_file():
        return Check("credentials", _OK, f"found {path}")
    return Check(
        "credentials",
        _FAIL,
        f"no {PROFILES_FILE} in {config_dir}",
        "Onboard with `python -m hermes_rine.onboard`, or point RINE_CONFIG_DIR at the "
        f"directory that already holds {PROFILES_FILE} and the agent's keys.",
    )


def _readable(config_dir: str | None) -> Check:
    """Report whether this process's user can read the profile and key files."""
    if config_dir is None:
        return Check("file access", _WARN, "no config directory to check")
    unreadable = [
        str(p)
        for p in sorted(Path(config_dir).glob("**/*"))
        if p.is_file() and not os.access(p, os.R_OK)
    ]
    if not unreadable:
        return Check("file access", _OK, f"uid {os.getuid()} can read {config_dir}")
    return Check(
        "file access",
        _FAIL,
        f"uid {os.getuid()} cannot read: {', '.join(unreadable[:3])}",
        "Onboarding wrote these as another user. Run the gateway as that user, or "
        "`chown -R` the config directory to the user Hermes runs as.",
    )


def _durable(config_dir: str | None) -> Check:
    """Report whether the keys survive the container they were written in."""
    if config_dir is None:
        return Check("durability", _WARN, "no config directory to check")
    if not _in_container():
        return Check("durability", _OK, "not running in a container")
    if _mount_point_for(Path(config_dir).resolve()) != "/":
        return Check("durability", _OK, f"{config_dir} is on a mount")
    return Check(
        "durability",
        _FAIL,
        f"{config_dir} is inside the container filesystem, not a mount",
        "The agent's leaf keys live here. Recreating the container loses them, and "
        "getting them back needs the org's recovery phrase (`rine org recover`). "
        "Mount a host directory and point RINE_CONFIG_DIR at it.",
    )


def _enabled() -> Check:
    """Report whether ``rine`` is in ``plugins.enabled`` — the loader activates nothing else."""
    from hermes_rine.enable import enabled_plugins, resolve_config_path

    path = resolve_config_path()
    try:
        plugins = enabled_plugins(path)
    except Exception as err:  # noqa: BLE001 — unreadable/invalid YAML is itself the answer
        return Check(
            "plugin enabled",
            _FAIL,
            f"could not read {path}: {err}",
            "Fix the YAML, then run `python -m hermes_rine.enable`.",
        )
    if "rine" in plugins:
        return Check("plugin enabled", _OK, f"rine is in plugins.enabled ({path})")
    return Check(
        "plugin enabled",
        _FAIL,
        f"rine is not in plugins.enabled ({path})",
        "Run `hermes plugins enable rine --no-allow-tool-override`, or "
        "`python -m hermes_rine.enable` for the same config edit with no prompt and no "
        "Hermes CLI on the path.",
    )


def _authenticates(config_dir: str | None) -> Check:
    """Report whether those credentials authenticate, and as which agent."""
    from rine import SyncRineClient

    try:
        client = SyncRineClient(config_dir=config_dir, agent_only=True)
        with client:
            me = client.whoami()
    except Exception as err:  # noqa: BLE001 — report the reason rather than a traceback
        from hermes_rine._client import _format_error, live_handles

        return Check(
            "wake channel",
            _FAIL,
            _format_error(err),
            "The wake channel authenticates exactly this way on connect, so it will fail "
            "the same way until this does.",
        )
    handles = live_handles(me)
    if not handles:
        return Check(
            "wake channel",
            _FAIL,
            "authenticated, but this org has no live agent",
            "Create one with `python -m hermes_rine.onboard --agent-name assistant`.",
        )
    transport = os.getenv("RINE_TRANSPORT", "poll")
    return Check("wake channel", _OK, f"{handles[0]} would wake over {transport}")


def _in_container() -> bool:
    """True when this process looks containerized (Docker/Podman/containerd)."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "libpod"))


def _mount_point_for(path: Path) -> str:
    """The longest mount point containing ``path`` (``/`` when it is on no other mount)."""
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "/"
    best = "/"
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        point = fields[4]
        if (path == Path(point) or point in {str(p) for p in path.parents}) and len(point) > len(
            best
        ):
            best = point
    return best


def run_checks(*, offline: bool = False) -> list[Check]:
    """Run every diagnosis in gating order and return the results."""
    try:
        from rine import resolve_config_dir

        config_dir: str | None = resolve_config_dir()
    except Exception:  # noqa: BLE001 — unresolvable is a finding, not a crash
        config_dir = None
    checks = [_credentials(config_dir), _readable(config_dir), _durable(config_dir), _enabled()]
    if not offline:
        checks.append(_authenticates(config_dir))
    return checks


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for ``python -m hermes_rine.doctor``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m hermes_rine.doctor",
        description="Diagnose a hermes-rine install: credentials, file access, "
        "durability, plugin activation, and the wake channel.",
    )
    parser.add_argument("--offline", action="store_true", help="Skip the network check.")
    args = parser.parse_args(argv)

    failed = False
    for check in run_checks(offline=args.offline):
        print(f"[{check.status:>4}] {check.name}: {check.detail}")
        if check.status == _FAIL:
            failed = True
            print(f"       → {check.fix}")
    if failed:
        print("\nFix the first failure above and run this again — each one gates the next.")
        return 1
    print("\nEverything checks out. Run `hermes gateway run` to start waking on new mail.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
