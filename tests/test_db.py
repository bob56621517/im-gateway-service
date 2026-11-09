"""Tests for the SQLite persistence layer (HANDOVER §13.2, §13.9)."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from im_gateway.db import (
    BackendRecord,
    ChannelRecord,
    MessageRecord,
    Store,
    SubjectRecord,
)

# Fixed reference instants keep retention tests deterministic.
NOW = 1_000_000.0
DAY = 86_400.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@contextmanager
def raw_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Plain sqlite3 connection for asserting/priming table contents directly."""
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def make_channel(
    channel_id: str = "feishu:acme:sales",
    alias: str | None = "sales",
    **overrides: Any,
) -> ChannelRecord:
    values: dict[str, Any] = {
        "channel_id": channel_id,
        "kind": "feishu",
        "tenant_id": "acme",
        "alias": alias,
        "name": "Sales",
        "config": {"app_id": "cli_x", "app_secret": "${FEISHU_APP_SECRET}"},
        "constraints": {"max_text_len": 4000},
        "enabled": True,
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_000.0,
        "last_started_at": None,
    }
    values.update(overrides)
    return ChannelRecord(**values)


def make_subject(
    channel_id: str = "feishu:acme:sales",
    subject_id: str = "ou_1",
    first_seen: float = 100.0,
    last_seen: float = 100.0,
    **overrides: Any,
) -> SubjectRecord:
    values: dict[str, Any] = {
        "channel_id": channel_id,
        "subject_id": subject_id,
        "chat_type": "direct",
        "display_name": "张三",
        "metadata": {"department": "sales"},
        "first_seen": first_seen,
        "last_seen": last_seen,
    }
    values.update(overrides)
    return SubjectRecord(**values)


def make_message(
    ts: float,
    channel_id: str = "feishu:acme:sales",
    subject_id: str = "ou_1",
    direction: str = "in",
    **overrides: Any,
) -> MessageRecord:
    values: dict[str, Any] = {
        "channel_id": channel_id,
        "subject_id": subject_id,
        "session_key": subject_id,
        "direction": direction,
        "kind": "inbound" if direction == "in" else "message",
        "content": [{"type": "text", "text": "你好"}],
        "metadata": {"source": "test"},
        "platform_msg_id": None,
        "ts": ts,
        "error": None,
        "turn_id": None,
    }
    values.update(overrides)
    return MessageRecord(**values)


def insert_turn_event(path: Path, turn_id: str, seq: int, ts: float) -> None:
    with raw_connection(path) as conn:
        conn.execute(
            "INSERT INTO turn_events(turn_id, seq, ts, kind, payload_json) VALUES (?, ?, ?, ?, ?)",
            (turn_id, seq, ts, "message", "{}"),
        )


@pytest.fixture()
def store(db_path: Path) -> Iterator[Store]:
    with Store(db_path) as store:
        store.migrate()
        yield store


# ---------------------------------------------------------------------------
# migrations / schema
# ---------------------------------------------------------------------------


def test_migrate_is_idempotent_and_sets_pragmas(db_path: Path) -> None:
    store = Store(db_path)
    store.migrate()
    store.migrate()  # second call must be a no-op

    with raw_connection(db_path) as conn:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        }

    assert versions == [1]
    assert journal_mode == "wal"
    assert auto_vacuum == 2  # INCREMENTAL
    assert tables == {
        "schema_migrations",
        "settings",
        "channels",
        "backends",
        "subjects",
        "inbound_dedup",
        "audit",
        "messages",
        "turn_events",
    }
    counts = store.table_counts()
    assert set(counts) == tables
    assert counts["schema_migrations"] == 1
    assert all(count == 0 for table, count in counts.items() if table != "schema_migrations")
    assert store.integrity_check() == "ok"


