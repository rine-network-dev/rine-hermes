"""Whether an inbound rine peer may drive the operator's Hermes gateway.

``MessageEvent.allow_gateway_control`` defaults **True** upstream, so before this knob a
peer's ``/new`` or ``/model …`` arriving as a rine DM ran as a gateway command and a
peer's "yes" resolved an approval prompt the operator was asked. The rule is now: control
iff the channel switch is on **and** the sender is named.

Each half of that conjunction is tested on its own, because either half alone silently
grants nothing (or, if the parse were shared with the wake allowlist, silently grants
everything — see ``parse_control_handles``' own docstring).
"""

from __future__ import annotations

import dataclasses

import pytest

from hermes_rine._gateway_control import (
    ALLOW_ENV,
    HANDLES_ENV,
    apply_yaml_config,
    grants_control,
    log_configuration,
    optional_event_kwargs,
    parse_control_handles,
    switch_on,
)
from hermes_rine._poll import _dispatch

ALICE = "alice"
BOB = "bob@other.rine.network"


# --- the two parsers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("0", False), ("", False), ("true", False), ("yes", False), (None, False)],
)
def test_switch_is_the_literal_one_and_nothing_else(raw, expected) -> None:
    # Same convention as RINE_REQUIRE_VERIFIED: one truthiness rule per package, and a
    # setting whose wrong answer hands a stranger the gateway does not guess at "true".
    assert switch_on(raw) is expected


def test_an_unset_allowlist_names_nobody_not_everybody() -> None:
    # 🔴 The inverse of ``adapter._parse_allowed_handles``, whose None means allow-all.
    # If this returned None the switch alone would open the gateway to every admitted
    # sender — the exact defect the knob exists to close.
    assert parse_control_handles("") == frozenset()
    assert parse_control_handles("  ,  ") == frozenset()


def test_a_star_means_every_sender_the_wake_allowlist_already_admits() -> None:
    assert parse_control_handles("*") is None
    assert parse_control_handles("alice, *") is None


def test_named_handles_are_split_and_stripped() -> None:
    assert parse_control_handles(" alice , bob@other.rine.network ") == frozenset({ALICE, BOB})


# --- the predicate ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "raw_handles", "sender", "expected"),
    [
        # switch off, list unset -> off for everyone (the default posture)
        (False, "", ALICE, False),
        # switch on, list unset/blank -> off; a switch alone grants nothing
        (True, "", ALICE, False),
        # switch on, named -> those senders only
        (True, "alice,bob@other.rine.network", ALICE, True),
        (True, "alice,bob@other.rine.network", BOB, True),
        (True, "alice", BOB, False),
        # switch on, star -> every sender the wake allowlist already admitted
        (True, "*", BOB, True),
        # switch off, named -> the list is inert
        (False, "alice", ALICE, False),
        # an unattributable sender is never granted under a NAMED list — trivially, it is
        # not in it
        (True, "alice", "", False),
        # ...and not under a star either. That row is the one that used to fail: the star
        # arm returned before the sender was read.
        (True, "*", "", False),
    ],
)
def test_control_truth_table(enabled, raw_handles, sender, expected) -> None:
    assert (
        grants_control(enabled=enabled, handles=parse_control_handles(raw_handles), sender=sender)
        is expected
    )


def test_a_star_denies_a_sender_with_no_resolvable_handle() -> None:
    # 🔴 ``_sender_handle`` returns "" for a frame carrying neither a sender handle nor a
    # from-agent id. An operator cannot have trusted a peer they cannot name, and the
    # audit line for such a grant would read "gateway command from ''". The wake allowlist
    # having admitted the frame is not an argument: it is allow-all by default, so under
    # the star arm this was every unattributable frame on rine.
    assert grants_control(enabled=True, handles=None, sender="") is False


# --- the half-configured warnings ------------------------------------------------


def test_a_named_list_with_the_switch_off_says_so(caplog) -> None:
    with caplog.at_level("WARNING", logger="hermes_rine.gateway_control"):
        log_configuration(enabled=False, handles=frozenset({ALICE}), wake_allowlist_named=False)
    assert HANDLES_ENV in caplog.text and ALLOW_ENV in caplog.text


def test_the_switch_with_no_named_list_says_so(caplog) -> None:
    with caplog.at_level("WARNING", logger="hermes_rine.gateway_control"):
        log_configuration(enabled=True, handles=frozenset(), wake_allowlist_named=False)
    assert HANDLES_ENV in caplog.text


