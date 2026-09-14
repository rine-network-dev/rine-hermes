"""Shared fixtures for the hermes-rine behavioral suite.

Two layers are exercised here, both mocking **only** the rine HTTP boundary
(respx) and keeping the rine SDK real (a real HPKE self-encrypt round-trip drives
the crypto / ciphertext-never-leaks invariants):

- **Wake layer** (``_poll._dispatch``): touches two seams — the Hermes
  ``MessageEvent``/``MessageType`` (lazily imported from ``gateway.platforms.base``)
  and the rine ``RineClient`` ack. Both are absent out-of-container, so we install
  minimal in-memory doubles for the Hermes seam and a recording fake for the ack.
  The journal is the **real** sqlite ``ConsumedJournal``; messages are **real** SDK
  ``DecryptedMessage`` models.
- **Tool / register layer** (``tools.*``, ``register(ctx)``): the handler modules do
  ``from tools.registry import tool_error, tool_result`` at import time. In the rig
  that module is real (Hermes on PYTHONPATH); on a bare host we install a faithful
  stub (the real ``tool_result``/``tool_error`` JSON semantics). Tools are driven
  through ``build_client`` with ``config_dir``/``api_url`` overrides, so the SDK
  resolves auth/keys exactly as in production — only the HTTP boundary is mocked.

A ``RecordingCtx`` doubles the Hermes ``PluginContext`` so ``register(ctx)`` tests can
assert exactly which tools/skill/platform/hook were registered, with no network.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

# --- Shared HTTP-boundary identifiers (mirror the rine SDK test fixtures' style) ---

API_URL = "https://rine.network"

AGENT_ID = "00000000-1111-2222-3333-444444444444"
OTHER_AGENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GROUP_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
MESSAGE_ID = "cccccccc-dddd-eeee-ffff-000000000001"
CONVERSATION_ID = "cccccccc-dddd-eeee-ffff-000000000002"
# A GROUP's running thread, which is a different object from the 1:1
# CONVERSATION_ID above: the group one is what GroupRead.conversation_id
# carries and what rine_thread is handed to read a group's backlog.
GROUP_CONVERSATION_ID = "cccccccc-dddd-eeee-ffff-000000000003"
REPLY_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"

ORG_ID = "eeeeeeee-ffff-0000-1111-222222222222"
OTHER_HANDLE = "other@acme.rine.network"
GROUP_HANDLE = "#ops@acme.rine.network"
SELF_HANDLE = "test-agent@myorg.rine.network"

TOKEN_RESPONSE = {"access_token": "test-token", "expires_in": 900}

AGENT_PAYLOAD = {
    "id": AGENT_ID,
    "name": "test-agent",
    "handle": SELF_HANDLE,
    "human_oversight": True,
    "incoming_policy": "accept_all",
    "outgoing_policy": "send_all",
    "created_at": "2024-01-01T00:00:00Z",
}

# A SECOND active agent in the same org — the condition the acting-agent
# parameter exists for, and the one under which the SDK refuses to guess which
# agent a call acts or reads as. Shared, because two suites now need it.
SECOND_AGENT_ID = "11111111-2222-3333-4444-555555555555"
SECOND_AGENT_PAYLOAD = {
    **AGENT_PAYLOAD,
    "id": SECOND_AGENT_ID,
    "name": "second-agent",
    "handle": "second-agent@myorg.rine.network",
}

ORG_PAYLOAD = {
    "id": ORG_ID,
    "name": "My Org",
    "slug": "myorg",
    "trust_tier": 2,
    "created_at": "2024-01-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Hermes-seam stubs: install only when the real Hermes module is unavailable.
# In the Docker rig (Hermes on PYTHONPATH) the real modules win — we never
# shadow them. On a bare host these faithful doubles let the suite import + run.
# ---------------------------------------------------------------------------


def host_session_key_profile(owner: Any, source: Any) -> str | None:
    """``BasePlatformAdapter._session_key_profile``'s ladder, transcribed from the host.

    ONE copy, because both doubles in this file need it and two copies disagreed: the
    off-Hermes ``BasePlatformAdapter`` stub read ``owner._owner_profile`` while
    ``FakeAdapter`` read ``owner.profile``, so ``test_session_key.py`` was proving a
    property of the double. The host (``gateway/platforms/base.py``, def
    ``_session_key_profile``) resolves ``source.profile``, then the adapter's own
    ``_owner_profile``, then the session store's resolver — the third rung is omitted
    here because no double carries a session store. It returns None when none answers,
    which is the single-profile answer and what keeps the key byte-identical to a call
    that passed no profile at all.
    """
    for candidate in (
        getattr(source, "profile", None) if source is not None else None,
        getattr(owner, "_owner_profile", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _install_gateway_stub() -> None:
    """Install minimal ``gateway.*`` doubles so the wake core + adapter import resolve.

    Provides ``gateway.platforms.base`` (``MessageEvent``/``MessageType``/
    ``SendResult``/``BasePlatformAdapter``) and ``gateway.config``
    (``Platform``/``PlatformConfig``) — exactly the names ``_poll`` / ``adapter`` /
    ``platform`` reference. Faithful enough to import + (for ``register()`` tests)
    define ``RineAdapter``; the real Hermes modules win when present (the rig).
    """
    try:  # real Hermes present (the rig) → use it, never shadow.
        import gateway.platforms.base  # type: ignore[import-not-found]  # noqa: F401

        return
    except ImportError:
        pass
    from enum import Enum

    sys.modules.setdefault("gateway", types.ModuleType("gateway"))
    sys.modules.setdefault("gateway.platforms", types.ModuleType("gateway.platforms"))
    base = types.ModuleType("gateway.platforms.base")

    class MessageType(Enum):
        TEXT = "text"

    class ProcessingOutcome(Enum):
        SUCCESS = "success"
        FAILURE = "failure"
        CANCELLED = "cancelled"

    class MessageEvent:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    class SendResult:
        def __init__(
            self,
            *,
            success: bool,
            message_id: str | None = None,
            error: str | None = None,
        ) -> None:
            self.success = success
            self.message_id = message_id
            self.error = error

    class BasePlatformAdapter:
        """Minimal abstract base mirroring the surface the adapter relies on.

        The post-delivery callback store reproduces the pinned host's *generation*
        semantics verbatim (base.py ``register_post_delivery_callback`` /
        ``pop_post_delivery_callback``): a registration without a generation is stored
        bare, one with a generation is stored as a ``(generation, callback)`` tuple, and
        a pop that supplies a generation returns None for a bare entry and for a tuple
        whose generation differs. That asymmetry is what ``RineAdapter`` overrides the
        pop for, so a double that ignored it would hide the very defect under test.
        """

        def __init__(self, config: object, platform: object) -> None:
            self.config = config
            self.platform = platform
            self._running = False
            self._post_delivery_callbacks: dict[str, object] = {}

        def build_source(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        async def handle_message(self, event: object) -> None:  # pragma: no cover
            return None

        def _session_key_profile(self, source: object | None = None) -> str | None:
            """Mirror the host's profile resolver — see ``host_session_key_profile``."""
            return host_session_key_profile(self, source)

        def register_post_delivery_callback(
            self, session_key: str, callback: object, *, generation: int | None = None
        ) -> None:
            if not session_key or not callable(callback):
                return
            self._post_delivery_callbacks[session_key] = (
                callback if generation is None else (int(generation), callback)
            )

        def pop_post_delivery_callback(
            self, session_key: str, *, generation: int | None = None
        ) -> object | None:
            entry = self._post_delivery_callbacks.get(session_key)
            if entry is None:
                return None
            if isinstance(entry, tuple):
                entry_generation, callback = entry
                if generation is not None and int(entry_generation) != int(generation):
                    return None
                self._post_delivery_callbacks.pop(session_key, None)
                return callback if callable(callback) else None
            if generation is not None:
                return None
            self._post_delivery_callbacks.pop(session_key, None)
            return entry

    def build_session_key(
        source: object,
        group_sessions_per_user: bool = True,
        thread_sessions_per_user: bool = False,
        profile: str | None = None,
    ) -> str:
        """A faithful-enough session key: ``<namespace>:rine:<chat_type>:<chat_id>``.

        ``profile`` selects the namespace exactly as the host's
        ``_session_key_namespace`` does — a falsy or ``"default"`` profile is
        ``agent:main`` (byte-identical to a call that passes no profile at all),
        anything else is ``agent:<profile>``. The double carries the parameter
        because a stub that silently ignored it would let a caller that forgot to
        pass a profile read green here and re-dispatch forever in production.
        """
        chat_id = (
            source.get("chat_id") if isinstance(source, dict) else getattr(source, "chat_id", "?")
        )
        chat_type = (
            source.get("chat_type")
            if isinstance(source, dict)
            else getattr(source, "chat_type", "dm")
        )
        namespace = "agent:main" if not profile or profile == "default" else f"agent:{profile}"
        return f"{namespace}:rine:{chat_type}:{chat_id}"

    base.MessageType = MessageType  # type: ignore[attr-defined]
    base.ProcessingOutcome = ProcessingOutcome  # type: ignore[attr-defined]
    base.MessageEvent = MessageEvent  # type: ignore[attr-defined]
    base.SendResult = SendResult  # type: ignore[attr-defined]
    base.BasePlatformAdapter = BasePlatformAdapter  # type: ignore[attr-defined]
    base.build_session_key = build_session_key  # type: ignore[attr-defined]
    sys.modules["gateway.platforms.base"] = base

    config = types.ModuleType("gateway.config")

    class Platform(Enum):
        RINE = "rine"

        @classmethod
        def _missing_(cls, value: object) -> Platform | None:
            # Accept any plugin platform string as a cached pseudo-member.
            if not isinstance(value, str) or not value.strip():
                return None
            key = value.strip().lower()
            if key in cls._value2member_map_:
                return cls._value2member_map_[key]  # type: ignore[return-value]
            pseudo = object.__new__(cls)
            pseudo._value_ = key
            pseudo._name_ = key.upper()
            cls._value2member_map_[key] = pseudo
            return pseudo

    class PlatformConfig:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    config.Platform = Platform  # type: ignore[attr-defined]
    config.PlatformConfig = PlatformConfig  # type: ignore[attr-defined]
    sys.modules["gateway.config"] = config