def test_migrate_creates_missing_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "data" / "nested" / "runtime.db"
    with Store(nested) as store:
        store.migrate()
    assert nested.exists()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_settings_json_roundtrip(store: Store, db_path: Path) -> None:
    assert store.get_setting("missing") is None
    assert store.get_setting("missing", "fallback") == "fallback"

    payload = {
        "workers_per_channel": 2,
        "error_texts": {"zh": "出错了", "en": "failed"},
        "flags": [True, None, 1.5],
        "unicode": "中文",
    }
    store.set_setting("runtime", payload)
    assert store.get_setting("runtime") == payload

    store.set_setting("runtime", {"workers_per_channel": 4})
    assert store.get_settings() == {"runtime": {"workers_per_channel": 4}}

    # JSON `null` is a stored value, not a missing key.
    store.set_setting("null_value", None)
    assert store.get_setting("null_value", "fallback") is None

    # ensure_ascii=False: unicode must be readable in the raw column.
    with raw_connection(db_path) as conn:
        raw = conn.execute("SELECT value_json FROM settings WHERE key = 'runtime'").fetchone()[0]
    assert "workers_per_channel" in raw


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


def test_channel_crud_and_alias_lookup(store: Store) -> None:
    record = make_channel()
    store.add_channel(record)

    assert store.get_channel(record.channel_id) == record
    assert store.get_channel("sales") == record
    assert store.get_channel("does-not-exist") is None
    assert store.list_channels() == [record]

    # channel_id wins over alias.
    other = make_channel(channel_id="wecom:acme:ops", alias=record.channel_id, kind="wecom")
    store.add_channel(other)
    assert store.get_channel(record.channel_id) == record


def test_add_channel_rejects_duplicate_channel_id(store: Store) -> None:
    store.add_channel(make_channel())
    with pytest.raises(ValueError, match="channel_id"):
        store.add_channel(make_channel(alias="another"))


def test_add_channel_rejects_duplicate_alias(store: Store) -> None:
    store.add_channel(make_channel())
    with pytest.raises(ValueError, match="alias"):
        store.add_channel(make_channel(channel_id="feishu:acme:other"))


def test_add_channel_accepts_null_alias(store: Store) -> None:
    first = make_channel(alias=None)
    second = make_channel(channel_id="feishu:acme:other", alias=None)
    store.add_channel(first)
    store.add_channel(second)
    assert store.get_channel("feishu:acme:other") == second


def test_list_channels_enabled_filter(store: Store) -> None:
    enabled = make_channel(channel_id="feishu:acme:sales", alias="sales")
    disabled = make_channel(channel_id="wecom:acme:ops", alias="ops", kind="wecom", enabled=False)
    store.add_channel(enabled)
    store.add_channel(disabled)

    assert {r.channel_id for r in store.list_channels()} == {"feishu:acme:sales", "wecom:acme:ops"}
    assert {r.channel_id for r in store.list_channels(include_disabled=False)} == {"feishu:acme:sales"}


def test_update_channel_whitelist_and_timestamp(store: Store) -> None:
    record = make_channel()
    store.add_channel(record)

    updated = store.update_channel(
        record.channel_id,
        kind="wecom",
        tenant_id="ops",
        alias="sales-2",
        name="Sales 2",
        config={"corp_id": "ww"},
        constraints={"max_text_len": 1000},
        enabled=False,
    )
    assert updated.kind == "wecom"
    assert updated.tenant_id == "ops"
    assert updated.alias == "sales-2"
    assert updated.name == "Sales 2"
    assert updated.config == {"corp_id": "ww"}
    assert updated.constraints == {"max_text_len": 1000}
    assert updated.enabled is False
    assert updated.created_at == record.created_at
    assert updated.updated_at > record.updated_at

    with pytest.raises(ValueError, match="unknown channel field"):
        store.update_channel(record.channel_id, last_started_at=1.0)
    with pytest.raises(KeyError):
        store.update_channel("missing-channel", name="x")

    # A rejected update must not have changed anything.
    assert store.get_channel(record.channel_id) == updated


def test_update_channel_alias_conflict(store: Store) -> None:
    store.add_channel(make_channel(channel_id="c1", alias="one"))
    store.add_channel(make_channel(channel_id="c2", alias="two"))
    with pytest.raises(ValueError, match="alias"):
        store.update_channel("c2", alias="one")


