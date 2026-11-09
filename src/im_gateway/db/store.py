"""SQLite persistence layer for configuration and runtime data (HANDOVER §13).

Design constraints from the handover:

- **SQLite is the single source of truth** for both declarative config
  (``channels`` / ``backends`` / ``settings``) and runtime data
  (``messages`` / ``turn_events`` / ``subjects`` / ``inbound_dedup`` /
  ``audit`` / ``schema_migrations``).
- **One writer** (the daemon) in steady state; the offline CLI takes a
  ``BEGIN IMMEDIATE`` transaction when the daemon is down.
- **No shared connection across threads.**  Every public method opens a short
  lived connection, applies the operational pragmas, does its work and closes.
  SQLite handles the rest via WAL + ``busy_timeout``.
- **No business logic on history.** ``messages`` exists for review, history
  queries and troubleshooting; nothing may read it to continue a conversation.

Pragmas applied on every connection: ``foreign_keys=ON`` and
``busy_timeout=5000``.  ``migrate()`` additionally sets
``auto_vacuum=INCREMENTAL`` (before WAL, see :mod:`im_gateway.db.schema`) and
``journal_mode=WAL``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from im_gateway.db.models import (
    DIRECTIONS,
    BackendRecord,
    ChannelRecord,
    MessageRecord,
    SubjectRecord,
)
from im_gateway.db.schema import MIGRATIONS

__all__ = ["Store"]

#: Seconds a connection waits for a competing writer before raising
#: ``sqlite3.OperationalError: database is locked``.
BUSY_TIMEOUT_MS: Final[int] = 5000

#: Retention pruning is done in bounded batches so no long write transaction
#: blocks the daemon (HANDOVER §13.9: ``LIMIT 5000`` loop).
PRUNE_BATCH_SIZE: Final[int] = 5000

#: Whitelist for :meth:`Store.update_channel` (public names, not column names).
CHANNEL_UPDATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"kind", "tenant_id", "alias", "name", "config", "constraints", "enabled"}
)

#: Tables reported by :meth:`Store.table_counts` and :meth:`Store.prune`.
TABLES: Final[tuple[str, ...]] = (
    "schema_migrations",
    "settings",
    "channels",
    "backends",
    "subjects",
    "inbound_dedup",
    "audit",
    "messages",
    "turn_events",
)

_SECONDS_PER_DAY: Final[float] = 86_400.0

# public record field name -> SQLite column name
_CHANNEL_COLUMNS: Final[dict[str, str]] = {
    "kind": "kind",
    "tenant_id": "tenant_id",
    "alias": "alias",
    "name": "name",
    "config": "config_json",
    "constraints": "constraints_json",
    "enabled": "enabled",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    """Serialize a JSON column the one way the whole project expects."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(raw: str) -> Any:
    return json.loads(raw)


def _channel_from_row(row: sqlite3.Row) -> ChannelRecord:
    return ChannelRecord(
        channel_id=row["channel_id"],
        kind=row["kind"],
        tenant_id=row["tenant_id"],
        alias=row["alias"],
        name=row["name"],
        config=_loads(row["config_json"]),
        constraints=_loads(row["constraints_json"]),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_started_at=row["last_started_at"],
    )