def _install_registry_stub() -> None:
    """Install a faithful ``tools.registry`` stub when the real one is absent.

    The handler/tool modules import ``tool_error``/``tool_result`` at module top.
    The stub mirrors the real JSON semantics (``tools.registry.tool_error`` /
    ``tools.registry.tool_result``) so tests can assert on the returned string
    identically with or without Hermes installed.
    """
    try:
        import tools.registry  # type: ignore[import-not-found]  # noqa: F401

        return
    except ImportError:
        pass
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    registry = types.ModuleType("tools.registry")

    def tool_error(message: object, **extra: object) -> str:
        result: dict[str, object] = {"error": str(message)}
        if extra:
            result.update(extra)
        return json.dumps(result, ensure_ascii=False)

    def tool_result(data: object = None, **kwargs: object) -> str:
        if data is not None:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps(kwargs, ensure_ascii=False)

    registry.tool_error = tool_error  # type: ignore[attr-defined]
    registry.tool_result = tool_result  # type: ignore[attr-defined]
    sys.modules["tools.registry"] = registry


_install_gateway_stub()
_install_registry_stub()


# ---------------------------------------------------------------------------
# Config-dir fixture (real SDK auth/keys on disk) + HPKE helpers.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ambient_acting_agent(monkeypatch):
    """Keep the developer's own ``RINE_AGENT`` out of every test in this package.

    The SDK client constructor reads it (rung 3 of the ladder), so a machine
    that exports one would silently give every tool call an actor the test never
    asked for. A test that wants the env rung sets it itself, after this ran.
    """
    monkeypatch.delenv("RINE_AGENT", raising=False)