def test_mark_channel_started(store: Store) -> None:
    record = make_channel()
    store.add_channel(record)
    store.mark_channel_started(record.channel_id, 123.0)
    assert store.get_channel(record.channel_id).last_started_at == 123.0  # type: ignore[union-attr]
    store.mark_channel_started("unknown-channel", 456.0)  # silently ignored


def test_delete_channel_keeps_runtime_data_by_default(store: Store) -> None:
    record = make_channel()
    store.add_channel(record)
    store.upsert_subject(make_subject())
    store.add_message(make_message(100.0))

    assert store.delete_channel(record.channel_id) is True
    assert store.delete_channel(record.channel_id) is False
    assert store.get_channel(record.channel_id) is None
    assert store.list_subjects(record.channel_id) != []
    assert store.query_history(channel_id=record.channel_id) != []


def test_delete_channel_purge_removes_runtime_data(store: Store, db_path: Path) -> None:
    record = make_channel()
    store.add_channel(record)
    store.upsert_subject(make_subject())
    store.add_message(make_message(100.0, turn_id="turn-1"))
    insert_turn_event(db_path, "turn-1", 1, 100.0)

    # Unrelated channel data must survive the purge.
    store.add_channel(make_channel(channel_id="feishu:acme:other", alias="other"))
    store.upsert_subject(make_subject(channel_id="feishu:acme:other", subject_id="ou_2"))

    assert store.delete_channel(record.channel_id, purge=True) is True

    counts = store.table_counts()
    assert counts["channels"] == 1
    assert counts["subjects"] == 1
    assert counts["messages"] == 0
    assert counts["turn_events"] == 0


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


def test_backend_upsert(store: Store) -> None:
    assert store.get_backend("") is None
    assert store.list_backends() == []

    first = BackendRecord(
        tenant_id="",
        base_url="http://127.0.0.1:9000",
        token_ref="${BACKEND_TOKEN}",
        timeout_ms=120_000,
        enabled=True,
        created_at=100.0,
        updated_at=100.0,
    )
    store.upsert_backend(first)
    assert store.get_backend("") == first
    assert store.list_backends() == [first]

    second = dataclasses.replace(
        first,
        base_url="http://127.0.0.1:9100",
        timeout_ms=5_000,
        enabled=False,
        created_at=999.0,
        updated_at=200.0,
    )
    store.upsert_backend(second)
    stored = store.get_backend("")
    assert stored is not None
    assert stored.base_url == "http://127.0.0.1:9100"
    assert stored.timeout_ms == 5_000
    assert stored.enabled is False
    assert stored.created_at == 100.0  # creation time survives updates
    assert stored.updated_at == 200.0

    assert store.delete_backend("") is True
    assert store.delete_backend("") is False
    assert store.get_backend("") is None


# ---------------------------------------------------------------------------
# subjects
# ---------------------------------------------------------------------------


def test_subject_upsert_updates_last_seen(store: Store) -> None:
    first = make_subject()
    store.upsert_subject(first)
    assert store.get_subject(first.channel_id, first.subject_id) == first
    assert store.list_subjects(first.channel_id) == [first]
    assert store.get_subject("other-channel", first.subject_id) is None
    assert store.list_subjects("other-channel") == []

    second = dataclasses.replace(first, last_seen=200.0, metadata={"department": "ops"})
    store.upsert_subject(second)
    stored = store.get_subject(first.channel_id, first.subject_id)
    assert stored is not None
    assert stored.last_seen == 200.0
    assert stored.metadata == {"department": "ops"}
    assert stored.first_seen == 100.0

    assert len(store.list_subjects(first.channel_id)) == 1


# ---------------------------------------------------------------------------
# inbound dedup
# ---------------------------------------------------------------------------