def _backend_from_row(row: sqlite3.Row) -> BackendRecord:
    return BackendRecord(
        tenant_id=row["tenant_id"],
        base_url=row["base_url"],
        token_ref=row["token_ref"],
        timeout_ms=row["timeout_ms"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _subject_from_row(row: sqlite3.Row) -> SubjectRecord:
    return SubjectRecord(
        channel_id=row["channel_id"],
        subject_id=row["subject_id"],
        chat_type=row["chat_type"],
        display_name=row["display_name"],
        metadata=_loads(row["metadata_json"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
    )


def _message_from_row(row: sqlite3.Row) -> MessageRecord:
    return MessageRecord(
        channel_id=row["channel_id"],
        subject_id=row["subject_id"],
        session_key=row["session_key"],
        direction=row["direction"],
        kind=row["kind"],
        content=_loads(row["content_json"]),
        metadata=_loads(row["metadata_json"]),
        platform_msg_id=row["platform_msg_id"],
        ts=row["ts"],
        error=row["error"],
        turn_id=row["turn_id"],
    )


def _retention_days(value: Any) -> float | None:
    """Coerce a retention day knob to a cutoff offset; ``None`` = don't prune.

    Missing, negative or non-numeric values mean "leave this table alone".
    A value of ``0`` is meaningful: delete everything older than *now*.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


def _retention_max_rows(value: Any) -> int | None:
    """Coerce ``chat_max_rows``; ``None``/non-positive means "no row cap"."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return int(value)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """SQLite-backed store.

    ``path`` must be a file path.  ``":memory:"`` is *not* supported because
    each method opens its own connection (an in-memory database would be
    thrown away after every call).  Use :class:`tempfile` or ``tmp_path`` in
    tests.

    Instances are cheap and thread-safe: no connection is retained between
    calls.  ``close()`` exists for API symmetry and is a no-op.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(Path(path))

    # -- connection / lifecycle ------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection, commit on success, roll back on error."""
        conn = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def migrate(self) -> None:
        """Create / upgrade the schema.  Idempotent; records applied versions."""
        path = Path(self._path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # auto_vacuum must be set while the database is empty and *before*
            # WAL mode is enabled, otherwise SQLite ignores the change.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("PRAGMA journal_mode=WAL")
            applied = self._applied_versions(conn)
            for version in sorted(MIGRATIONS):
                if version in applied:
                    continue
                conn.executescript(MIGRATIONS[version])
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
                applied.add(version)

    @staticmethod
    def _applied_versions(conn: sqlite3.Connection) -> set[int]:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if exists is None:
            return set()
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {int(row[0]) for row in rows}

    def close(self) -> None:
        """No-op: connections are not kept between calls."""

    # -- settings --------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return _loads(row["value_json"])

    def set_setting(self, key: str, value: Any) -> None:
        encoded = _dumps(value)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  value_json = excluded.value_json,
                  updated_at = excluded.updated_at
                """,
                (key, encoded, now),
            )

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value_json FROM settings ORDER BY key").fetchall()
        return {row["key"]: _loads(row["value_json"]) for row in rows}

    # -- channels --------------------------------------------------------------

    def list_channels(self, *, include_disabled: bool = True) -> list[ChannelRecord]:
        sql = "SELECT * FROM channels"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY channel_id"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_channel_from_row(row) for row in rows]

    def get_channel(self, ref: str) -> ChannelRecord | None:
        """Look up by immutable ``channel_id`` first, then by mutable ``alias``."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (ref,)).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM channels WHERE alias = ?", (ref,)).fetchone()
        return None if row is None else _channel_from_row(row)

    def add_channel(self, record: ChannelRecord) -> None:
        """Insert a channel; duplicate ``channel_id``/``alias`` raise ``ValueError``."""
        with self._connect() as conn:
            clash = conn.execute(
                "SELECT 1 FROM channels WHERE channel_id = ?", (record.channel_id,)
            ).fetchone()
            if clash is not None:
                raise ValueError(f"channel_id already exists: {record.channel_id!r}")
            if record.alias is not None:
                clash = conn.execute(
                    "SELECT 1 FROM channels WHERE alias = ?", (record.alias,)
                ).fetchone()
                if clash is not None:
                    raise ValueError(f"alias already exists: {record.alias!r}")
            try:
                conn.execute(
                    """
                    INSERT INTO channels(
                      channel_id, kind, tenant_id, alias, name,
                      config_json, constraints_json, enabled,
                      created_at, updated_at, last_started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.channel_id,
                        record.kind,
                        record.tenant_id,
                        record.alias,
                        record.name,
                        _dumps(record.config),
                        _dumps(record.constraints),
                        int(record.enabled),
                        record.created_at,
                        record.updated_at,
                        record.last_started_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:  # concurrent writer won the race
                if "UNIQUE" not in str(exc):
                    raise
                raise ValueError(f"duplicate channel_id or alias: {record.channel_id!r}") from exc

    def update_channel(self, channel_id: str, **fields: Any) -> ChannelRecord:
        """Update whitelisted mutable fields; bumps ``updated_at`` automatically.

        Unknown field names raise ``ValueError``; a missing channel raises
        ``KeyError``; an alias already taken by another channel raises
        ``ValueError``.
        """
        unknown = set(fields) - CHANNEL_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unknown channel field(s): {', '.join(sorted(unknown))}")

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
            if row is None:
                raise KeyError(channel_id)

            assignments = ["updated_at = ?"]
            params: list[Any] = [time.time()]
            for name in sorted(fields):
                value = fields[name]
                if name in ("config", "constraints"):
                    value = _dumps(value)
                elif name == "enabled":
                    value = int(bool(value))
                assignments.append(f"{_CHANNEL_COLUMNS[name]} = ?")
                params.append(value)
            params.append(channel_id)
            try:
                conn.execute(
                    f"UPDATE channels SET {', '.join(assignments)} WHERE channel_id = ?",
                    params,
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE" not in str(exc):
                    raise
                alias = fields.get("alias")
                raise ValueError(f"alias already exists: {alias!r}") from exc

            updated = conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
            if updated is None:  # pragma: no cover - row cannot vanish in one transaction
                raise KeyError(channel_id)
            return _channel_from_row(updated)

    def delete_channel(self, channel_id: str, *, purge: bool = False) -> bool:
        """Delete a channel row; ``purge`` also drops its subjects/messages/turn_events.

        ``turn_events`` has no ``channel_id`` column (HANDOVER §13.2), so a purge
        removes the turn events referenced by the channel's messages' ``turn_id``.
        Returns whether the channel row existed.
        """
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
            if row is None:
                return False
            if purge:
                conn.execute(
                    """
                    DELETE FROM turn_events WHERE turn_id IN (
                      SELECT turn_id FROM messages
                      WHERE channel_id = ? AND turn_id IS NOT NULL
                    )
                    """,
                    (channel_id,),
                )
                conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
                conn.execute("DELETE FROM subjects WHERE channel_id = ?", (channel_id,))
            conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
        return True

    def mark_channel_started(self, channel_id: str, ts: float) -> None:
        """Record the last successful registration; silently ignores unknown ids."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE channels SET last_started_at = ? WHERE channel_id = ?",
                (ts, channel_id),
            )

    # -- backends --------------------------------------------------------------

    def list_backends(self) -> list[BackendRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM backends ORDER BY tenant_id").fetchall()
        return [_backend_from_row(row) for row in rows]

    def get_backend(self, tenant_id: str) -> BackendRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM backends WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return None if row is None else _backend_from_row(row)

    def upsert_backend(self, record: BackendRecord) -> None:
        """Insert or update by ``tenant_id``; the original ``created_at`` is kept."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO backends(
                  tenant_id, base_url, token_ref, timeout_ms, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                  base_url = excluded.base_url,
                  token_ref = excluded.token_ref,
                  timeout_ms = excluded.timeout_ms,
                  enabled = excluded.enabled,
                  updated_at = excluded.updated_at
                """,
                (
                    record.tenant_id,
                    record.base_url,
                    record.token_ref,
                    record.timeout_ms,
                    int(record.enabled),
                    record.created_at,
                    record.updated_at,
                ),
            )

    def delete_backend(self, tenant_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM backends WHERE tenant_id = ?", (tenant_id,))
        return cursor.rowcount > 0

    # -- subjects --------------------------------------------------------------

    def upsert_subject(self, record: SubjectRecord) -> None:
        """Insert a subject or refresh ``last_seen``/``metadata`` of an existing one.

        ``first_seen`` and the display fields are only written on first insert.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subjects(
                  channel_id, subject_id, chat_type, display_name,
                  metadata_json, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, subject_id) DO UPDATE SET
                  last_seen = excluded.last_seen,
                  metadata_json = excluded.metadata_json
                """,
                (
                    record.channel_id,
                    record.subject_id,
                    record.chat_type,
                    record.display_name,
                    _dumps(record.metadata),
                    record.first_seen,
                    record.last_seen,
                ),
            )

    def list_subjects(self, channel_id: str) -> list[SubjectRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subjects WHERE channel_id = ? ORDER BY subject_id",
                (channel_id,),
            ).fetchall()
        return [_subject_from_row(row) for row in rows]

    def get_subject(self, channel_id: str, subject_id: str) -> SubjectRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subjects WHERE channel_id = ? AND subject_id = ?",
                (channel_id, subject_id),
            ).fetchone()
        return None if row is None else _subject_from_row(row)

    # -- inbound dedup ---------------------------------------------------------

    def claim_inbound(self, idempotency_key: str, channel_id: str) -> bool:
        """Atomically claim an idempotency key.

        Returns ``True`` the first time a key is seen (process the message) and
        ``False`` for a platform redelivery, via ``INSERT OR IGNORE``.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO inbound_dedup(idempotency_key, channel_id, created_at) "
                "VALUES (?, ?, ?)",
                (idempotency_key, channel_id, time.time()),
            )
            return cursor.rowcount == 1

    # -- audit -----------------------------------------------------------------

    def add_audit(
        self,
        action: str,
        *,
        actor: str,
        channel_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit row.  Secrets must be masked by the caller."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit(ts, actor, action, channel_id, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), actor, action, channel_id, _dumps(payload or {})),
            )

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first; ``payload_json`` is decoded into a ``payload`` dict."""
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, actor, action, channel_id, payload_json FROM audit "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "channel_id": row["channel_id"],
                "payload": _loads(row["payload_json"]),
            }
            for row in rows
        ]

    # -- messages --------------------------------------------------------------

    def add_message(self, record: MessageRecord) -> int:
        """Insert one message row and return its auto-increment ``id``."""
        if record.direction not in DIRECTIONS:
            raise ValueError(f"invalid direction {record.direction!r}; expected one of {sorted(DIRECTIONS)}")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages(
                  channel_id, subject_id, session_key, direction, turn_id, kind,
                  content_json, metadata_json, platform_msg_id, ts, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.channel_id,
                    record.subject_id,
                    record.session_key,
                    record.direction,
                    record.turn_id,
                    record.kind,
                    _dumps(record.content),
                    _dumps(record.metadata),
                    record.platform_msg_id,
                    record.ts,
                    record.error,
                ),
            )
            message_id = cursor.lastrowid
        if message_id is None:  # pragma: no cover - sqlite always returns a rowid here
            raise RuntimeError("sqlite3 did not return a row id for the inserted message")
        return int(message_id)

    def query_history(
        self,
        *,
        channel_id: str | None = None,
        subject_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MessageRecord]:
        """Query messages newest-first.

        ``since``/``until`` are inclusive bounds on ``ts``.  A non-positive
        ``limit`` yields an empty list; a negative ``offset`` is clamped to 0.
        """
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        sql = "SELECT * FROM messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        params.extend((limit, max(offset, 0)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_message_from_row(row) for row in rows]

    # -- maintenance -----------------------------------------------------------

    def prune(self, retention: dict[str, Any], *, now: float | None = None) -> dict[str, int]:
        """Apply the retention policy in bounded batches (HANDOVER §13.9).

        Recognised keys: ``chat_days``, ``chat_max_rows``, ``turn_events_days``,
        ``dedup_days``, ``audit_days``.  Missing keys are skipped; a day value of
        ``0`` deletes everything older than ``now``.  ``chat_max_rows`` is applied
        *after* the time-based chat prune, deleting the oldest rows over the cap.

        Returns the number of deleted rows per table.
        """
        reference = time.time() if now is None else now
        counts: dict[str, int] = {"messages": 0, "turn_events": 0, "inbound_dedup": 0, "audit": 0}
        with self._connect() as conn:
            chat_days = _retention_days(retention.get("chat_days"))
            if chat_days is not None:
                counts["messages"] += _delete_older_than(
                    conn, "messages", "ts", reference - chat_days * _SECONDS_PER_DAY
                )
            turn_days = _retention_days(retention.get("turn_events_days"))
            if turn_days is not None:
                counts["turn_events"] += _delete_older_than(
                    conn, "turn_events", "ts", reference - turn_days * _SECONDS_PER_DAY
                )
            dedup_days = _retention_days(retention.get("dedup_days"))
            if dedup_days is not None:
                counts["inbound_dedup"] += _delete_older_than(
                    conn, "inbound_dedup", "created_at", reference - dedup_days * _SECONDS_PER_DAY
                )
            audit_days = _retention_days(retention.get("audit_days"))
            if audit_days is not None:
                counts["audit"] += _delete_older_than(
                    conn, "audit", "ts", reference - audit_days * _SECONDS_PER_DAY
                )
            counts["messages"] += _trim_to_max_rows(conn, _retention_max_rows(retention.get("chat_max_rows")))
        return counts

    def vacuum(self) -> None:
        """Release free pages (``auto_vacuum=INCREMENTAL``); cheap and online."""
        with self._connect() as conn:
            conn.execute("PRAGMA incremental_vacuum")

    def backup(self, dest: str | Path) -> None:
        """Write a consistent online snapshot with ``VACUUM INTO``.

        ``ValueError`` if ``dest`` already exists (SQLite refuses to overwrite).
        """
        target = Path(dest)
        if target.exists():
            raise ValueError(f"backup destination already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("VACUUM INTO ?", (str(target),))

    def table_counts(self) -> dict[str, int]:
        """Row count for every table in schema v1."""
        counts: dict[str, int] = {}
        with self._connect() as conn:
            for table in TABLES:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                counts[table] = int(row["n"]) if row is not None else 0
        return counts

    def integrity_check(self) -> str:
        """Run ``PRAGMA integrity_check``; returns ``"ok"`` for a healthy file."""
        with self._connect() as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None:  # pragma: no cover - pragma always returns a row
            return "unknown"
        return str(row[0])


# ---------------------------------------------------------------------------
# Batching helpers for prune (module level: no state, easy to test)
# ---------------------------------------------------------------------------


def _delete_older_than(
    conn: sqlite3.Connection,
    table: str,
    ts_column: str,
    cutoff: float,
) -> int:
    """Delete rows with ``ts_column < cutoff`` in batches; returns rows removed."""
    removed = 0
    while True:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE {ts_column} < ? LIMIT ?"
            ")",
            (cutoff, PRUNE_BATCH_SIZE),
        )
        batch = cursor.rowcount
        removed += batch
        if batch < PRUNE_BATCH_SIZE:
            return removed


def _trim_to_max_rows(conn: sqlite3.Connection, max_rows: int | None) -> int:
    """Delete oldest messages until the table holds at most ``max_rows`` rows."""
    if max_rows is None:
        return 0
    row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    excess = (int(row["n"]) if row is not None else 0) - max_rows
    removed = 0
    while excess > 0:
        batch = min(PRUNE_BATCH_SIZE, excess)
        cursor = conn.execute(
            "DELETE FROM messages WHERE id IN ("
            "SELECT id FROM messages ORDER BY ts ASC, id ASC LIMIT ?"
            ")",
            (batch,),
        )
        deleted = cursor.rowcount
        removed += deleted
        excess -= deleted
        if deleted == 0:  # pragma: no cover - defensive: table shrank concurrently
            break
    return removed
