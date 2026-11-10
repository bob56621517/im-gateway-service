"""SQLite schema (HANDOVER §13.2; v2 in §16).

The DDL text below is the **single authoritative definition** of the on-disk
schema. ``Store.migrate()`` applies every entry of :data:`MIGRATIONS` whose
version is not yet recorded in ``schema_migrations``; therefore each migration
must be idempotent (``IF NOT EXISTS``) and must never edit an already shipped
entry.

Schema history:

* **v1** — tenant-aware routing: a ``backends`` table (tenant -> webhook) and a
  ``channels.tenant_id`` column.
* **v2** — the gateway no longer manages backend routing (GitHub issue #3): drop
  the ``backends`` table and ``channels.tenant_id``. The backend is one URL from
  ``--backend-url`` / ``IMGW_BACKEND_URL``.

Operational pragmas (applied by ``Store``, not here):

- ``journal_mode=WAL`` — one writer, many readers;
- ``auto_vacuum=INCREMENTAL`` — paired with periodic ``incremental_vacuum``;
- ``foreign_keys=ON`` — no FK constraints in v1, but keep future ones honest;
- ``busy_timeout=5000`` — short-lived connections, bounded contention.

Note: SQLite only honours a change of ``auto_vacuum`` while the database is
still empty, and not once WAL mode is active. ``Store.migrate()`` therefore sets
it *before* enabling WAL and creating tables.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "SCHEMA_VERSION",
]

#: Current schema version (v2: single backend URL, no tenant routing).
SCHEMA_VERSION: Final[int] = 2

#: Version -> DDL. Sorted application order is insertion order of the mapping.
_SCHEMA_V1: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id       TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,
  tenant_id        TEXT,
  alias            TEXT UNIQUE,
  name             TEXT NOT NULL DEFAULT '',
  config_json      TEXT NOT NULL,
  constraints_json TEXT NOT NULL DEFAULT '{}',
  enabled          INTEGER NOT NULL DEFAULT 1,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL,
  last_started_at  REAL
);

CREATE TABLE IF NOT EXISTS backends (
  tenant_id   TEXT PRIMARY KEY,
  base_url    TEXT NOT NULL,
  token_ref   TEXT NOT NULL,
  timeout_ms  INTEGER NOT NULL DEFAULT 120000,
  enabled     INTEGER NOT NULL DEFAULT 1,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
  channel_id    TEXT NOT NULL,
  subject_id    TEXT NOT NULL,
  chat_type     TEXT NOT NULL DEFAULT '',
  display_name  TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  first_seen    REAL NOT NULL,
  last_seen     REAL NOT NULL,
  PRIMARY KEY (channel_id, subject_id)
);

CREATE TABLE IF NOT EXISTS inbound_dedup (
  idempotency_key TEXT PRIMARY KEY,
  channel_id      TEXT NOT NULL,
  created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id           INTEGER PRIMARY KEY,
  ts           REAL NOT NULL,
  actor        TEXT NOT NULL,
  action       TEXT NOT NULL,
  channel_id   TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY,
  channel_id      TEXT NOT NULL,
  subject_id      TEXT NOT NULL,
  session_key     TEXT NOT NULL,
  direction       TEXT NOT NULL,
  turn_id         TEXT,
  kind            TEXT NOT NULL,
  content_json    TEXT NOT NULL,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  platform_msg_id TEXT,
  ts              REAL NOT NULL,
  error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_subject ON messages(channel_id, subject_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);

CREATE TABLE IF NOT EXISTS turn_events (
  turn_id      TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  ts           REAL NOT NULL,
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (turn_id, seq)
);
"""

MIGRATIONS: Final[dict[int, str]] = {
    1: _SCHEMA_V1,
    2: """
-- v2: the gateway no longer manages backend routing (GitHub issue #3): one
-- backend URL comes from --backend-url / IMGW_BACKEND_URL. SQLite >= 3.35 is
-- required for DROP COLUMN; the bundled Python 3.12 satisfies that.
DROP TABLE IF EXISTS backends;
ALTER TABLE channels DROP COLUMN tenant_id;
""",
}

LATEST_SCHEMA_VERSION: Final[int] = max(MIGRATIONS)