def test_a_star_behind_a_named_wake_allowlist_is_bounded_by_it(caplog) -> None:
    with caplog.at_level("WARNING", logger="hermes_rine.gateway_control"):
        log_configuration(enabled=True, handles=None, wake_allowlist_named=True)
    assert "EVERY sender RINE_ALLOWED_HANDLES admits" in caplog.text
    assert "EVERY AGENT ON RINE" not in caplog.text


def test_a_star_with_no_wake_allowlist_says_every_agent_on_rine(caplog) -> None:
    # 🔴 The out-of-the-box posture: RINE_ALLOWED_HANDLES unset is allow-all, so ``*`` here
    # is not narrowed by anything. The milder wording above described a configuration the
    # docs do not recommend and most operators do not have.
    with caplog.at_level("WARNING", logger="hermes_rine.gateway_control"):
        log_configuration(enabled=True, handles=None, wake_allowlist_named=False)
    assert "EVERY AGENT ON RINE" in caplog.text


def test_a_coherently_off_knob_is_silent(caplog) -> None:
    with caplog.at_level("WARNING", logger="hermes_rine.gateway_control"):
        log_configuration(enabled=False, handles=frozenset(), wake_allowlist_named=False)
    assert caplog.text == ""


def test_the_enabled_log_counts_peers_without_naming_them(caplog) -> None:
    with caplog.at_level("INFO", logger="hermes_rine.gateway_control"):
        log_configuration(
            enabled=True, handles=frozenset({ALICE, BOB}), wake_allowlist_named=False
        )
    assert "2 named peer" in caplog.text
    assert ALICE not in caplog.text and BOB not in caplog.text


# --- the introspection guard ------------------------------------------------------


def test_an_unknown_kwarg_is_dropped_for_a_real_dataclass() -> None:
    # This is what keeps the plugin loading on a host below the advertised floor: an
    # undeclared kwarg to MessageEvent is a TypeError raised inside _dispatch, which
    # stops the plugin waking on mail at all.
    @dataclasses.dataclass
    class Narrow:
        text: str

    assert optional_event_kwargs(Narrow, allow_gateway_control=False) == {}
    assert optional_event_kwargs(Narrow, text="t") == {"text": "t"}


def test_every_kwarg_survives_a_non_dataclass_event_class() -> None:
    # 🔴 Load-bearing: this suite's own MessageEvent double is a plain **kwargs class,
    # so dataclasses.fields raises on it. A guard without the except-TypeError arm would
    # drop the kwarg under the double and make correct code fail every wake test here.
    class Loose:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    assert optional_event_kwargs(Loose, allow_gateway_control=True) == {
        "allow_gateway_control": True
    }


# --- the YAML bridge ---------------------------------------------------------------


def test_yaml_seeds_both_settings(monkeypatch) -> None:
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.delenv(HANDLES_ENV, raising=False)
    import os

    assert (
        apply_yaml_config(
            {}, {"allow_gateway_control": True, "gateway_control_handles": [ALICE, BOB]}
        )
        is None
    )
    assert os.environ[ALLOW_ENV] == "1"
    assert os.environ[HANDLES_ENV] == f"{ALICE},{BOB}"


def test_a_single_handle_written_as_a_bare_string_is_honoured(monkeypatch) -> None:
    # ``gateway_control_handles: ops@acme.rine.network`` is what a user writes for one
    # peer. Read as a truthy non-list it would have joined its characters with commas.
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.delenv(HANDLES_ENV, raising=False)
    import os

    apply_yaml_config({}, {"gateway_control_handles": BOB})
    assert os.environ[HANDLES_ENV] == BOB


def test_env_wins_over_yaml(monkeypatch) -> None:
    monkeypatch.setenv(ALLOW_ENV, "0")
    monkeypatch.setenv(HANDLES_ENV, ALICE)
    apply_yaml_config({}, {"allow_gateway_control": True, "gateway_control_handles": [BOB]})
    import os

    assert os.environ[ALLOW_ENV] == "0"
    assert os.environ[HANDLES_ENV] == ALICE


def test_a_deliberately_blanked_env_var_is_not_refilled_by_yaml(monkeypatch) -> None:
    # 🔴 ``export RINE_GATEWAY_CONTROL_HANDLES=""`` is this setting's revoke gesture:
    # ``parse_control_handles("")`` is an empty set, which denies everyone. Guarding the
    # bridge with ``not os.getenv(...)`` — the idiom upstream's own bridges use — would
    # write the YAML list back over that revoke and hand the named peer control again,
    # silently. Present, not truthy.
    monkeypatch.setenv(ALLOW_ENV, "")
    monkeypatch.setenv(HANDLES_ENV, "")
    apply_yaml_config({}, {"allow_gateway_control": True, "gateway_control_handles": [BOB]})
    import os

    assert os.environ[ALLOW_ENV] == ""
    assert os.environ[HANDLES_ENV] == ""
    assert parse_control_handles(os.environ[HANDLES_ENV]) == frozenset()


