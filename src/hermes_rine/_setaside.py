"""Durable set-aside list: the inbound messages this agent could not read.

An unreadable inbound message is **drained** (acked) rather than left ``new``, because a
real inbox with an unverifiable backlog would otherwise re-quarantine it on every poll
forever. Draining it used to also *forget* it; that is what this module replaces. The id is
recorded here first, so a cause that fixes itself (a group sender key that had not
arrived yet, a restored config dir) is still answerable — by the wake channel's own
sweep, or on demand through ``rine_retry_unreadable``.

🔴 A **separate table**, never a widening of ``consumed_message``: that journal means
*handled*, and ``_poll._dispatch`` turns ``is_consumed`` into "re-ack only, never
dispatch". A message parked here has explicitly **not** been handled, so journaling it
would make it permanently undispatchable the moment it became readable.

Bounded on two axes, because the design it replaces was rejected for being unbounded:
``SETASIDE_MAX_ATTEMPTS`` retries per message and ``SETASIDE_MAX_ROWS`` rows per agent.
A row at the attempt bound stays *listed* but is never retried again, so "where did my
message go?" keeps an answer long after the retrying stops.

Like the journal, this stores **only** ``message.id`` — never a payload, never
ciphertext, never the decrypt error's prose — and shares the journal's file, so the
plugin's durable state stays one artefact to back up or delete.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum

from hermes_rine._journal import default_journal_path

logger = logging.getLogger("hermes_rine.setaside")

__all__ = [
    "SETASIDE_MAX_ATTEMPTS",
    "SETASIDE_MAX_ROWS",
    "SETASIDE_SWEEP_BATCH",
    "SWEEP_INTERVAL_ENV",
    "SetAsideReason",
    "SetAsideRow",
    "SetAsideStore",
    "sweep_interval_seconds",
]

#: Read attempts a single message gets before it is listed-but-never-retried.
SETASIDE_MAX_ATTEMPTS = 5

#: Rows kept per agent. Reaching it evicts, so the list cannot grow without bound.
SETASIDE_MAX_ROWS = 500

#: Rows one sweep re-reads. The list is walked oldest-first, so a full list is still
#: covered across consecutive ticks — this bounds the I/O *per tick*, which is the
#: "log/IO storm" the rejected never-ack design was rejected for.
SETASIDE_SWEEP_BATCH = 50

SWEEP_INTERVAL_ENV = "RINE_SETASIDE_SWEEP_INTERVAL"
_DEFAULT_SWEEP_INTERVAL = 300


class SetAsideReason(StrEnum):
    """Why a message is on the list — the retry decision, not the error's prose.

    ``UNREADABLE`` is the transient class: malformed, an unsupported encryption
    version, or a group sender key that has not arrived yet. It is retried up to the
    attempt bound.

    ``AUTHENTICITY`` is a refused sender — a signature that does not cover the bytes,
    or a message belonging to another agent. It is recorded already exhausted: those
    bytes cannot start to verify, so a retry is pure waste, and re-presenting an
    attacker-controlled message on a schedule is a log amplifier whose cadence the
    attacker would choose. It is still listed.
    """

    UNREADABLE = "unreadable"
    AUTHENTICITY = "authenticity"


@dataclass(frozen=True, slots=True)
class SetAsideRow:
    """One parked message: ids and counters only, never content."""

    agent: str
    message_id: str
    reason: SetAsideReason
    attempts: int
    first_seen: float
    last_tried: float | None
    exhausted: bool


def sweep_interval_seconds() -> int:
    """Seconds between automatic sweeps (``RINE_SETASIDE_SWEEP_INTERVAL``, default 300).

    Read per tick rather than once at construction so an operator debugging a stuck
    message can shorten it without restarting the gateway. A value that is not a
    positive integer falls back to the default with a warning — a silently disabled
    retry would read exactly like the behaviour this module replaces.
    """
    raw = os.getenv(SWEEP_INTERVAL_ENV, "").strip()
    if not raw:
        return _DEFAULT_SWEEP_INTERVAL
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 0
    if seconds < 1:
        logger.warning(
            "%s=%r is not a positive number of seconds; using %d",
            SWEEP_INTERVAL_ENV,
            raw,
            _DEFAULT_SWEEP_INTERVAL,
        )
        return _DEFAULT_SWEEP_INTERVAL
    return seconds


_COLUMNS = "agent, message_id, reason, attempts, first_seen, last_tried, exhausted"

_SQL_CREATE = (
    "CREATE TABLE IF NOT EXISTS setaside_message ("
    "agent TEXT NOT NULL, message_id TEXT NOT NULL, reason TEXT NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, last_tried REAL, "
    "exhausted INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY (agent, message_id))"
)
_SQL_UPSERT = (
    f"INSERT INTO setaside_message ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (agent, message_id) DO UPDATE SET "
    "reason = excluded.reason, attempts = setaside_message.attempts + 1, "
    "last_tried = excluded.last_tried, "
    "exhausted = (setaside_message.exhausted OR excluded.exhausted "
    "OR setaside_message.attempts + 1 >= ?) "
    f"RETURNING {_COLUMNS}"
)
_SQL_TOUCH = (
    "UPDATE setaside_message SET last_tried = ? WHERE agent = ? AND message_id = ? "
    f"RETURNING {_COLUMNS}"
)
_SQL_GET = f"SELECT {_COLUMNS} FROM setaside_message WHERE agent = ? AND message_id = ?"
_SQL_ENTRIES = (
    f"SELECT {_COLUMNS} FROM setaside_message WHERE agent = ? "
    "ORDER BY exhausted ASC, first_seen ASC"
)
_SQL_DUE = (
    f"SELECT {_COLUMNS} FROM setaside_message WHERE agent = ? AND exhausted = 0 "
    "ORDER BY first_seen ASC LIMIT ?"
)
_SQL_FORGET = "DELETE FROM setaside_message WHERE agent = ? AND message_id = ?"
_SQL_COUNT = "SELECT COUNT(*) FROM setaside_message WHERE agent = ?"
# Oldest EXHAUSTED row first (it will never be retried, so it is the cheapest thing to
# lose), then the oldest row overall. ``exhausted DESC`` sorts every exhausted row ahead
# of every live one; ``first_seen ASC`` orders within each class.
_SQL_EVICT = (
    "DELETE FROM setaside_message WHERE rowid IN ("
    "SELECT rowid FROM setaside_message WHERE agent = ? "
    "ORDER BY exhausted DESC, first_seen ASC LIMIT ?)"
)


def _row(raw: tuple[str, str, str, int, float, float | None, int]) -> SetAsideRow:
    """Build a ``SetAsideRow`` from one sqlite tuple."""
    return SetAsideRow(
        agent=raw[0],
        message_id=raw[1],
        reason=SetAsideReason(raw[2]),
        attempts=raw[3],
        first_seen=raw[4],
        last_tried=raw[5],
        exhausted=bool(raw[6]),
    )


class SetAsideStore:
    """The durable per-agent list of messages that could not be read.

    ``record`` is the idempotent upsert the wake channel writes **before** it acks
    (the same record-before-ack discipline as :mod:`hermes_rine._journal`: a crash in
    that window leaves the message un-acked, so the next poll parks it again). ``due``
    is what the sweep retries; ``entries`` is what the tool lists, exhausted rows
    included; ``forget`` drops a row that opened or that the server no longer has.
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

    def record(
        self,
        agent: str,
        message_id: str,
        reason: SetAsideReason,
        *,
        exhausted: bool = False,
    ) -> SetAsideRow:
        """Park ``message_id``, counting this sighting as an attempt; return the row.

        Idempotent by ``(agent, message_id)``: a re-sighting bumps ``attempts`` and
        keeps the original ``first_seen``, so the eviction order stays "oldest
        problem first" no matter how often a message is retried. The row is marked
        exhausted once ``attempts`` reaches :data:`SETASIDE_MAX_ATTEMPTS`, or
        immediately when the caller passes ``exhausted=True`` (an authenticity
        failure, which is never retried) — and exhaustion is **sticky**, so nothing
        un-gives-up on a row it has already given up on.

        🔴 The increment happens inside the upsert (``attempts + 1`` in SQL,
        ``RETURNING`` the row it wrote), never as a Python read-modify-write across two
        statements. The adapter and ``rine_retry_unreadable`` each hold their **own**
        store over the same file, so a read-then-write would let two callers read the
        same ``attempts`` and write the same successor — a lost increment silently
        widens the bound whose absence got the previous design rejected.
        """
        conn = self._connection()
        now = _now()
        raw = conn.execute(
            _SQL_UPSERT,
            (
                agent,
                message_id,
                str(reason),
                1,
                now,
                now,
                int(exhausted or SETASIDE_MAX_ATTEMPTS <= 1),
                SETASIDE_MAX_ATTEMPTS,
            ),
        ).fetchone()
        self._evict_overflow(conn, agent)
        conn.commit()
        return _row(raw)

    def touch(self, agent: str, message_id: str) -> SetAsideRow | None:
        """Stamp ``last_tried`` on a parked row without spending an attempt.

        Returns the row as it now stands, or None when the id is not on the list.

        🔴 This is what keeps the **on-demand** retry out of the **automatic** retry's
        budget. ``rine_retry_unreadable`` is model-facing and gated by no allowlist, so
        a model that retries four times "helpfully" while a sender key is still in
        flight would otherwise exhaust every live row through :meth:`record` — ``due``
        then returns nothing, the tick retries nothing ever again, and the operator's
        documented remedy ("fix the cause, the sweep will get it") is dead with one INFO
        line as its only trace. An operator-initiated look records that it happened and
        nothing else.
        """
        conn = self._connection()
        raw = conn.execute(_SQL_TOUCH, (_now(), agent, message_id)).fetchone()
        conn.commit()
        return _row(raw) if raw is not None else None

    def _evict_overflow(self, conn: sqlite3.Connection, agent: str) -> None:
        """Trim ``agent``'s rows back to :data:`SETASIDE_MAX_ROWS`."""
        count = int(conn.execute(_SQL_COUNT, (agent,)).fetchone()[0])
        over = count - SETASIDE_MAX_ROWS
        if over <= 0:
            return
        conn.execute(_SQL_EVICT, (agent, over))
        logger.warning(
            "rine set-aside list for %s is full (%d rows); dropped %d oldest",
            agent or "?",
            SETASIDE_MAX_ROWS,
            over,
        )

    def get(self, agent: str, message_id: str) -> SetAsideRow | None:
        """The parked row for one message, or None if it is not on the list."""
        raw = self._connection().execute(_SQL_GET, (agent, message_id)).fetchone()
        return _row(raw) if raw is not None else None

    def due(self, agent: str, limit: int = SETASIDE_SWEEP_BATCH) -> list[SetAsideRow]:
        """Up to ``limit`` not-yet-exhausted rows for ``agent``, oldest first."""
        rows = self._connection().execute(_SQL_DUE, (agent, limit)).fetchall()
        return [_row(raw) for raw in rows]

    def entries(self, agent: str) -> list[SetAsideRow]:
        """Every parked row for ``agent`` — retryable first, then exhausted."""
        rows = self._connection().execute(_SQL_ENTRIES, (agent,)).fetchall()
        return [_row(raw) for raw in rows]

    def forget(self, agent: str, message_id: str) -> None:
        """Drop one row (it opened, or the server no longer has it). Idempotent."""
        conn = self._connection()
        conn.execute(_SQL_FORGET, (agent, message_id))
        conn.commit()

    def close(self) -> None:
        """Close the underlying connection if it was opened (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _now() -> float:
    """Wall-clock seconds. Wall clock, not monotonic: the rows outlive the process."""
    return time.time()