def registered_tool_id(handler: Any) -> str:
    """The tool id Hermes actually registers ``handler`` under.

    🔴 Derivation, not a retyped literal. A refusal that names a verb is an
    instruction a model follows, so the id in the sentence and the id in
    ``_HANDLERS`` must be the same string — and only deriving one from the other
    catches the id drifting AWAY from the constant. The cross-package pin in
    ``tests/helpers_client_paths.py`` compares the five constants to each other
    and cannot see that drift at all.

    Imported lazily: this module installs the gateway/registry stubs Hermes
    imports through, and they have to be in place first.
    """
    from hermes_rine.tools import _HANDLERS

    for name, (registered, _env) in _HANDLERS.items():
        if registered is handler:
            return name
    raise AssertionError(f"{handler!r} is registered under no tool id")


@pytest.fixture
def config_dir(tmp_path) -> str:
    """A config dir with valid credentials + the caller agent's keys on disk.

    A tool's ``config_dir`` override points here, so the real SDK resolves
    auth/keys as in production — only the HTTP boundary is mocked, never auth.
    """
    from rine._config import save_credentials
    from rine._crypto.keys import generate_agent_keys, save_agent_keys

    cdir = str(tmp_path)
    save_credentials(cdir, {"default": {"client_id": "cid", "client_secret": "csecret"}})
    save_agent_keys(cdir, AGENT_ID, generate_agent_keys())
    return cdir


@pytest.fixture
def other_keys(config_dir):
    """Generate + persist the recipient agent's keys so a self-mocked key fetch works."""
    from rine._crypto.keys import generate_agent_keys, save_agent_keys

    keys = generate_agent_keys()
    save_agent_keys(config_dir, OTHER_AGENT_ID, keys)
    return keys


