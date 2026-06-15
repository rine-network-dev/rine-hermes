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
        """Minimal abstract base mirroring the surface the adapter relies on."""

        def __init__(self, config: object, platform: object) -> None:
            self.config = config
            self.platform = platform
            self._running = False

        def build_source(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        async def handle_message(self, event: object) -> None:  # pragma: no cover
            return None

    def build_session_key(
        source: object,
        group_sessions_per_user: bool = True,
        thread_sessions_per_user: bool = False,
    ) -> str:
        """A faithful-enough session key: ``<chat_type>:<chat_id>`` (per-conversation)."""
        chat_id = (
            source.get("chat_id") if isinstance(source, dict) else getattr(source, "chat_id", "?")
        )
        chat_type = (
            source.get("chat_type")
            if isinstance(source, dict)
            else getattr(source, "chat_type", "dm")
        )
        return f"agent:main:rine:{chat_type}:{chat_id}"

    base.MessageType = MessageType  # type: ignore[attr-defined]
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
    The stub mirrors the real JSON semantics (``registry.py:563,577``) so tests can
    assert on the returned string identically with or without Hermes installed.
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
    from rine._crypto.keys import load_agent_keys
    from rine._crypto.message import encrypt_message

    own_pk = load_agent_keys(config_dir, AGENT_ID).encryption.public_key
    return encrypt_message(config_dir, AGENT_ID, own_pk, payload).encrypted_payload


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
) -> dict[str, Any]:
    """Build a server message payload dict (what ``/messages/{id}`` returns)."""
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


def group_payload(
    *,
    name: str = "ops",
    handle: str = GROUP_HANDLE,
    group_id: str = GROUP_ID,
    member_count: int = 2,
    mls_enabled: bool = False,
    mls_group_id: str | None = None,
    mls_pending: bool = False,
    description: str | None = None,
) -> dict[str, Any]:
    """A ``GroupRead`` payload (server ``/groups`` item shape)."""
    return {
        "id": group_id,
        "name": name,
        "handle": handle,
        "description": description,
        "enrollment_policy": "closed",
        "visibility": "private",
        "member_count": member_count,
        "created_at": "2024-01-01T00:00:00Z",
        "mls_enabled": mls_enabled,
        "mls_group_id": mls_group_id,
        "mls_pending": mls_pending,
    }


# ---------------------------------------------------------------------------
# Wake-layer doubles (the Hermes handle_message seam + rine ack recorder).
# ---------------------------------------------------------------------------


class RecordingClient:
    """A fake ``RineClient`` recording the ids passed to ``mark_delivered``.

    ``fail_ack`` makes the first ``mark_delivered`` raise (to test that a failed ack
    neither journals nor loses a message). Only ``mark_delivered`` is exercised by
    the dispatch path; reads/sends go through the tools, not here.
    """

    def __init__(self, *, fail_ack: bool = False) -> None:
        self.acked: list[str] = []
        self.fail_ack = fail_ack

    async def mark_delivered(self, ids: list[str]) -> int:
        if self.fail_ack:
            self.fail_ack = False
            raise RuntimeError("simulated ack failure")
        self.acked.extend(ids)
        return len(ids)


class _FakeConfig:
    """Minimal ``PlatformConfig`` stand-in carrying ``extra`` (for session-key toggles)."""

    def __init__(self) -> None:
        self.extra: dict[str, object] = {}


@pytest.fixture(autouse=True)
def _rine_platform_registered() -> None:
    """Register the rine platform so ``Platform("rine")`` resolves during tests.

    The real ``Platform`` enum's ``_missing_`` mints a pseudo-member only for a platform
    present in ``platform_registry`` (gateway/config.py:200). In production
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

    Like the real ``BasePlatformAdapter`` (base.py:3839), ``handle_message`` only
    *schedules* the turn: it records the event and returns — it does NOT complete the
    turn, and it never fires the post-delivery callback itself. The test harness drives
    completion via ``fire_post_delivery()`` (a successful turn) or leaves it unfired (a
    crash / mid-turn). This is what makes the exactly-once assertions test reality: in
    production journal + ack happen only inside that post-delivery callback, never when
    ``handle_message`` returns.
    """

    def __init__(self, journal: object, *, agent_handle: str = "me", **opts: object) -> None:
        self.client = RecordingClient(fail_ack=bool(opts.get("fail_ack", False)))
        self.journal = journal
        self.agent_handle = agent_handle
        self.allowed_handles = opts.get("allowed_handles")
        self.require_verified = bool(opts.get("require_verified", False))
        self.poll_interval = 0
        self.running = True
        self.config = _FakeConfig()
        self.inflight: set[str] = set()
        self.events: list[object] = []
        # session_key -> registered (possibly chained) post-delivery callback.
        self._post_delivery_callbacks: dict[str, object] = {}
        self._raise_on_handle = bool(opts.get("raise_on_handle", False))

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

    def register_post_delivery_callback(self, session_key: str, callback: object) -> None:
        """Chain callbacks for the same key (mirrors base.py one-shot chaining semantics)."""
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

    def pop_post_delivery_callback(self, session_key: str) -> object | None:
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
        return FakeAdapter(journal, agent_handle=agent_handle, **opts)

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
