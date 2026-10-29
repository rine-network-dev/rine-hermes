"""One recording client behind both the rine tools and a bare adapter, shaped like the server.

The server answers a direct send with a message in a conversation of its own, addressed to
the recipient, and a group send with a message in the group's conversation. The fakes here
return exactly that, so a test cannot lean on a conversation id a 1:1 send never yields.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hermes_rine.tools import messaging

from .conftest import SELF_HANDLE
from .test_adapter_send import FINAL, _adapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import pytest


class _Row:
    def __init__(
        self,
        conversation_id: str,
        group_handle: str | None = None,
        recipient_handle: str | None = None,
    ) -> None:
        self.id = uuid4()
        self.conversation_id = conversation_id
        self.group_handle = group_handle
        self.recipient_handle = recipient_handle


class _Result:
    def __init__(self, sent: _Row) -> None:
        self.sent = sent
        self.reply = None


class RecordingClient:
    """Logs every call; ``failing`` makes each raise.

    ``groups`` maps a spelling a tool is given to the group it names and that group's
    conversation; every other target is a peer.
    """

    def __init__(self, groups: dict[str, tuple[str, str]]) -> None:
        self.log: list[str] = []
        self.failing = False
        self._groups = groups

    def _call(self, entry: str, target: str | None = None) -> _Row:
        self.log.append(entry)
        if self.failing:
            raise RuntimeError("network down")
        if target in self._groups:
            handle, conversation = self._groups[target]
            return _Row(conversation, group_handle=handle)
        return _Row(str(uuid4()), recipient_handle=target)

    async def reply(self, message_id: str, payload: Any, **_: Any) -> _Row:
        return self._call(f"reply:{message_id}")

    async def send(self, target: str, payload: Any, **_: Any) -> _Row:
        return self._call(f"send:{target}", target)

    async def send_and_wait(self, target: str, payload: Any, **_: Any) -> _Result:
        return _Result(self._call(f"send:{target}", target))


def rig(monkeypatch: pytest.MonkeyPatch, groups: dict[str, tuple[str, str]] | None = None):
    """A recording client wired into both the tools and a bare adapter."""
    client = RecordingClient(groups or {})

    @asynccontextmanager
    async def fake_build_client(**_: Any):
        yield client

    monkeypatch.setattr(messaging, "build_client", fake_build_client)
    return client, _adapter(SELF_HANDLE, client)


def session_of(chat_id: str) -> str:
    """The session key ``_woken_by`` gives a turn woken in ``chat_id``."""
    return f"agent:main:rine:dm:{chat_id}"


@contextmanager
def bound_session(session_key: str):
    """Bind the gateway session the host binds around a turn, so tools read it."""
    from gateway.session_context import clear_session_vars, set_session_vars

    tokens = set_session_vars(session_key=session_key)
    try:
        yield
    finally:
        clear_session_vars(tokens)


async def tool(chat_id: str, handler: Callable[..., Awaitable[str]], args: dict[str, Any]) -> str:
    """Call a rine tool as the model does inside the turn woken in ``chat_id``."""
    with bound_session(session_of(chat_id)):
        return await handler(args)


async def final(adapter, chat_id: str, inbound: str) -> None:
    """The gateway relaying the turn's final text."""
    await adapter.send(chat_id, "the final text", reply_to=inbound, metadata=FINAL)
