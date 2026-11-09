"""Extra unit tests for ``gateway/runtime.py`` and ``gateway/app.py``.

Everything is offline: the upstream ``ChannelManager`` is replaced with
:class:`StubManager`, no channel SDK is imported for a live connection, and
``TestClient`` drives the ASGI app in-process.
"""

from __future__ import annotations

import json
import time
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from octop_gateway.constraints import ChannelConstraints
from octop_gateway.models import ChannelSubject

from im_gateway.core.config import (
    MASKED_SECRET,
    ChannelConfigEntry,
    ConfigDocument,
)
from im_gateway.db.models import ChannelRecord, MessageRecord, SubjectRecord
from im_gateway.db.store import Store
from im_gateway.gateway.app import (
    _error_response,
    _mask_secrets,
    _parse_listen_address,
    create_app,
)
from im_gateway.gateway.runtime import GatewayRuntime

TOKEN = "extra-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CHANNEL_ID = "feishu:acme:sales"


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeProcessor:
    active_turns: int = 0


@dataclass
class StubManager:
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)
    subjects: dict[str, list[ChannelSubject]] = field(default_factory=dict)
    fail_add_kinds: set[str] = field(default_factory=set)
    fail_remove: bool = False
    fail_stop: bool = False
    fail_list_subjects: bool = False
    fail_push_subjects: set[str] = field(default_factory=set)
    started: int = 0
    stopped: int = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1
        if self.fail_stop:
            raise RuntimeError("stub manager stop failed")

    async def add_channel(
        self,
        *,
        channel_type: str,
        config: Any,
        channel_id: str,
        tenant_id: str | None,
        constraints: ChannelConstraints | None = None,
    ) -> str:
        if channel_type in self.fail_add_kinds:
            raise ValueError(f"stub refused to add a {channel_type} channel")
        self.added.append(
            {
                "channel_type": channel_type,
                "config": config,
                "channel_id": channel_id,
                "tenant_id": tenant_id,
                "constraints": constraints,
            }
        )
        return channel_id

    async def remove_channel(self, channel_id: str) -> None:
        self.removed.append(channel_id)
        if self.fail_remove:
            raise RuntimeError("stub remove failed")

    async def probe_channel(
        self,
        channel_type: str,
        config: Any,
        *,
        tenant_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        self.probes.append({"channel_type": channel_type, "config": config})

    async def push_text(self, channel_id: str, subject: ChannelSubject, text: str) -> None:
        if subject.subject_id in self.fail_push_subjects:
            raise RuntimeError("stub push failed")
        self.pushed.append((channel_id, subject.subject_id, text))

    def list_subjects(self, channel_id: str) -> list[ChannelSubject]:
        if self.fail_list_subjects:
            raise RuntimeError("stub subject registry unavailable")
        return list(self.subjects.get(channel_id, []))


def make_store(db_path: Path) -> Store:
    store = Store(db_path)
    store.migrate()
    return store


def make_record(
    channel_id: str = CHANNEL_ID,
    *,
    kind: str = "feishu",
    alias: str | None = "sales",
    enabled: bool = True,
    config: dict[str, Any] | None = None,
    tenant_id: str | None = "acme",
) -> ChannelRecord:
    now = 1_700_000_000.0
    return ChannelRecord(
        channel_id=channel_id,
        kind=kind,
        tenant_id=tenant_id,
        alias=alias,
        name="Sales",
        config=config if config is not None else {"app_id": "cli_x"},
        constraints={},
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def make_entry(
    channel_id: str = CHANNEL_ID,
    *,
    kind: str = "feishu",
    alias: str | None = "sales",
    enabled: bool = True,
    config: dict[str, Any] | None = None,
) -> ChannelConfigEntry:
    return ChannelConfigEntry(
        channel_id=channel_id,
        kind=kind,
        tenant_id="acme",
        alias=alias,
        name="Sales",
        enabled=enabled,
        config=config if config is not None else {"app_id": "cli_x"},
    )


def initial_document() -> dict[str, Any]:
    return {
        "version": 1,
        "channels": [
            {
                "channel_id": CHANNEL_ID,
                "kind": "feishu",
                "tenant_id": "acme",
                "alias": "sales",
                "name": "Sales",
                "enabled": True,
                "config": {
                    "app_id": "cli_x",
                    "app_secret": "literal-secret",
                    "nested": {"token": "literal-inner-token"},
                },
                "constraints": {},
            }
        ],
        "backends": [
            {
                "tenant_id": "acme",
                "base_url": "http://127.0.0.1:9000",
                "token_ref": "literal-backend-token",
                "timeout_ms": 5000,
            }
        ],
        "settings": {"workers_per_channel": 2},
    }


@asynccontextmanager
async def running_runtime(
    db_path: Path,
    manager: StubManager | None = None,
    processor: FakeProcessor | None = None,
    media_dir: Path | None = None,
) -> Any:
    store = make_store(db_path)
    stub = manager if manager is not None else StubManager()
    runtime = GatewayRuntime(
        store,
        token=TOKEN,
        manager=stub,
        processor=processor if processor is not None else FakeProcessor(),
        media_dir=media_dir,
    )
    await runtime.start()
    try:
        yield runtime, stub
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# Runtime: introspection / manager construction
# ---------------------------------------------------------------------------


async def test_runtime_properties_before_start(db_path: Path, tmp_path: Path) -> None:
    store = make_store(db_path)
    runtime = GatewayRuntime(store, token=TOKEN, media_dir=tmp_path / "media")
    assert runtime.manager is None
    assert runtime.processor is None
    assert runtime.media_dir_path == tmp_path / "media"
    assert runtime.uptime_s == 0.0
    assert runtime.channel_state("unknown") == ("disabled", None)
    assert runtime.db_path.endswith("runtime.db")
    with pytest.raises(RuntimeError, match="not started"):
        await runtime.add_channel(make_entry())


async def test_runtime_builds_manager_from_settings(db_path: Path) -> None:
    store = make_store(db_path)
    runtime = GatewayRuntime(store, token=TOKEN, media_dir=Path("media-root"))

    # Invalid knobs fall back to the defaults (bool/str/negative are rejected).
    store.set_setting("workers_per_channel", "not-an-int")
    store.set_setting("queue_maxsize", -5)
    manager = runtime._build_manager()
    assert manager._workers_per_channel == 4
    assert manager._queue_maxsize == 1000
    assert manager._media_backend is not None

    store.set_setting("workers_per_channel", 2)
    store.set_setting("queue_maxsize", 50)
    tuned = runtime._build_manager()
    assert tuned._workers_per_channel == 2
    assert tuned._queue_maxsize == 50


# ---------------------------------------------------------------------------
# Runtime: startup registration isolation
# ---------------------------------------------------------------------------


async def test_startup_isolates_channel_failures_and_skips_disabled(db_path: Path) -> None:
    store = make_store(db_path)
    store.add_channel(make_record(CHANNEL_ID))
    store.add_channel(make_record("wecom:acme:ops", kind="wecom", alias="ops"))
    store.add_channel(make_record("dingtalk:acme:off", kind="dingtalk", alias="off", enabled=False))

    manager = StubManager()
    manager.fail_add_kinds.add("wecom")
    runtime = GatewayRuntime(store, token=TOKEN, manager=manager, processor=FakeProcessor())
    await runtime.start()

    assert manager.started == 1
    assert runtime.channel_state(CHANNEL_ID)[0] == "running"
    state, last_error = runtime.channel_state("wecom:acme:ops")
    assert state == "error"
    assert last_error and "wecom" in last_error
    assert runtime.channel_state("dingtalk:acme:off") == ("disabled", None)
    assert [entry["channel_id"] for entry in manager.added] == [CHANNEL_ID]
    assert store.get_channel(CHANNEL_ID) is not None
    assert store.get_channel(CHANNEL_ID).last_started_at is not None

    # Starting twice is a no-op.
    await runtime.start()
    assert manager.started == 1
    await runtime.stop()


async def test_stop_before_start_is_a_noop(db_path: Path) -> None:
    store = make_store(db_path)
    manager = StubManager()
    runtime = GatewayRuntime(store, token=TOKEN, manager=manager, processor=FakeProcessor())
    await runtime.stop()
    assert manager.stopped == 0


async def test_stop_survives_manager_stop_failure(db_path: Path) -> None:
    store = make_store(db_path)
    manager = StubManager()
    manager.fail_stop = True
    runtime = GatewayRuntime(store, token=TOKEN, manager=manager, processor=FakeProcessor())
    await runtime.start()
    await runtime.stop()  # must not raise
    assert manager.stopped == 1


async def test_wait_for_idle_returns_false_after_timeout(db_path: Path) -> None:
    store = make_store(db_path)
    runtime = GatewayRuntime(
        store, token=TOKEN, manager=StubManager(), processor=FakeProcessor(active_turns=1)
    )
    started = time.monotonic()
    assert await runtime.wait_for_idle_turns(0.01, poll_s=0.005) is False
    assert time.monotonic() - started < 0.3


# ---------------------------------------------------------------------------
# Runtime: channel update semantics
# ---------------------------------------------------------------------------


async def test_update_channel_validation_errors(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        await runtime.add_channel(make_entry())
        with pytest.raises(ValueError, match="不支持的通道字段"):
            await runtime.update_channel(CHANNEL_ID, {"bogus": 1})
        with pytest.raises(ValueError, match="没有要更新的字段"):
            await runtime.update_channel(CHANNEL_ID, {})
        with pytest.raises(ValueError, match="kind"):
            await runtime.update_channel(CHANNEL_ID, {"kind": 42})
        with pytest.raises(ValueError, match="config"):
            await runtime.update_channel(CHANNEL_ID, {"config": "not-a-dict"})
        with pytest.raises(ValueError, match="constraints"):
            await runtime.update_channel(CHANNEL_ID, {"constraints": []})
        with pytest.raises(ValueError, match="unknown channel kind"):
            await runtime.update_channel(CHANNEL_ID, {"kind": "mystery"})


async def test_update_alias_conflict_is_rejected(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        await runtime.add_channel(make_entry())
        await runtime.add_channel(make_entry("feishu:acme:other", alias="other"))
        with pytest.raises(ValueError, match="alias"):
            await runtime.update_channel(CHANNEL_ID, {"alias": "other"})


async def test_name_only_update_keeps_upstream_instance(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        assert len(manager.added) == 1
        await runtime.update_channel(CHANNEL_ID, {"name": "Renamed", "alias": "sales-2"})
        assert len(manager.added) == 1
        assert manager.removed == []
        stored = runtime.store.get_channel(CHANNEL_ID)
        assert stored is not None
        assert stored.name == "Renamed"
        assert stored.alias == "sales-2"


async def test_config_update_restarts_upstream_instance(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        await runtime.update_channel(CHANNEL_ID, {"config": {"app_id": "cli_new"}})
        assert manager.removed == [CHANNEL_ID]
        assert len(manager.added) == 2
        assert manager.added[-1]["config"] == {"app_id": "cli_new"}


async def test_update_rolls_back_when_db_write_fails(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        real_update = runtime.store.update_channel

        def failing_update(channel_id: str, **fields: Any) -> Any:
            raise ValueError("db write failed")

        monkeypatch.setattr(runtime.store, "update_channel", failing_update)
        with pytest.raises(ValueError, match="db write failed"):
            await runtime.update_channel(CHANNEL_ID, {"config": {"app_id": "cli_new"}})
        # Old registration was restored after the DB rejected the change.
        assert manager.removed == [CHANNEL_ID]
        assert len(manager.added) == 2
        monkeypatch.setattr(runtime.store, "update_channel", real_update)
        stored = runtime.store.get_channel(CHANNEL_ID)
        assert stored is not None
        assert stored.config == {"app_id": "cli_x"}


async def test_update_reregister_failure_rolls_back_config(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        original_add = manager.add_channel

        async def failing_add(**kwargs: Any) -> str:
            raise ValueError("re-register failed")

        manager.add_channel = failing_add  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="重新注册失败"):
            await runtime.update_channel(CHANNEL_ID, {"config": {"app_id": "cli_new"}})
        # Both the re-register and the rollback re-register failed -> state=error,
        # but the durable record was restored before the rollback attempt.
        assert runtime.channel_state(CHANNEL_ID)[0] == "error"
        stored = runtime.store.get_channel(CHANNEL_ID)
        assert stored is not None
        assert stored.config == {"app_id": "cli_x"}
        manager.add_channel = original_add  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Runtime: remove / push / subjects
# ---------------------------------------------------------------------------


async def test_remove_unknown_ref_raises_key_error(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        with pytest.raises(KeyError):
            await runtime.remove_channel("does-not-exist")


async def test_remove_purge_cascades_subjects_and_messages(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        now = time.time()
        runtime.store.upsert_subject(
            SubjectRecord(
                channel_id=CHANNEL_ID,
                subject_id="user-1",
                chat_type="direct",
                display_name="User One",
                metadata={},
                first_seen=now,
                last_seen=now,
            )
        )
        runtime.store.add_message(
            MessageRecord(
                channel_id=CHANNEL_ID,
                subject_id="user-1",
                session_key="k",
                direction="out",
                kind="message",
                content=[{"type": "text", "text": "hi"}],
                metadata={},
                platform_msg_id=None,
                ts=now,
                error=None,
            )
        )
        await runtime.remove_channel(CHANNEL_ID, purge=True)
        assert manager.removed == [CHANNEL_ID]
        assert runtime.store.get_channel(CHANNEL_ID) is None
        assert runtime.store.list_subjects(CHANNEL_ID) == []
        assert runtime.store.query_history(channel_id=CHANNEL_ID) == []
        assert runtime.channel_state(CHANNEL_ID) == ("disabled", None)


async def test_remove_returns_key_error_when_row_vanishes(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        await runtime.add_channel(make_entry())
        monkeypatch.setattr(runtime.store, "delete_channel", lambda *a, **k: False)
        with pytest.raises(KeyError):
            await runtime.remove_channel(CHANNEL_ID)


async def test_remove_survives_upstream_remove_failure(db_path: Path) -> None:
    manager = StubManager()
    manager.fail_remove = True
    async with running_runtime(db_path, manager=manager) as (runtime, _manager):
        await runtime.add_channel(make_entry())
        await runtime.remove_channel(CHANNEL_ID)
        assert runtime.store.get_channel(CHANNEL_ID) is None


async def test_push_rejects_disabled_and_unregistered_channels(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        await runtime.add_channel(make_entry(enabled=False))
        with pytest.raises(ValueError, match="disabled"):
            await runtime.push_text(CHANNEL_ID, "user-1", "hi")

        await runtime.add_channel(make_entry("feishu:acme:second", alias="second"))
        runtime._registered.discard("feishu:acme:second")
        with pytest.raises(ValueError, match="not running"):
            await runtime.push_text("feishu:acme:second", "user-1", "hi")


async def test_push_all_without_subjects_is_zero(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        await runtime.add_channel(make_entry())
        assert await runtime.push_all(CHANNEL_ID, "hello") == (0, 0)


async def test_push_all_lists_subjects_failure_degrades_to_zero(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        await runtime.add_channel(make_entry())

        def broken(_ref: str) -> list[dict[str, Any]]:
            raise RuntimeError("subject listing failed")

        monkeypatch.setattr(runtime, "list_subjects", broken)
        assert await runtime.push_all(CHANNEL_ID, "hello") == (0, 0)


async def test_push_all_skips_blank_and_duplicate_subject_ids(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        monkeypatch.setattr(
            runtime,
            "list_subjects",
            lambda ref: [
                {"subject_id": ""},
                {"subject_id": "user-1"},
                {"subject_id": "user-1"},
            ],
        )
        assert await runtime.push_all(CHANNEL_ID, "hi") == (1, 0)
        assert manager.pushed == [(CHANNEL_ID, "user-1", "hi")]


async def test_push_all_merges_durable_and_memory_subjects(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry())
        now = time.time()
        runtime.store.upsert_subject(
            SubjectRecord(
                channel_id=CHANNEL_ID,
                subject_id="durable-user",
                chat_type="direct",
                display_name="Durable",
                metadata={"route": "durable"},
                first_seen=now,
                last_seen=now,
            )
        )
        manager.subjects[CHANNEL_ID] = [
            ChannelSubject(subject_id="memory-user", chat_type="group", display_name="Memory"),
            ChannelSubject(subject_id="durable-user", display_name="Live Override"),
        ]

        merged = {item["subject_id"]: item for item in runtime.list_subjects(CHANNEL_ID)}
        assert set(merged) == {"durable-user", "memory-user"}
        assert merged["durable-user"]["display_name"] == "Live Override"
        assert merged["durable-user"]["metadata"] == {"route": "durable"}
        assert merged["memory-user"]["chat_type"] == "group"

        sent, failed = await runtime.push_all(CHANNEL_ID, "broadcast")
        assert (sent, failed) == (2, 0)
        assert {subject_id for _, subject_id, _ in manager.pushed} == {
            "durable-user",
            "memory-user",
        }


async def test_list_subjects_survives_memory_registry_failure(db_path: Path) -> None:
    manager = StubManager()
    manager.fail_list_subjects = True
    async with running_runtime(db_path, manager=manager) as (runtime, _manager):
        await runtime.add_channel(make_entry())
        now = time.time()
        runtime.store.upsert_subject(
            SubjectRecord(
                channel_id=CHANNEL_ID,
                subject_id="durable-user",
                chat_type="direct",
                display_name="Durable",
                metadata={},
                first_seen=now,
                last_seen=now,
            )
        )
        subjects = runtime.list_subjects(CHANNEL_ID)
        assert [item["subject_id"] for item in subjects] == ["durable-user"]


# ---------------------------------------------------------------------------
# Runtime: apply_config
# ---------------------------------------------------------------------------


async def test_apply_config_masks_delete_and_settings(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        applied = await runtime.apply_config(ConfigDocument.model_validate(initial_document()))
        assert applied == {"channels": 1, "backends": 1, "settings": 1}
        assert len(manager.added) == 1

        masked = {
            "version": 1,
            "channels": [
                {
                    "channel_id": CHANNEL_ID,
                    "kind": "feishu",
                    "tenant_id": "acme",
                    "alias": "sales",
                    "name": "Renamed",
                    "enabled": True,
                    "config": {
                        "app_id": "cli_x",
                        "app_secret": MASKED_SECRET,
                        "nested": {"token": MASKED_SECRET},
                    },
                    "constraints": {},
                }
            ],
            "backends": [
                {
                    "tenant_id": "acme",
                    "base_url": "http://127.0.0.1:9000",
                    "token_ref": MASKED_SECRET,
                    "timeout_ms": 5000,
                }
            ],
            "settings": {"workers_per_channel": MASKED_SECRET},
        }
        again = await runtime.apply_config(ConfigDocument.model_validate(masked))
        assert again == {"channels": 1, "backends": 1, "settings": 0}
        stored = runtime.store.get_channel(CHANNEL_ID)
        assert stored is not None
        assert stored.name == "Renamed"
        assert stored.config["app_secret"] == "literal-secret"
        assert stored.config["nested"]["token"] == "literal-inner-token"
        backend = runtime.store.get_backend("acme")
        assert backend is not None and backend.token_ref == "literal-backend-token"
        assert runtime.store.get_setting("workers_per_channel") == 2
        # Alias/name-only import does not restart the live instance.
        assert len(manager.added) == 1

        deleted = await runtime.apply_config(
            ConfigDocument.model_validate({"version": 1, "channels": [], "backends": [], "settings": {}})
        )
        assert deleted == {"channels": 0, "backends": 0, "settings": 0}
        assert runtime.store.get_channel(CHANNEL_ID) is None
        assert runtime.store.get_backend("acme") is None
        assert manager.removed == [CHANNEL_ID]


async def test_apply_config_add_failure_becomes_warning(db_path: Path) -> None:
    manager = StubManager()
    manager.fail_add_kinds.add("wecom")
    async with running_runtime(db_path, manager=manager) as (runtime, _manager):
        document = {
            "version": 1,
            "channels": [
                {
                    "channel_id": "wecom:acme:ops",
                    "kind": "wecom",
                    "tenant_id": "acme",
                    "enabled": True,
                    "config": {"bot_id": "b"},
                    "constraints": {},
                }
            ],
            "backends": [
                {
                    "tenant_id": "acme",
                    "base_url": "http://127.0.0.1:9000",
                    "token_ref": "${WECOM_TOKEN}",
                    "timeout_ms": 1000,
                }
            ],
            "settings": {},
        }
        applied = await runtime.apply_config(ConfigDocument.model_validate(document))
        assert applied["channels"] == 1
        assert len(runtime.last_config_warnings) == 1
        assert "wecom:acme:ops" in runtime.last_config_warnings[0]
        assert runtime.channel_state("wecom:acme:ops")[0] == "error"
        assert runtime.store.get_channel("wecom:acme:ops") is not None


async def test_apply_config_adds_disabled_channel(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        document = {
            "version": 1,
            "channels": [
                {
                    "channel_id": "feishu:acme:off",
                    "kind": "feishu",
                    "tenant_id": None,
                    "enabled": False,
                    "config": {"app_id": "x"},
                    "constraints": {},
                }
            ],
            "backends": [],
            "settings": {},
        }
        await runtime.apply_config(ConfigDocument.model_validate(document))
        assert runtime.channel_state("feishu:acme:off") == ("disabled", None)
        assert manager.added == []


async def test_apply_config_recovers_unregistered_channel(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        document = ConfigDocument.model_validate(initial_document())
        await runtime.apply_config(document)
        assert len(manager.added) == 1

        runtime._registered.discard(CHANNEL_ID)
        await runtime.apply_config(document)
        assert len(manager.added) == 2


async def test_apply_config_rejects_masked_new_backend_and_empty_fields(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        masked_new_backend = {
            "version": 1,
            "channels": [],
            "backends": [{"tenant_id": "new", "base_url": "http://x", "token_ref": MASKED_SECRET}],
            "settings": {},
        }
        with pytest.raises(ValueError, match="打码"):
            await runtime.apply_config(ConfigDocument.model_validate(masked_new_backend))

        empty_token = {
            "version": 1,
            "channels": [],
            "backends": [{"tenant_id": "t", "base_url": "http://x", "token_ref": "  "}],
            "settings": {},
        }
        with pytest.raises(ValueError, match="token_ref"):
            await runtime.apply_config(ConfigDocument.model_validate(empty_token))

        empty_url = {
            "version": 1,
            "channels": [],
            "backends": [{"tenant_id": "t", "base_url": "  ", "token_ref": "${T}"}],
            "settings": {},
        }
        with pytest.raises(ValueError, match="base_url"):
            await runtime.apply_config(ConfigDocument.model_validate(empty_url))
        assert runtime.store.list_backends() == []


async def test_apply_config_rejects_non_dict_config_and_bad_settings(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        bad_config = ConfigDocument.model_construct(
            version=1,
            channels=[
                ChannelConfigEntry.model_construct(
                    channel_id="c1",
                    kind="feishu",
                    tenant_id=None,
                    alias=None,
                    name="",
                    enabled=False,
                    config="not-a-dict",
                    constraints={},
                )
            ],
            backends=[],
            settings={},
        )
        with pytest.raises(ValueError, match="必须是 JSON 对象"):
            await runtime.apply_config(bad_config)

        bad_settings = ConfigDocument.model_construct(
            version=1, channels=[], backends=[], settings={"bad": object()}
        )
        with pytest.raises(ValueError, match="可序列化"):
            await runtime.apply_config(bad_settings)


async def test_audit_failure_does_not_break_operations(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_runtime(db_path) as (runtime, _manager):

        def broken_audit(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(runtime.store, "add_audit", broken_audit)
        runtime._audit("channel.add", CHANNEL_ID, {"kind": "feishu"})  # must not raise


async def test_apply_config_resolves_masked_lists(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        base = initial_document()
        base["channels"][0]["config"]["names"] = ["first", "second"]
        await runtime.apply_config(ConfigDocument.model_validate(base))

        same_length = initial_document()
        same_length["channels"][0]["config"] = {
            "app_id": "cli_x",
            "app_secret": MASKED_SECRET,
            "nested": {"token": MASKED_SECRET},
            "names": [MASKED_SECRET, MASKED_SECRET],
        }
        await runtime.apply_config(ConfigDocument.model_validate(same_length))
        stored = runtime.store.get_channel(CHANNEL_ID)
        assert stored is not None
        assert stored.config["names"] == ["first", "second"]

        mismatched = initial_document()
        mismatched["channels"][0]["config"] = {
            "app_id": "cli_x",
            "app_secret": "literal-secret",
            "nested": {"token": "literal-inner-token"},
            "names": [MASKED_SECRET],
        }
        with pytest.raises(ValueError, match="打码"):
            await runtime.apply_config(ConfigDocument.model_validate(mismatched))


async def test_apply_config_alias_conflict_is_rejected(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, _manager):
        duplicate = initial_document()
        duplicate["channels"][0]["alias"] = "dup"
        await runtime.apply_config(ConfigDocument.model_validate(duplicate))

        first = ChannelConfigEntry(
            channel_id=CHANNEL_ID,
            kind="feishu",
            tenant_id="acme",
            alias="dup",
            name="A",
            enabled=True,
            config={"app_id": "cli_x"},
            constraints={},
        )
        second = ChannelConfigEntry(
            channel_id="feishu:acme:other",
            kind="feishu",
            tenant_id="acme",
            alias="dup",
            name="B",
            enabled=True,
            config={"app_id": "cli_x"},
            constraints={},
        )
        document = ConfigDocument.model_construct(
            version=1, channels=[first, second], backends=[], settings={}
        )
        with pytest.raises(ValueError, match="已被通道"):
            await runtime.apply_config(document)


async def test_apply_config_structural_change_restarts_or_disables(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.apply_config(ConfigDocument.model_validate(initial_document()))
        assert len(manager.added) == 1

        changed = initial_document()
        changed["channels"][0]["config"] = {
            "app_id": "cli_new",
            "app_secret": "literal-secret",
            "nested": {"token": "literal-inner-token"},
        }
        await runtime.apply_config(ConfigDocument.model_validate(changed))
        assert manager.removed == [CHANNEL_ID]
        assert len(manager.added) == 2
        assert manager.added[-1]["config"]["app_id"] == "cli_new"

        disabled = initial_document()
        disabled["channels"][0]["config"] = changed["channels"][0]["config"]
        disabled["channels"][0]["enabled"] = False
        await runtime.apply_config(ConfigDocument.model_validate(disabled))
        assert runtime.channel_state(CHANNEL_ID)[0] == "disabled"


async def test_start_without_injected_manager_builds_real_one(db_path: Path) -> None:
    store = make_store(db_path)
    runtime = GatewayRuntime(store, token=TOKEN, processor=FakeProcessor())
    await runtime.start()
    assert runtime.manager is not None
    assert runtime.manager.channel_ids == []
    await runtime.stop()


async def test_update_re_enable_failure_rolls_back_to_disabled(db_path: Path) -> None:
    async with running_runtime(db_path) as (runtime, manager):
        await runtime.add_channel(make_entry(enabled=False))

        async def failing_add(**kwargs: Any) -> str:
            raise ValueError("upstream down")

        manager.add_channel = failing_add  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="重新注册失败"):
            await runtime.update_channel(CHANNEL_ID, {"enabled": True, "config": {"app_id": "cli_new"}})
        assert runtime.channel_state(CHANNEL_ID)[0] == "disabled"
        stored = runtime.store.get_channel(CHANNEL_ID)
        assert stored is not None
        assert stored.enabled is False


# ---------------------------------------------------------------------------
# App: helpers
# ---------------------------------------------------------------------------


def _request(path: str = "/v1/test") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )


def test_error_response_maps_domain_exceptions() -> None:
    key_error = _error_response(_request(), KeyError("missing-channel"))
    assert key_error.status_code == 404
    assert json.loads(bytes(key_error.body)) == {"detail": "未找到: missing-channel"}

    value_error = _error_response(_request(), ValueError("bad input"))
    assert value_error.status_code == 400
    assert json.loads(bytes(value_error.body))["detail"] == "bad input"

    empty_value_error = _error_response(_request(), ValueError())
    assert json.loads(bytes(empty_value_error.body))["detail"] == "请求无效"

    grouped = _error_response(
        _request(), ExceptionGroup("g", [ExceptionGroup("inner", [KeyError("nested")])])
    )
    assert grouped.status_code == 404
    assert "nested" in json.loads(bytes(grouped.body))["detail"]

    unexpected = _error_response(_request(), RuntimeError("boom"))
    assert unexpected.status_code == 500
    assert json.loads(bytes(unexpected.body)) == {"detail": "内部错误"}


def test_mask_secrets_recurses_and_keeps_env_references() -> None:
    document = {
        "channels": [
            {
                "config": {
                    "app_secret": "literal",
                    "verification_token": "literal-token",
                    "env_token": "${FEISHU_TOKEN}",
                    "password": "hunter2",
                    "plain": "visible",
                },
                "nested": [{"api_key": "k", "list": ["private_key_value"]}],
            }
        ],
        "backends": [{"token_ref": "${BACKEND_TOKEN}", "base_url": "http://x"}],
        "settings": {"token": "abc", "plain_setting": 1},
        "non_dict": None,
    }
    masked = _mask_secrets(document)
    config = masked["channels"][0]["config"]
    assert config["app_secret"] == MASKED_SECRET
    assert config["verification_token"] == MASKED_SECRET
    assert config["env_token"] == "${FEISHU_TOKEN}"
    assert config["password"] == MASKED_SECRET
    assert config["plain"] == "visible"
    assert masked["channels"][0]["nested"][0]["api_key"] == MASKED_SECRET
    # list elements are passed through recursively; a bare string in a list has
    # no key to match, so it is preserved.
    assert masked["channels"][0]["nested"][0]["list"] == ["private_key_value"]
    assert masked["backends"][0]["token_ref"] == "${BACKEND_TOKEN}"
    assert masked["settings"]["token"] == MASKED_SECRET
    assert masked["non_dict"] is None


def test_parse_listen_address_validation() -> None:
    with pytest.raises(ValueError, match="socket path"):
        _parse_listen_address(listen="unix://", host=None, port=None)
    with pytest.raises(ValueError, match="port is not an integer"):
        _parse_listen_address(listen="tcp://127.0.0.1:not-a-port", host=None, port=None)
    with pytest.raises(ValueError, match="port out of range"):
        _parse_listen_address(listen="tcp://127.0.0.1:70000", host=None, port=None)
    with pytest.raises(ValueError, match="expected tcp"):
        _parse_listen_address(listen="tcp://missing-port", host=None, port=None)

    tcp = _parse_listen_address(listen=None, host="0.0.0.0", port=1234)
    assert tcp.host == "0.0.0.0"
    assert tcp.port == 1234
    defaults = _parse_listen_address(listen=None, host=None, port=None)
    assert defaults.uds is not None or defaults.port is not None


def test_parse_listen_address_uses_paths_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from im_gateway.gateway import app as app_module

    monkeypatch.setattr(app_module.paths, "default_listen", lambda: "tcp://127.0.0.1:8765")
    address = _parse_listen_address(listen=None, host=None, port=None)
    assert (address.host, address.port) == ("127.0.0.1", 8765)


# ---------------------------------------------------------------------------
# App: HTTP branches
# ---------------------------------------------------------------------------


@dataclass
class AppHarness:
    client: TestClient
    app: Any
    store: Store
    manager: StubManager

    def add_channel(self) -> dict[str, Any]:
        response = self.client.post(
            "/v1/channels",
            json={
                "channel_id": CHANNEL_ID,
                "kind": "feishu",
                "tenant_id": "acme",
                "alias": "sales",
                "name": "Sales",
                "enabled": True,
                "config": {"app_id": "cli_x"},
                "constraints": {},
            },
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture()
def app_harness(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IMGW_DATA_DIR", str(tmp_path / "data"))
    store = Store(db_path)
    manager = StubManager()
    app = create_app(store=store, token=TOKEN, manager=manager)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with TestClient(app) as client:
            yield AppHarness(client=client, app=app, store=store, manager=manager)


def test_app_exposes_login_manager_and_scheduler(app_harness: AppHarness) -> None:
    assert app_harness.app.state.login_manager is not None
    assert app_harness.app.state.scheduler is not None
    assert app_harness.app.state.token == TOKEN
    assert app_harness.client.get("/healthz").status_code == 200


def test_app_readyz_reports_db_failure(app_harness: AppHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_counts() -> dict[str, int]:
        raise RuntimeError("sqlite is gone")

    monkeypatch.setattr(app_harness.store, "table_counts", broken_counts)
    response = app_harness.client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"detail": "数据库不可用"}


def test_app_status_maps_unexpected_failure_to_json_500(
    app_harness: AppHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_counts() -> dict[str, int]:
        raise RuntimeError("sqlite is gone")

    monkeypatch.setattr(app_harness.store, "table_counts", broken_counts)
    response = app_harness.client.get("/v1/status", headers=AUTH)
    assert response.status_code == 500
    assert response.json() == {"detail": "内部错误"}


def test_app_history_clamps_pagination_and_rejects_unknown_alias(
    app_harness: AppHarness,
) -> None:
    app_harness.add_channel()
    response = app_harness.client.get(
        "/v1/history",
        params={"channel_id": "sales", "limit": 99_999, "offset": -10},
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json() == {"messages": []}

    missing = app_harness.client.get("/v1/history", params={"channel_id": "nope"}, headers=AUTH)
    assert missing.status_code == 404
    assert isinstance(missing.json()["detail"], str)


def test_app_events_resolves_alias_and_streams_matching_events(
    app_harness: AppHarness,
) -> None:
    app_harness.add_channel()
    with app_harness.client.stream(
        "GET", "/v1/events", params={"channel_id": "sales"}, headers=AUTH
    ) as response:
        assert response.status_code == 200
        lines = response.iter_lines()
        assert next(lines) == ": connected"

        app_harness.app.state.runtime.events.publish(
            {"type": "channel.state", "channel_id": CHANNEL_ID, "state": "running"}
        )
        app_harness.app.state.runtime.events.publish(
            {"type": "channel.state", "channel_id": "other", "state": "running"}
        )
        data_lines = []
        for line in lines:
            if line.startswith("data:"):
                data_lines.append(json.loads(line[len("data:") :].strip()))
                break
        assert data_lines == [{"type": "channel.state", "channel_id": CHANNEL_ID, "state": "running"}]
        response.close()


def test_app_config_import_invalid_document_keeps_db(app_harness: AppHarness) -> None:
    app_harness.add_channel()
    before = app_harness.store.list_channels()
    response = app_harness.client.post(
        "/v1/config/import",
        json={
            "version": 1,
            "channels": [{"channel_id": "bad", "kind": "not-a-kind"}],
            "backends": [{"tenant_id": "", "base_url": "http://x", "token_ref": "${T}"}],
        },
        headers=AUTH,
    )
    assert response.status_code == 400
    assert app_harness.store.list_channels() == before


def test_app_maintenance_unknown_action_is_400(app_harness: AppHarness) -> None:
    response = app_harness.client.post("/v1/db/maintenance", json={"action": "explode"}, headers=AUTH)
    assert response.status_code == 400
    assert "不支持的维护操作" in response.json()["detail"]


def test_app_channel_duplicate_and_missing(app_harness: AppHarness) -> None:
    app_harness.add_channel()
    duplicate = app_harness.client.post(
        "/v1/channels",
        json={
            "channel_id": CHANNEL_ID,
            "kind": "feishu",
            "config": {"app_id": "cli_x"},
        },
        headers=AUTH,
    )
    assert duplicate.status_code == 400
    assert app_harness.client.get("/v1/channels/nope", headers=AUTH).status_code == 404


def test_app_auth_rejects_blank_bearer(app_harness: AppHarness) -> None:
    response = app_harness.client.get("/v1/status", headers={"Authorization": "Bearer "})
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


def test_app_config_import_pydantic_validation_is_400(app_harness: AppHarness) -> None:
    response = app_harness.client.post(
        "/v1/config/import",
        json={
            "version": 1,
            "channels": [
                {"channel_id": "dup", "kind": "feishu"},
                {"channel_id": "dup", "kind": "feishu"},
            ],
            "backends": [{"tenant_id": "", "base_url": "http://x", "token_ref": "${T}"}],
        },
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["detail"].startswith("配置文档无效")


def test_app_maintenance_prune_tolerates_non_dict_retention(app_harness: AppHarness) -> None:
    app_harness.store.set_setting("retention", "not-a-dict")
    response = app_harness.client.post("/v1/db/maintenance", json={"action": "prune"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["action"] == "prune"