def test_claim_inbound_first_then_duplicate(store: Store) -> None:
    assert store.claim_inbound("feishu:om_1", "feishu:acme:sales") is True
    assert store.claim_inbound("feishu:om_1", "feishu:acme:sales") is False
    assert store.claim_inbound("feishu:om_2", "feishu:acme:sales") is True
    # The key is global, not per channel.
    assert store.claim_inbound("feishu:om_1", "wecom:acme:ops") is False


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def test_audit_write_and_read(store: Store) -> None:
    store.add_audit(
        "channel.add",
        actor="api-token:default",
        channel_id="c1",
        payload={"alias": "sales", "secret": "***"},
    )
    store.add_audit("config.export", actor="offline-cli")

    entries = store.list_audit()
    assert len(entries) == 2
    newest = entries[0]
    assert newest["action"] == "config.export"
    assert newest["actor"] == "offline-cli"
    assert newest["channel_id"] is None
    assert newest["payload"] == {}

    oldest = entries[1]
    assert oldest["action"] == "channel.add"
    assert oldest["channel_id"] == "c1"
    assert oldest["payload"] == {"alias": "sales", "secret": "***"}
    assert isinstance(oldest["ts"], float)

    assert [entry["action"] for entry in store.list_audit(limit=1)] == ["config.export"]
    assert store.list_audit(limit=0) == []


# ---------------------------------------------------------------------------
# messages / history
# ---------------------------------------------------------------------------


def test_add_message_returns_increasing_ids(store: Store) -> None:
    first = store.add_message(make_message(100.0))
    second = store.add_message(make_message(200.0))
    assert second > first


def test_add_message_rejects_invalid_direction(store: Store) -> None:
    with pytest.raises(ValueError, match="direction"):
        store.add_message(make_message(100.0, direction="sideways"))


def test_query_history_filters_and_pagination(store: Store) -> None:
    store.add_message(make_message(100.0, channel_id="c1", subject_id="s1", direction="in"))
    store.add_message(make_message(200.0, channel_id="c1", subject_id="s1", direction="out"))
    store.add_message(make_message(300.0, channel_id="c1", subject_id="s2", direction="in"))
    store.add_message(make_message(400.0, channel_id="c2", subject_id="s1", direction="in"))
    store.add_message(make_message(500.0, channel_id="c2", subject_id="s2", direction="out"))

    all_rows = store.query_history()
    assert [row.ts for row in all_rows] == [500.0, 400.0, 300.0, 200.0, 100.0]

    assert [row.ts for row in store.query_history(channel_id="c1")] == [300.0, 200.0, 100.0]
    assert [row.ts for row in store.query_history(subject_id="s1")] == [400.0, 200.0, 100.0]
    assert [row.ts for row in store.query_history(channel_id="c1", subject_id="s1")] == [200.0, 100.0]

    bounded = store.query_history(since=200.0, until=400.0)  # inclusive bounds
    assert [row.ts for row in bounded] == [400.0, 300.0, 200.0]

    assert [row.ts for row in store.query_history(limit=2)] == [500.0, 400.0]
    assert [row.ts for row in store.query_history(limit=2, offset=2)] == [300.0, 200.0]
    assert [row.ts for row in store.query_history(offset=4)] == [100.0]
    assert store.query_history(limit=0) == []
    assert store.query_history(channel_id="missing") == []

    record = store.query_history(channel_id="c1", subject_id="s1", since=200.0, limit=1)[0]
    assert record == make_message(
        200.0,
        channel_id="c1",
        subject_id="s1",
        direction="out",
        session_key="s1",
    )


# ---------------------------------------------------------------------------
# maintenance: prune / vacuum / backup / counts
# ---------------------------------------------------------------------------


def _seed_retention_data(store: Store, db_path: Path) -> None:
    """Two messages/turn-events/dedup/audit rows: one stale, one fresh."""
    store.add_message(make_message(NOW - 40 * DAY, direction="in"))
    store.add_message(make_message(NOW - 20 * DAY, direction="out"))
    insert_turn_event(db_path, "old-turn", 1, NOW - 2 * DAY)
    insert_turn_event(db_path, "fresh-turn", 1, NOW - 0.5 * DAY)
    with raw_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO inbound_dedup(idempotency_key, channel_id, created_at) VALUES (?, ?, ?)",
            ("stale-key", "c1", NOW - 8 * DAY),
        )
        conn.execute(
            "INSERT INTO inbound_dedup(idempotency_key, channel_id, created_at) VALUES (?, ?, ?)",
            ("fresh-key", "c1", NOW - 1 * DAY),
        )
        conn.execute(
            "INSERT INTO audit(ts, actor, action, payload_json) VALUES (?, ?, ?, ?)",
            (NOW - 91 * DAY, "system", "stale.action", "{}"),
        )
        conn.execute(
            "INSERT INTO audit(ts, actor, action, payload_json) VALUES (?, ?, ?, ?)",
            (NOW - 1 * DAY, "system", "fresh.action", "{}"),
        )


