"""``python -m hermes_rine.doctor`` — the diagnosis an operator runs when nothing happens.

Every failure mode this covers is silent in production: the toolset hides itself when no
credential resolves, Hermes logs an entry-point miss at DEBUG, and an inbox nothing is
polling looks exactly like an inbox with no mail. So the value of each check is not that it
detects the condition but that it *names* it and says what to do — both are asserted here.

Network-free throughout: every test runs the offline lane.
"""

from __future__ import annotations

import os

import pytest

from hermes_rine import doctor
from hermes_rine.doctor import main, run_checks


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No ambient credentials, and a Hermes config that exists but enables nothing."""
    monkeypatch.delenv("RINE_CLIENT_ID", raising=False)
    monkeypatch.delenv("RINE_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("RINE_CONFIG_DIR", str(tmp_path / "rine"))
    (tmp_path / "rine").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))


def _check(checks, name):
    return next(c for c in checks if c.name == name)


def test_missing_credentials_are_reported_with_the_onboarding_command():
    check = _check(run_checks(offline=True), "credentials")
    assert check.status == "FAIL"
    assert "hermes_rine.onboard" in check.fix


def test_env_credentials_satisfy_the_credentials_check(monkeypatch):
    monkeypatch.setenv("RINE_CLIENT_ID", "cid")
    monkeypatch.setenv("RINE_CLIENT_SECRET", "csecret")
    assert _check(run_checks(offline=True), "credentials").status == "ok"


def test_credentials_file_satisfies_the_credentials_check(tmp_path):
    from rine._config import save_credentials

    save_credentials(str(tmp_path / "rine"), {"default": {"client_id": "c", "client_secret": "s"}})
    assert _check(run_checks(offline=True), "credentials").status == "ok"


def test_unreadable_credential_file_is_reported_as_a_permission_problem(tmp_path, monkeypatch):
    # Onboarding inside a sandbox writes keys as another user; the gateway then sees a
    # config directory that exists and cannot be read, which looks like no credentials.
    #
    # The unreadability is forced through ``os.access`` rather than ``chmod(0o000)``,
    # because root reads every file whatever the mode bits say — and the packaged rig
    # runs as root, where this test asserted the opposite of what it measured. What is
    # under test is the *reporting*, not the kernel's permission check: given an OS that
    # refuses the read, doctor must name the file and offer the chown remedy. Patching
    # the OS boundary is what makes that true for any uid.
    creds = tmp_path / "rine" / "credentials.json"
    creds.write_text("{}", encoding="utf-8")
    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode, *a, **kw: (
            False if str(path) == str(creds) else real_access(path, mode, *a, **kw)
        ),
    )

    check = _check(run_checks(offline=True), "file access")

    assert check.status == "FAIL"
    assert "credentials.json" in check.detail
    assert "chown" in check.fix


def test_readable_credentials_pass_the_file_access_check(tmp_path):
    (tmp_path / "rine" / "credentials.json").write_text("{}", encoding="utf-8")
    assert _check(run_checks(offline=True), "file access").status == "ok"


def test_plugin_not_enabled_is_reported_with_the_enable_command():
    check = _check(run_checks(offline=True), "plugin enabled")
    assert check.status == "FAIL"
    assert "hermes_rine.enable" in check.fix


def test_enabled_plugin_passes_the_activation_check(tmp_path):
    from hermes_rine.enable import enable_rine

    enable_rine(tmp_path / "hermes" / "config.yaml")
    assert _check(run_checks(offline=True), "plugin enabled").status == "ok"


def test_offline_lane_never_reaches_the_network():
    assert all(c.name != "wake channel" for c in run_checks(offline=True))


def test_exit_code_is_non_zero_while_anything_fails(capsys):
    assert main(["--offline"]) == 1
    out = capsys.readouterr().out
    assert "credentials" in out
    assert "→" in out  # the remedy, not just the verdict


def test_exit_code_is_zero_once_every_offline_check_passes(monkeypatch, tmp_path, capsys):
    from hermes_rine.enable import enable_rine

    monkeypatch.setenv("RINE_CLIENT_ID", "cid")
    monkeypatch.setenv("RINE_CLIENT_SECRET", "csecret")
    enable_rine(tmp_path / "hermes" / "config.yaml")
    # Durability is the one check whose verdict depends on where the suite itself runs:
    # inside the packaged rig the config dir is a container path on no mount, so it FAILs
    # and this test — which is about the exit code, not about durability — went red for a
    # reason unrelated to what it asserts. Pinned to the host case; the check's own three
    # arms are asserted below, which they never were before.
    monkeypatch.setattr(doctor, "_in_container", lambda: False)

    assert main(["--offline"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_durability_flags_a_container_config_dir_that_is_on_no_mount(monkeypatch):
    monkeypatch.setattr(doctor, "_in_container", lambda: True)
    monkeypatch.setattr(doctor, "_mount_point_for", lambda _path: "/")

    check = _check(run_checks(offline=True), "durability")

    assert check.status == "FAIL"
    assert "not a mount" in check.detail
    # The remedy has to say why it matters: these keys ARE the identity and no
    # regeneration recovers the handle.
    assert "cannot be regenerated" in check.fix


def test_durability_accepts_a_container_config_dir_on_a_mount(monkeypatch):
    monkeypatch.setattr(doctor, "_in_container", lambda: True)
    monkeypatch.setattr(doctor, "_mount_point_for", lambda _path: "/keys")

    assert _check(run_checks(offline=True), "durability").status == "ok"


def test_durability_is_not_a_concern_outside_a_container(monkeypatch):
    monkeypatch.setattr(doctor, "_in_container", lambda: False)

    check = _check(run_checks(offline=True), "durability")

    assert check.status == "ok"
    assert "not running in a container" in check.detail