def self_keys_json(config_dir: str, agent_id: str = AGENT_ID) -> dict[str, Any]:
    """The ``/agents/{id}/keys`` JSON body for an agent whose keys are on disk."""
    from rine._crypto.keys import (
        encryption_public_key_to_jwk,
        load_agent_keys,
        signing_public_key_to_jwk,
    )

    keys = load_agent_keys(config_dir, agent_id)
    return {
        "agent_id": agent_id,
        "encryption_public_key": encryption_public_key_to_jwk(keys.encryption.public_key),
        "signing_public_key": signing_public_key_to_jwk(keys.signing.public_key),
    }


def hpke_self_encrypt(config_dir: str, payload: dict[str, Any]) -> str:
    """HPKE-encrypt ``payload`` from AGENT_ID to AGENT_ID (self-readable ciphertext)."""
    from rine._crypto.dm_envelope import RecipientKeys
    from rine._crypto.keys import load_agent_keys
    from rine._crypto.message import encrypt_message

    # Classical on purpose: the recipient's ML-KEM half is left out so this helper
    # keeps producing `hpke-v1`, which is what the fixtures around it declare.
    own = load_agent_keys(config_dir, AGENT_ID)
    recipient = RecipientKeys(encryption=own.encryption.public_key)
    return encrypt_message(config_dir, AGENT_ID, recipient, payload).encrypted_payload


def hpke_peer_encrypt(config_dir: str, payload: dict[str, Any]) -> str:
    """HPKE-encrypt ``payload`` from OTHER_AGENT_ID to AGENT_ID, signed by the peer.

    A row the server reports as coming from another agent is signed by that agent's
    own key, so a fixture standing in for a received turn is sealed with the peer's
    keys — sealing it with the caller's own key would attribute a signature to an
    agent that did not make it. Needs the ``other_keys`` fixture (peer keys on disk)
    and ``mock_recipient_keys`` (the peer's published signing key).
    """
    from rine._crypto.dm_envelope import RecipientKeys
    from rine._crypto.keys import load_agent_keys
    from rine._crypto.message import encrypt_message

    # Classical on purpose, matching ``hpke_self_encrypt``: no ML-KEM half, so the
    # ciphertext stays `hpke-v1` as the fixtures around it declare.
    own = load_agent_keys(config_dir, AGENT_ID)
    recipient = RecipientKeys(encryption=own.encryption.public_key)
    return encrypt_message(config_dir, OTHER_AGENT_ID, recipient, payload).encrypted_payload


# ---------------------------------------------------------------------------
# Reusable respx route installers (called inside @respx.mock test bodies).
# ---------------------------------------------------------------------------


def mock_token() -> None:
    """Stub the OAuth2 token endpoint."""
    import respx

    respx.post(f"{API_URL}/oauth/token").mock(
        return_value=httpx.Response(200, json=TOKEN_RESPONSE)
    )


def mock_agents() -> None:
    """Stub ``GET /agents`` (single-agent org → auto agent resolution)."""
    import respx

    respx.get(f"{API_URL}/agents").mock(
        return_value=httpx.Response(200, json={"items": [AGENT_PAYLOAD]})
    )


def mock_group_row(group_id: str = GROUP_ID, **payload: Any) -> None:
    """Stub ``GET /groups/{id}`` — the one indexed read a group reference costs.

    A reference is resolved before it becomes a URL, and a UUID is
    checked against its own row rather than against a listing. Every verb that
    takes a group therefore pays this read once, whatever spelling it was given.
    """
    import respx

    respx.get(f"{API_URL}/groups/{group_id}").mock(
        return_value=httpx.Response(200, json=group_payload(group_id=group_id, **payload))
    )


def mock_two_agents() -> None:
    """Stub ``GET /agents`` with a two-agent org (auto-resolution is refused)."""
    import respx

    respx.get(f"{API_URL}/agents").mock(
        return_value=httpx.Response(200, json={"items": [AGENT_PAYLOAD, SECOND_AGENT_PAYLOAD]})
    )


def mock_org() -> None:
    """Stub ``GET /org`` (used by ``whoami``)."""
    import respx

    respx.get(f"{API_URL}/org").mock(return_value=httpx.Response(200, json=ORG_PAYLOAD))


def mock_webfinger(handle_uuid: str = OTHER_AGENT_ID) -> None:
    """Stub WebFinger so a handle resolves to ``handle_uuid``."""
    import respx

    respx.get(f"{API_URL}/.well-known/webfinger").mock(
        return_value=httpx.Response(200, json={"aliases": [f"{API_URL}/agents/{handle_uuid}"]})
    )


def mock_recipient_keys(config_dir: str, agent_id: str = OTHER_AGENT_ID) -> None:
    """Stub ``GET /agents/{id}/keys`` with the persisted recipient public key."""
    import respx

    respx.get(f"{API_URL}/agents/{agent_id}/keys").mock(
        return_value=httpx.Response(200, json=self_keys_json(config_dir, agent_id))
    )