def test_prune_deletes_by_retention_days(store: Store, db_path: Path) -> None:
    _seed_retention_data(store, db_path)

    counts = store.prune(
        {
            "chat_days": 30,
            "turn_events_days": 1,
            "dedup_days": 7,
            "audit_days": 90,
        },
        now=NOW,
    )
    assert counts == {"messages": 1, "turn_events": 1, "inbound_dedup": 1, "audit": 1}
    assert [row.ts for row in store.query_history()] == [NOW - 20 * DAY]
    assert [entry["action"] for entry in store.list_audit()] == ["fresh.action"]
    table_counts = store.table_counts()
    assert table_counts["turn_events"] == 1
    assert table_counts["inbound_dedup"] == 1


def test_prune_chat_max_rows_keeps_newest(store: Store) -> None:
    for step in range(1, 6):
        store.add_message(make_message(float(step)))

    counts = store.prune({"chat_max_rows": 3}, now=NOW)
    assert counts == {"messages": 2, "turn_events": 0, "inbound_dedup": 0, "audit": 0}
    assert [row.ts for row in store.query_history()] == [5.0, 4.0, 3.0]


def test_prune_skips_missing_knobs(store: Store) -> None:
    store.add_message(make_message(1.0))
    counts = store.prune({}, now=NOW)
    assert counts == {"messages": 0, "turn_events": 0, "inbound_dedup": 0, "audit": 0}
    assert len(store.query_history()) == 1


def test_prune_batches_large_message_tables(store: Store, db_path: Path) -> None:
    rows = [
        ("c1", "s1", "s1", "in", None, "inbound", "[]", "{}", None, NOW - 40 * DAY, None)
        for _ in range(5_003)
    ]
    with raw_connection(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO messages(
              channel_id, subject_id, session_key, direction, turn_id, kind,
              content_json, metadata_json, platform_msg_id, ts, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    counts = store.prune({"chat_days": 30}, now=NOW)
    assert counts["messages"] == 5_003
    assert store.table_counts()["messages"] == 0


def test_vacuum_is_safe(store: Store) -> None:
    store.vacuum()  # must not raise, also on an empty database


def test_backup_creates_openable_snapshot(store: Store, db_path: Path) -> None:
    store.add_channel(make_channel())
    dest = db_path.parent / "nested" / "backup.db"

    store.backup(dest)

    assert dest.exists()
    assert dest.stat().st_size > 0
    with raw_connection(dest) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1

    with pytest.raises(ValueError, match="already exists"):
        store.backup(dest)


def test_table_counts_reports_every_table(store: Store, db_path: Path) -> None:
    store.set_setting("log_level", "INFO")
    store.add_channel(make_channel())
    store.upsert_backend(
        BackendRecord(
            tenant_id="",
            base_url="http://127.0.0.1:9000",
            token_ref="${TOKEN}",
            timeout_ms=1_000,
            enabled=True,
            created_at=1.0,
            updated_at=1.0,
        )
    )
    store.upsert_subject(make_subject())
    store.claim_inbound("key", "c1")
    store.add_audit("channel.add", actor="offline-cli")
    store.add_message(make_message(1.0))
    insert_turn_event(db_path, "turn-1", 1, 1.0)

    counts = store.table_counts()
    assert counts == {
        "schema_migrations": 1,
        "settings": 1,
        "channels": 1,
        "backends": 1,
        "subjects": 1,
        "inbound_dedup": 1,
        "audit": 1,
        "messages": 1,
        "turn_events": 1,
    }
