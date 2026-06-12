"""Durable sqlite consumed-id journal for exactly-once inbound wake (D-WAKE).

A tiny ``(agent, message_id)`` table that survives a gateway restart, so a rine
message that was already woken-and-recorded is never dispatched a second time. The
adapter records an id **before** it acks (``mark_delivered``) — the record-before-ack
discipline lifted from ``rine-langchain``'s ``_sqlite_store.py``: if the process dies
between record and ack the message merely re-delivers and is recognised as consumed
(re-acked, not re-dispatched). The journal stores **only** ``message.id`` — never any
payload, never ciphertext.

Sync stdlib ``sqlite3`` is intentional: the loop touches it in short critical sections
(one row read / one idempotent upsert) — far cheaper than the event-loop hop an async
driver would cost. ``check_same_thread=False`` lets a single-threaded async loop and its
executor share the one connection; ``WAL`` keeps it lock-friendly if a sibling process
opens the same file. Side-effect-free import: the connection + table are created lazily
on first use, never at import.
"""

from __future__ import annotations

import os
import sqlite3

from rine import resolve_config_dir

__all__ = ["ConsumedJournal", "default_journal_path"]

_SQL_CREATE = (
    "CREATE TABLE IF NOT EXISTS consumed_message ("
    "agent TEXT NOT NULL, message_id TEXT NOT NULL, "
    "PRIMARY KEY (agent, message_id))"
)
_SQL_IS_CONSUMED = "SELECT 1 FROM consumed_message WHERE agent = ? AND message_id = ? LIMIT 1"
_SQL_MARK_CONSUMED = (
    "INSERT INTO consumed_message (agent, message_id) VALUES (?, ?) "
    "ON CONFLICT (agent, message_id) DO NOTHING"
)


def default_journal_path() -> str:
    """Resolve a cheap default path for the journal db.

    Prefers the rine config dir (so the journal travels with the agent's creds);
    falls back to ``~/.hermes`` if config-dir resolution fails. No network, no
    credential read — just a directory join. The parent dir is created on demand.
    """
    try:
        base = resolve_config_dir()
    except Exception:  # noqa: BLE001 — best-effort; absence just means use ~/.hermes
        base = os.path.join(os.path.expanduser("~"), ".hermes")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "hermes_rine_consumed.sqlite")


class ConsumedJournal:
    """A durable ``(agent, message_id)`` consumed-id journal (exactly-once guard).

    ``is_consumed`` is the redelivery guard; ``mark_consumed`` is the idempotent
    record the adapter writes *before* it acks. The connection + table are opened
    lazily on first use and reused for the journal's lifetime; ``close()`` releases
    it (the adapter calls it on ``disconnect()``).
    """

    def __init__(self, path: str | None = None) -> None:
        """Record the db path; the connection + table are created lazily."""
        self._path = path or default_journal_path()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        """Open (once) the sqlite connection and ensure the table exists."""
        if self._conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SQL_CREATE)
            conn.commit()
            self._conn = conn
        return self._conn

    def is_consumed(self, agent: str, message_id: str) -> bool:
        """True iff ``message_id`` was already woken-and-recorded for ``agent``."""
        row = self._connection().execute(_SQL_IS_CONSUMED, (agent, message_id)).fetchone()
        return row is not None

    def mark_consumed(self, agent: str, message_id: str) -> None:
        """Durably record ``(agent, message_id)`` as consumed (idempotent upsert)."""
        conn = self._connection()
        conn.execute(_SQL_MARK_CONSUMED, (agent, message_id))
        conn.commit()

    def close(self) -> None:
        """Close the underlying connection if it was opened (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