def mock_self_keys(config_dir: str) -> None:
    """Stub ``GET /agents/{AGENT_ID}/keys`` so self-encrypted mail decrypts + verifies."""
    import respx

    respx.get(f"{API_URL}/agents/{AGENT_ID}/keys").mock(
        return_value=httpx.Response(200, json=self_keys_json(config_dir))
    )


def mock_group_mls_init(group_id: str = GROUP_ID) -> Any:
    """Stub the MLS-founding calls a group create fires after ``POST /groups``.

    ``client.groups.create`` now founds post-quantum MLS state: it opens the init
    window (``mls/init``) and publishes the founder's init commit (``mls/commit``).
    The founder is the only member here, so no KeyPackage claims occur.

    It then **re-reads the group**: the server latches ``mls_group_id`` after the
    create response was minted, so without this ``GET`` a brand-new MLS group
    renders as sender-key. The re-read is guarded in the SDK, so leaving it
    unmocked would not fail a test — it would silently return the unlatched
    create response, which is how a create test passes for the wrong reason.

    Returns the ``mls/init`` route so a caller can assert the founder opened the
    init window.
    """
    import respx

    init = respx.post(f"{API_URL}/groups/{group_id}/mls/init").mock(
        return_value=httpx.Response(200, json={"members": []})
    )
    respx.post(f"{API_URL}/groups/{group_id}/mls/commit").mock(
        return_value=httpx.Response(200, json={"epoch": 1})
    )
    respx.get(f"{API_URL}/groups/{group_id}").mock(
        return_value=httpx.Response(
            200, json=group_payload(group_id=group_id, mls_enabled=True, mls_group_id="mls-xyz")
        )
    )
    return init


# ---------------------------------------------------------------------------
# Server-message + group payload builders.
# ---------------------------------------------------------------------------


def message_payload(
    *,
    encryption_version: str,
    encrypted_payload: str,
    msg_id: str = MESSAGE_ID,
    from_agent_id: str = AGENT_ID,
    to_agent_id: str = AGENT_ID,
    msg_type: str = "rine.v1.task_request",
    group_id: str | None = None,
    group_handle: str | None = None,
    direction: str | None = None,
    self_encrypted_payload: str | None = None,
) -> dict[str, Any]:
    """Build a server message payload dict (what ``/messages/{id}`` returns).

    ``direction``/``self_encrypted_payload`` mirror the server's sender view: only
    when the caller IS the sender does the row come back ``direction="sent"`` with
    the sender's self-seal, which is the only copy of its own send it can reopen.
    """
    payload: dict[str, Any] = {
        "id": msg_id,
        "conversation_id": CONVERSATION_ID,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "type": msg_type,
        "encrypted_payload": encrypted_payload,
        "encryption_version": encryption_version,
        "sender_signing_kid": f"rine:{from_agent_id}",
        "created_at": "2024-01-01T00:00:00Z",
    }
    if group_id is not None:
        payload["group_id"] = group_id
    if group_handle is not None:
        payload["group_handle"] = group_handle
    if direction is not None:
        payload["direction"] = direction
    if self_encrypted_payload is not None:
        payload["self_encrypted_payload"] = self_encrypted_payload
    return payload


def sent_message_payload(
    *, to_agent_id: str | None = OTHER_AGENT_ID, encrypted_payload: str = "ZHVtbXk="
) -> dict[str, Any]:
    """A ``MessageRead`` payload for a just-sent message (ciphertext opaque)."""
    return {
        "id": MESSAGE_ID,
        "conversation_id": CONVERSATION_ID,
        "from_agent_id": AGENT_ID,
        "to_agent_id": to_agent_id,
        "type": "rine.v1.task_request",
        "encrypted_payload": encrypted_payload,
        "encryption_version": "hpke-v1",
        "sender_signing_kid": f"rine:{AGENT_ID}",
        "created_at": "2024-01-01T00:00:00Z",
    }


def member_payload(
    agent_id: str, handle: str, *, role: str = "member", is_own_org: bool = True
) -> dict[str, Any]:
    """A ``GroupMember`` payload (server ``/groups/{id}/members`` item shape).

    ``is_own_org`` defaults to ``True`` because that is what most of these rows
    are: the roster of a group this agent's org is in. A test that wants another
    org's seat says so, which is the only way that row is ever interesting.

    It lives here rather than beside the roster tests because the roster is no
    longer the only reader of it: ``rine_group_remove`` resolves the handle it
    was given against the same rows, so both suites have to agree on the shape.
    """
    return {
        "id": "dddddddd-0000-1111-2222-333333333333",
        "group_id": GROUP_ID,
        "agent_id": agent_id,
        "role": role,
        "joined_at": "2024-03-04T05:06:07Z",
        "agent_handle": handle,
        "is_own_org": is_own_org,
    }


