"""``rine_retry_unreadable`` — the on-demand half of the set-aside list.

Driven through the handler's public async signature, with only the rine HTTP boundary
mocked (respx): the SDK's crypto, config resolution and HTTP are real, and so is the
sqlite set-aside store the wake channel would have written. The three things a mutation
must not survive:

- the re-read carries ``X-Rine-Peek``, so answering the question does not consume the
  message the operator is trying to recover;
- an authenticity-refused row is LISTED and never re-read;
- the attempt counter is what the verb reports, so an operator can tell "not yet" from
  "never".
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from rine._constants import PEEK_HEADER, EncryptionVersion

from hermes_rine._setaside import SETASIDE_MAX_ATTEMPTS, SetAsideReason, SetAsideStore
from hermes_rine.tools.unreadable import _handle_retry_unreadable

from .conftest import (
    AGENT_ID,
    API_URL,
    MESSAGE_ID,
    SELF_HANDLE,
    hpke_self_encrypt,
    message_payload,
    mock_agents,
    mock_recipient_keys,
    mock_token,
    mock_whoami,
)

OTHER_MESSAGE_ID = "cccccccc-dddd-eeee-ffff-000000000009"


@pytest.fixture(autouse=True)
def _route_sdk_to_config(config_dir, monkeypatch):
    """Point the SDK resolution chain at the test config dir + mock API URL."""
    monkeypatch.setenv("RINE_CONFIG_DIR", config_dir)
    monkeypatch.setenv("RINE_API_URL", API_URL)


@pytest.fixture
def seed():
    """Write rows into the very store the handler will open (the resolved config dir)."""
    from hermes_rine._journal import default_journal_path

    store = SetAsideStore(default_journal_path())

    def _seed(message_id: str, reason: SetAsideReason, *, attempts: int = 1) -> None:
        for _ in range(attempts):
            store.record(
                SELF_HANDLE,
                message_id,
                reason,
                exhausted=reason is SetAsideReason.AUTHENTICITY,
            )

    yield _seed
    store.close()


def _mock_identity(config_dir: str) -> None:
    mock_token()
    mock_agents()
    mock_whoami()
    mock_recipient_keys(AGENT_ID)


def _readable(config_dir: str, text: str, msg_id: str = MESSAGE_ID):
    return httpx.Response(
        200,
        json=message_payload(
            msg_id=msg_id,
            encryption_version=EncryptionVersion.HPKE_HYBRID_V2,
            encrypted_payload=hpke_self_encrypt(config_dir, {"text": text}),
        ),
    )


def _unreadable(msg_id: str = MESSAGE_ID):
    # An mls-v1 row naming no group has no MLS state to open it with, so the SDK reports
    # ``decrypt_error`` rather than raising — the shape a parked message really has.
    return httpx.Response(
        200,
        json=message_payload(
            msg_id=msg_id,
            encryption_version=EncryptionVersion.MLS_V1,
            encrypted_payload="bm90LW1scw==",
        ),
    )


@respx.mock
async def test_an_empty_list_says_so_and_reads_nothing(config_dir):
    _mock_identity(config_dir)
    route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}")

    data = json.loads(await _handle_retry_unreadable({}))

    assert data["set_aside"] == 0
    assert data["retried"] == 0
    assert data["messages"] == "Nothing set aside."
    assert route.call_count == 0


@respx.mock
async def test_a_message_that_now_opens_is_returned_and_dropped_from_the_list(config_dir, seed):
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
        return_value=_readable(config_dir, "the key finally arrived")
    )

    data = json.loads(await _handle_retry_unreadable({}))

    assert data["retried"] == 1
    assert data["opened"] == 1
    assert "the key finally arrived" in data["messages"]
    assert route.call_count == 1
    store = SetAsideStore()
    try:
        assert store.get(SELF_HANDLE, MESSAGE_ID) is None
    finally:
        store.close()


@respx.mock
async def test_the_retry_peeks_so_it_does_not_consume_the_message(config_dir, seed):
    # 🔴 Mutation guard for ``peek=True``. Without the header the server marks the row
    # delivered, so asking "where did my message go?" is what makes it go — and every
    # other assertion in this file would still pass.
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
        return_value=_readable(config_dir, "peeked")
    )

    await _handle_retry_unreadable({})

    assert route.calls.last.request.headers[PEEK_HEADER] == "1"


@respx.mock
async def test_a_still_unreadable_message_reports_its_attempt_count(config_dir, seed):
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(return_value=_unreadable())

    data = json.loads(await _handle_retry_unreadable({}))

    assert data["retried"] == 1
    assert data["opened"] == 0
    assert "attempts=1" in data["messages"]
    assert "still unreadable" in data["messages"]


@respx.mock
async def test_an_on_demand_retry_does_not_spend_the_automatic_retry_budget(config_dir, seed):
    # 🔴 Mutation guard for ``store.touch`` (NOT ``store.record``) in ``_retry_row``. This
    # verb is model-facing and gated by no allowlist. Under ``record`` a model calling it
    # ``SETASIDE_MAX_ATTEMPTS`` times — a plausible "let me try that again" — exhausts
    # every live row, ``due()`` then returns nothing, and the tick never retries anything
    # again: the documented remedy ("fix the cause, the sweep will get it") is dead with
    # one INFO line as its trace. Asserting the *rendered* attempts count alone cannot see
    # this; the row's ``exhausted`` flag is what the sweep reads.
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(return_value=_unreadable())

    for _ in range(SETASIDE_MAX_ATTEMPTS + 2):
        await _handle_retry_unreadable({})

    store = SetAsideStore()
    try:
        row = store.get(SELF_HANDLE, MESSAGE_ID)
        assert row.attempts == 1, "an operator's look must not count as a retry attempt"
        assert row.exhausted is False
        assert row.last_tried is not None, "it still records that the look happened"
        assert [r.message_id for r in store.due(SELF_HANDLE)] == [MESSAGE_ID]
    finally:
        store.close()


@respx.mock
async def test_an_authenticity_refusal_is_listed_but_never_reread(config_dir, seed):
    # 🔴 The path the SPEC assigns a refused sender: visible, so the operator is not left
    # guessing, and never re-read, because those bytes cannot start to verify.
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.AUTHENTICITY)
    route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}")

    data = json.loads(await _handle_retry_unreadable({}))

    assert data["set_aside"] == 1
    assert data["retried"] == 0
    assert "refused for authenticity" in data["messages"]
    assert route.call_count == 0


@respx.mock
async def test_a_message_that_gave_up_says_so_rather_than_reporting_nothing(config_dir, seed):
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE, attempts=SETASIDE_MAX_ATTEMPTS)
    route = respx.get(f"{API_URL}/messages/{MESSAGE_ID}")

    data = json.loads(await _handle_retry_unreadable({}))

    assert data["retried"] == 0
    assert f"gave up after {SETASIDE_MAX_ATTEMPTS} attempts" in data["messages"]
    assert route.call_count == 0


@respx.mock
async def test_a_message_the_server_no_longer_has_is_dropped(config_dir, seed):
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )

    data = json.loads(await _handle_retry_unreadable({}))

    assert "gone from the server" in data["messages"]
    store = SetAsideStore()
    try:
        assert store.get(SELF_HANDLE, MESSAGE_ID) is None
    finally:
        store.close()


@respx.mock
async def test_one_bad_row_does_not_hide_the_rest(config_dir, seed):
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    seed(OTHER_MESSAGE_ID, SetAsideReason.UNREADABLE)
    respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    respx.get(f"{API_URL}/messages/{OTHER_MESSAGE_ID}").mock(
        return_value=_readable(config_dir, "second one through", msg_id=OTHER_MESSAGE_ID)
    )

    data = json.loads(await _handle_retry_unreadable({}))

    assert data["retried"] == 2
    assert data["opened"] == 1
    assert "retry failed" in data["messages"]
    assert "second one through" in data["messages"]


@respx.mock
async def test_the_result_never_carries_ciphertext(config_dir, seed):
    _mock_identity(config_dir)
    seed(MESSAGE_ID, SetAsideReason.UNREADABLE)
    ciphertext = hpke_self_encrypt(config_dir, {"text": "plain"})
    respx.get(f"{API_URL}/messages/{MESSAGE_ID}").mock(
        return_value=httpx.Response(
            200,
            json=message_payload(
                encryption_version=EncryptionVersion.HPKE_HYBRID_V2,
                encrypted_payload=ciphertext,
            ),
        )
    )

    out = await _handle_retry_unreadable({})

    assert "plain" in out
    assert ciphertext not in out
    assert "encrypted_payload" not in out