def test_a_garbage_block_is_logged_not_raised(monkeypatch, caplog) -> None:
    # 🔴 Hermes catches a raising hook and logs it at DEBUG, so a raise here would be a
    # silent plugin-wide config failure. Warn loudly and carry on env-only instead.
    monkeypatch.delenv(HANDLES_ENV, raising=False)

    class Hostile:
        def __iter__(self):
            raise RuntimeError("not iterable after all")

        def __bool__(self) -> bool:
            return True

    with caplog.at_level("WARNING", logger="hermes_rine.gateway_control"):
        assert apply_yaml_config({}, {"gateway_control_handles": Hostile()}) is None
    assert "YAML bridge failed" in caplog.text


# --- the wake path ------------------------------------------------------------------


async def test_a_peer_does_not_drive_the_gateway_by_default(make_adapter, make_message) -> None:
    adapter = make_adapter()
    await _dispatch(adapter, make_message(sender_handle=ALICE))
    [event] = adapter.events
    assert event.allow_gateway_control is False


async def test_a_named_peer_drives_the_gateway(make_adapter, make_message) -> None:
    adapter = make_adapter(gateway_control=True, gateway_control_handles=ALICE)
    await _dispatch(adapter, make_message(sender_handle=ALICE))
    [event] = adapter.events
    assert event.allow_gateway_control is True


async def test_an_unnamed_peer_does_not_drive_the_gateway(make_adapter, make_message) -> None:
    # The peer still wakes the agent — the wake allowlist is a separate, wider gate —
    # it just cannot issue commands once awake.
    adapter = make_adapter(gateway_control=True, gateway_control_handles=BOB)
    await _dispatch(adapter, make_message(sender_handle=ALICE))
    [event] = adapter.events
    assert event.allow_gateway_control is False


# --- the real adapter's own env reads -----------------------------------------------
#
# 🔴 Everything above this line drives ``FakeAdapter``, which takes the posture from
# ``opts`` rather than from the process env, and every other adapter suite builds its
# adapter with ``RineAdapter.__new__`` and hand-sets attributes. So the two
# ``os.getenv`` calls in ``RineAdapter.__init__`` — the wiring that decides the DEFAULT
# a real operator gets — were read by no test at all: swapping ``switch_on`` for
# ``parse_control_handles`` there, or dropping the switch read entirely, would have left
# the whole file green. These build the real thing.


def _adapter_from_env(monkeypatch, tmp_path, **env: str):
    """A real ``RineAdapter`` whose gateway-control env is exactly ``env``."""
    from gateway.config import PlatformConfig  # type: ignore[import-not-found]

    from hermes_rine.adapter import RineAdapter

    # The journal path resolves through the rine config dir and makedirs it; point it
    # at the test's own tmp so constructing an adapter never touches a real one.
    monkeypatch.setenv("RINE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    monkeypatch.delenv(HANDLES_ENV, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return RineAdapter(PlatformConfig())


def test_an_out_of_the_box_adapter_grants_control_to_nobody(monkeypatch, tmp_path) -> None:
    adapter = _adapter_from_env(monkeypatch, tmp_path)
    assert adapter.allows_gateway_control(ALICE) is False


def test_the_switch_alone_grants_control_to_nobody(monkeypatch, tmp_path) -> None:
    adapter = _adapter_from_env(monkeypatch, tmp_path, **{ALLOW_ENV: "1"})
    assert adapter.allows_gateway_control(ALICE) is False


def test_a_named_list_alone_grants_control_to_nobody(monkeypatch, tmp_path) -> None:
    adapter = _adapter_from_env(monkeypatch, tmp_path, **{HANDLES_ENV: ALICE})
    assert adapter.allows_gateway_control(ALICE) is False


def test_both_halves_grant_control_to_the_named_peer_only(monkeypatch, tmp_path) -> None:
    adapter = _adapter_from_env(monkeypatch, tmp_path, **{ALLOW_ENV: "1", HANDLES_ENV: ALICE})
    assert adapter.allows_gateway_control(ALICE) is True
    assert adapter.allows_gateway_control(BOB) is False
    assert adapter.allows_gateway_control("") is False