def group_payload(
    *,
    name: str = "ops",
    handle: str = GROUP_HANDLE,
    group_id: str = GROUP_ID,
    member_count: int = 2,
    enrollment_policy: str = "closed",
    mls_enabled: bool = False,
    mls_group_id: str | None = None,
    mls_pending: bool = False,
    description: str | None = None,
    conversation_id: str | None = None,
    member_agent_ids: list[str] | None = None,
) -> dict[str, Any]:
    """A ``GroupRead`` payload (server ``/groups`` item shape).

    ``conversation_id`` defaults to ``None`` and the key is always emitted,
    which is the server's own shape: it is nullable because a group's
    conversation is created lazily on its first post, and a group nobody has
    posted in therefore has none.

    ``member_agent_ids`` defaults to **empty**, and the key is likewise always
    emitted. Empty is the server's honest answer for a group the org is in but
    no agent of it is seated in, and defaulting it that way keeps every test
    that does not care about seats from quietly asserting one.

    ``enrollment_policy`` is an argument because the default pair here —
    ``closed`` with ``mls_enabled=False`` — is a row no shipped client creates:
    every create sends ``mls_enabled=True`` and the server column defaults true.
    A group that really runs sender keys is ``open``, and without this the
    sender-key arm of every verb was only ever driven on the unreachable row.
    """
    return {
        "id": group_id,
        "name": name,
        "handle": handle,
        "description": description,
        "enrollment_policy": enrollment_policy,
        "visibility": "private",
        "member_count": member_count,
        "created_at": "2024-01-01T00:00:00Z",
        "mls_enabled": mls_enabled,
        "mls_group_id": mls_group_id,
        "mls_pending": mls_pending,
        "conversation_id": conversation_id,
        "member_agent_ids": member_agent_ids or [],
    }


# ---------------------------------------------------------------------------
# Wake-layer doubles (the Hermes handle_message seam + rine ack recorder).
# ---------------------------------------------------------------------------


class RecordingClient:
    """A fake ``RineClient`` recording the ids passed to ``mark_delivered`` and ``read``.

    ``fail_ack`` makes the first ``mark_delivered`` raise (to test that a failed ack
    neither journals nor loses a message). ``read`` exists because the set-aside sweep
    re-reads a parked id through the adapter's own client (the tools build their own),
    and it records the ``peek`` flag it was called with: whether the retry consumes the
    message it is recovering is the whole point of that keyword, so a test asserts on it
    rather than trusting it. ``reads`` is keyed by id so a test states the answer per
    message; ``read_calls`` is the call log.
    """

    def __init__(self, *, fail_ack: bool = False) -> None:
        self.acked: list[str] = []
        self.fail_ack = fail_ack
        self.reads: dict[str, object] = {}
        self.read_calls: list[tuple[str, bool]] = []

    async def mark_delivered(self, ids: list[str]) -> int:
        if self.fail_ack:
            self.fail_ack = False
            raise RuntimeError("simulated ack failure")
        self.acked.extend(ids)
        return len(ids)

    async def read(
        self, message_id: str, *, peek: bool = False, agent: str | None = None
    ) -> object:
        """Answer a sweep re-read from ``reads``; an absent id is a server 404."""
        self.read_calls.append((str(message_id), peek))
        answer = self.reads.get(str(message_id))
        if answer is None:
            from rine import NotFoundError

            raise NotFoundError("message not found")
        if isinstance(answer, Exception):
            raise answer
        return answer


class _FakeConfig:
    """Minimal ``PlatformConfig`` stand-in carrying ``extra`` (for session-key toggles)."""

    def __init__(self) -> None:
        self.extra: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _rine_platform_registered() -> None:
    """Register the rine platform so ``Platform("rine")`` resolves during tests.

    The real ``Platform`` enum's ``_missing_`` mints a pseudo-member only for a platform
    present in ``platform_registry`` (``gateway.config.Platform._missing_``). In production
    ``register_platform`` runs at plugin load before the adapter is instantiated; the tests
    replicate that minimally. No-op off-Hermes (the guarded double accepts any string).
    """
    try:
        from gateway.platform_registry import (  # type: ignore[import-not-found]
            PlatformEntry,
            platform_registry,
        )
    except ImportError:
        return
    if not platform_registry.is_registered("rine"):
        platform_registry.register(
            PlatformEntry(
                name="rine",
                label="rine",
                adapter_factory=lambda _cfg: None,
                check_fn=lambda: True,
            )
        )


class FakeAdapter:
    """A double mirroring the *asynchronous* Hermes wake contract.

    Like the real ``BasePlatformAdapter.handle_message``, ``handle_message`` only
    *schedules* the turn: it records the event and returns — it does NOT complete the
    turn, and it never fires the post-delivery callback itself. The test harness drives
    completion via ``fire_post_delivery()`` (a successful turn) or leaves it unfired (a
    crash / mid-turn). This is what makes the exactly-once assertions test reality: in
    production journal + ack happen only inside that post-delivery callback, never when
    ``handle_message`` returns.
    """

    def __init__(
        self,
        journal: object,
        *,
        agent_handle: str = "me",
        setaside_path: str,
        **opts: object,
    ) -> None:
        self.client = RecordingClient(fail_ack=bool(opts.get("fail_ack", False)))
        self.journal = journal
        # A REAL ``SetAsideStore`` on the same per-test sqlite file as the journal —
        # the production shape (one file, two tables), and the table's own semantics
        # (bounds, exhaustion, eviction) are what several dispatch assertions read.
        from hermes_rine._setaside import SetAsideStore

        self.setaside = SetAsideStore(setaside_path)
        self.agent_handle = agent_handle
        self.allowed_handles = opts.get("allowed_handles")
        self.require_verified = bool(opts.get("require_verified", False))
        self.poll_interval = 0
        self.running = True
        self.config = _FakeConfig()
        self.events: list[object] = []
        # session_key -> registered (possibly chained) post-delivery callback.
        self._post_delivery_callbacks: dict[str, object] = {}
        self._raise_on_handle = bool(opts.get("raise_on_handle", False))
        # The REAL in-flight turn registry — it is plain state with no Hermes seam in it,
        # so a double would only be a second implementation to keep in step.
        from hermes_rine._gateway_control import parse_control_handles
        from hermes_rine._turns import InboundTurns

        self.turns = InboundTurns()
        # Gateway control, as ``RineAdapter.__init__`` reads it — but from opts instead of
        # env, so a test states the posture it means rather than mutating the process.
        # The parse and the predicate are the REAL ones: they are pure and env-free, so a
        # double here would only be a second implementation of the rule under test.
        self._gateway_control_on = bool(opts.get("gateway_control", False))
        raw_control = opts.get("gateway_control_handles", "")
        self._gateway_control_handles = parse_control_handles(
            raw_control if isinstance(raw_control, str) else ""
        )

    def allows_gateway_control(self, sender: str) -> bool:
        """Mirror ``RineAdapter.allows_gateway_control`` through the real predicate."""
        from hermes_rine._gateway_control import grants_control

        return grants_control(
            enabled=self._gateway_control_on,
            handles=self._gateway_control_handles,
            sender=sender,
        )

    def _session_key_profile(self, source: object | None = None) -> str | None:
        """The profile namespace ``_session_key_for`` keys the callback under.

        Inherited from ``BasePlatformAdapter`` in production; delegated here to the one
        transcription of the host's ladder (``host_session_key_profile``) because this
        double does not subclass it. It answers None unless a test sets
        ``adapter._owner_profile`` (or stamps ``source.profile``) — None being the
        single-profile answer that produces today's ``agent:main`` key.
        """
        return host_session_key_profile(self, source)

    def build_source(self, **kwargs: object) -> object:
        """Return a real ``SessionSource`` under real Hermes (the rig), else a dict double.

        The real ``build_session_key`` reads ``source.platform.value`` (gateway/session.py),
        so a dict / string-platform would crash there. Mirror the real ``build_source``:
        ``platform=Platform("rine")`` (the pseudo-member ``_missing_`` mints). Off-Hermes the
        guarded ``build_session_key`` double accepts the dict.
        """
        try:
            from gateway.config import Platform  # type: ignore[import-not-found]
            from gateway.platforms.base import SessionSource  # type: ignore[import-not-found]
        except ImportError:
            return dict(kwargs)
        uid = kwargs.get("user_id")
        return SessionSource(
            platform=Platform("rine"),
            chat_id=str(kwargs.get("chat_id", "")),
            chat_name=kwargs.get("chat_name"),  # type: ignore[arg-type]
            chat_type=str(kwargs.get("chat_type", "dm")),
            user_id=str(uid) if uid else None,
            user_name=kwargs.get("user_name"),  # type: ignore[arg-type]
        )

    def register_post_delivery_callback(
        self, session_key: str, callback: object, *, generation: int | None = None
    ) -> None:
        """Chain callbacks for the same key (mirrors base.py one-shot chaining semantics).

        ``generation`` is accepted and ignored: what the gateway's generation gate does to
        a bare registration is a property of ``RineAdapter.pop_post_delivery_callback``,
        exercised against the real adapter and the faithful base double, not here.
        """
        if not session_key or not callable(callback):
            return
        existing = self._post_delivery_callbacks.get(session_key)
        if existing is not None and callable(existing):
            prev, new = existing, callback

            async def _chained() -> None:
                for cb in (prev, new):
                    result = cb()
                    if hasattr(result, "__await__"):
                        await result

            callback = _chained
        self._post_delivery_callbacks[session_key] = callback

    def pop_post_delivery_callback(
        self, session_key: str, *, generation: int | None = None
    ) -> object | None:
        """One-shot pop of a registered callback for ``session_key`` (or None)."""
        return self._post_delivery_callbacks.pop(session_key, None)

    @property
    def registered_callbacks(self) -> dict[str, object]:
        """Read-only view so a test can assert a callback was (or wasn't) registered."""
        return self._post_delivery_callbacks

    async def fire_post_delivery(self, session_key: str | None = None) -> None:
        """Simulate a SUCCESSFUL turn completing: pop + invoke the post-delivery callback.

        With ``session_key`` None, fires every pending callback (the common single-message
        case). Mirrors Hermes firing the popped callback after ``_message_handler`` returns.
        """
        keys = [session_key] if session_key is not None else list(self._post_delivery_callbacks)
        for key in keys:
            cb = self._post_delivery_callbacks.pop(key, None)
            if cb is None:
                continue
            result = cb()
            if hasattr(result, "__await__"):
                await result

    async def handle_message(self, event: object) -> None:
        if self._raise_on_handle:
            raise RuntimeError("simulated handle_message failure")
        self.events.append(event)  # schedule only — the turn is NOT complete yet


@pytest.fixture
def make_adapter(tmp_path):
    """Factory for a ``FakeAdapter`` backed by a real per-test sqlite journal."""
    from hermes_rine._journal import ConsumedJournal

    def _make(**opts: object) -> FakeAdapter:
        journal = ConsumedJournal(str(tmp_path / "journal.sqlite"))
        agent_handle = str(opts.pop("agent_handle", "me"))
        return FakeAdapter(
            journal,
            agent_handle=agent_handle,
            setaside_path=str(tmp_path / "journal.sqlite"),
            **opts,
        )

    return _make


@pytest.fixture
def make_message():
    """Factory for real ``DecryptedMessage`` fixtures with sane defaults."""
    from rine import DecryptedMessage

    def _make(
        *,
        message_id: UUID | None = None,
        sender_handle: str | None = "alice",
        plaintext: str | None = '{"text":"hello"}',
        verification_status: str = "verified",
        verified: bool = True,
        decrypt_error: str | None = None,
        decrypt_error_code: str | None = None,
        group_handle: str | None = None,
        msg_type: str = "rine.v1.task_request",
        metadata: dict[str, Any] | None = None,
    ) -> DecryptedMessage:
        return DecryptedMessage(
            id=message_id or uuid4(),
            conversation_id=uuid4(),
            type=msg_type,
            metadata=metadata or {},
            encrypted_payload="OPAQUE-CIPHERTEXT-NEVER-SURFACED",
            created_at=datetime.now(UTC),
            sender_handle=sender_handle,
            group_handle=group_handle,
            plaintext=plaintext,
            verified=verified,
            verification_status=verification_status,  # type: ignore[arg-type]
            decrypt_error=decrypt_error,
            decrypt_error_code=decrypt_error_code,
        )

    return _make


# ---------------------------------------------------------------------------
# Fake Hermes PluginContext recorder (for register() tests).
# ---------------------------------------------------------------------------


class RecordingCtx:
    """Records every ``register_*`` call ``register(ctx)`` makes, with no network.

    Mirrors the public ``PluginContext`` registration surface
    (``register_tool``/``register_skill``/``register_platform``/``register_hook``);
    each call is captured as a kwargs dict so a test can count + introspect them.
    """

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []
        self.skills: list[dict[str, Any]] = []
        self.platforms: list[dict[str, Any]] = []
        self.hooks: list[dict[str, Any]] = []

    def register_tool(self, **kwargs: Any) -> None:
        self.tools.append(kwargs)

    def register_skill(self, name: str, path: Any, description: str = "") -> None:
        self.skills.append({"name": name, "path": path, "description": description})

    def register_platform(self, **kwargs: Any) -> None:
        self.platforms.append(kwargs)

    def register_hook(self, hook_name: str, callback: Any) -> None:
        self.hooks.append({"hook_name": hook_name, "callback": callback})


@pytest.fixture
def fake_ctx() -> RecordingCtx:
    """A fresh ``RecordingCtx`` for register() assertions."""
    return RecordingCtx()
