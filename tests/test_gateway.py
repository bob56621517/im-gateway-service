"""Tests for the control-plane layer (HANDOVER §13.6).

The upstream ``ChannelManager`` is replaced by :class:`StubManager` so no test
touches the network, platform SDKs, or a real event loop owned by a channel.
The app is exercised through ``fastapi.testclient.TestClient`` with the
lifespan hook enabled, exactly like the daemon runs it.
"""

from __future__ import annotations

import asyncio
import platform
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from octop_gateway import ChannelSubject
from octop_gateway.channel import ChannelCredentialsError
from octop_gateway.constraints import ChannelConstraints
from octop_gateway.models import InboundMessage, MessageEvent, MessageEventType, TextContent

from im_gateway.core.config import MASKED_SECRET, ChannelConfigEntry, ConfigDocument
from im_gateway.db.models import SubjectRecord
from im_gateway.db.store import Store
from im_gateway.gateway.app import create_app, run_daemon
from im_gateway.gateway.dev_processor import echo_processor
from im_gateway.gateway.events import EventBus
from im_gateway.gateway.factory import build_config, constraints_of

# ``fastapi.testclient`` warns that starlette now prefers ``httpx2``.  That
# warning is a UserWarning (not a DeprecationWarning), but silence it locally
# anyway so the suite output stays focused on our own assertions.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)
    from fastapi.testclient import TestClient

TOKEN = "test-control-plane-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

CHANNEL_ID = "feishu:acme:sales"
CHANNEL_CONFIG = {"app_id": "cli_x", "app_secret": "literal-secret"}

INITIAL_DOC: dict[str, Any] = {
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
                "verification_token": "${FEISHU_VERIFICATION_TOKEN}",
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


# ---------------------------------------------------------------------------
# Stub upstream manager
# ---------------------------------------------------------------------------


@dataclass
class StubManager:
    """Records every upstream call; failures are opt-in per kind/subject."""

    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    pushed: list[tuple[str, str, str]] = field(default_factory=list)
    probes: list[dict[str, Any]] = field(default_factory=list)
    subjects: dict[str, list[ChannelSubject]] = field(default_factory=dict)
    fail_add_kinds: set[str] = field(default_factory=set)
    fail_add_config_marker: str | None = None
    fail_probe_kinds: set[str] = field(default_factory=set)
    fail_push_subjects: set[str] = field(default_factory=set)
    started: bool = False
    stopped: bool = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

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
        if self.fail_add_config_marker and self.fail_add_config_marker in str(config):
            raise ValueError("stub refused the marked channel config")
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

    async def probe_channel(
        self,
        channel_type: str,
        config: Any,
        *,
        tenant_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        if channel_type in self.fail_probe_kinds:
            raise ChannelCredentialsError(channel_type, ["app_secret"])
        self.probes.append(
            {
                "channel_type": channel_type,
                "config": config,
                "tenant_id": tenant_id,
                "channel_id": channel_id,
            }
        )

    async def push_text(self, channel_id: str, subject: ChannelSubject, text: str) -> None:
        if subject.subject_id in self.fail_push_subjects:
            raise RuntimeError("stub push failed")
        self.pushed.append((channel_id, subject.subject_id, text))

    def list_subjects(self, channel_id: str) -> list[ChannelSubject]:
        return list(self.subjects.get(channel_id, []))


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    client: TestClient
    manager: StubManager
    store: Store
    app: FastAPI

    @property
    def runtime(self) -> Any:
        return self.app.state.runtime


@pytest.fixture()
def harness(db_path: Path) -> Iterator[Harness]:
    store = Store(db_path)
    manager = StubManager()
    app = create_app(store=store, token=TOKEN, manager=manager)
    with TestClient(app) as client:
        yield Harness(client=client, manager=manager, store=store, app=app)


def channel_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "channel_id": CHANNEL_ID,
        "kind": "feishu",
        "tenant_id": "acme",
        "alias": "sales",
        "name": "Sales",
        "enabled": True,
        "config": dict(CHANNEL_CONFIG),
        "constraints": {"show_thinking": True},
    }
    body.update(overrides)
    return body


def add_channel(harness: Harness, **overrides: Any) -> dict[str, Any]:
    response = harness.client.post("/v1/channels", json=channel_body(**overrides), headers=AUTH)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def import_doc(harness: Harness, document: dict[str, Any]) -> dict[str, Any]:
    response = harness.client.post("/v1/config/import", json=document, headers=AUTH)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# Probes / auth / version
# ---------------------------------------------------------------------------


def test_healthz_is_public(harness: Harness) -> None:
    response = harness.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_public(harness: Harness) -> None:
    response = harness.client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_auth_is_required_everywhere_else(harness: Harness) -> None:
    unauthenticated = harness.client.get("/v1/status")
    assert unauthenticated.status_code == 401
    assert isinstance(unauthenticated.json()["detail"], str)

    wrong = harness.client.get("/v1/status", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401

    ok = harness.client.get("/v1/status", headers=AUTH)
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["service_version"] == "0.1.0"
    assert payload["db_path"].endswith("runtime.db")
    assert payload["counts"] == {"channels": 0, "messages": 0, "subjects": 0}
    assert payload["channels"] == []
    assert payload["backends"] == 0


def test_version_reports_daemon_and_upstream(harness: Harness) -> None:
    payload = harness.client.get("/v1/version", headers=AUTH).json()
    assert payload["service"] == "0.1.0"
    assert payload["python"] == platform.python_version()
    assert payload["octop_gateway"]


def test_lifespan_starts_and_stops_manager(db_path: Path) -> None:
    store = Store(db_path)
    manager = StubManager()
    app = create_app(store=store, token=TOKEN, manager=manager)
    with TestClient(app) as client:
        assert manager.started is True
        assert client.get("/healthz").status_code == 200
    assert manager.stopped is True


# ---------------------------------------------------------------------------
# Channel CRUD
# ---------------------------------------------------------------------------


def test_channels_crud_and_manager_calls(harness: Harness) -> None:
    created = add_channel(harness)
    assert created["channel_id"] == CHANNEL_ID
    assert set(created) == {
        "channel_id",
        "kind",
        "tenant_id",
        "alias",
        "name",
        "enabled",
        "state",
        "last_error",
        "login_supported",
        "credentials",
        "capabilities",
        "created_at",
        "last_started_at",
        "config",
    }
    assert created["config"]["app_secret"] == "***"
    assert created["credentials"]["configured"] is True
    assert created["state"] == "running"
    assert created["last_error"] is None
    assert created["capabilities"]["streaming"] == "buffered"
    assert created["enabled"] is True

    assert len(harness.manager.added) == 1
    upstream = harness.manager.added[0]
    assert upstream["channel_type"] == "feishu"
    assert upstream["channel_id"] == CHANNEL_ID
    assert upstream["tenant_id"] == "acme"
    assert upstream["config"] == CHANNEL_CONFIG
    assert isinstance(upstream["constraints"], ChannelConstraints)
    assert upstream["constraints"].show_thinking is True

    stored = harness.store.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.alias == "sales"
    assert stored.last_started_at is not None

    listed = harness.client.get("/v1/channels", headers=AUTH).json()["channels"]
    assert [channel["channel_id"] for channel in listed] == [CHANNEL_ID]

    by_alias = harness.client.get("/v1/channels/sales", headers=AUTH)
    assert by_alias.status_code == 200
    assert by_alias.json()["channel_id"] == CHANNEL_ID

    missing = harness.client.get("/v1/channels/does-not-exist", headers=AUTH)
    assert missing.status_code == 404
    assert "detail" in missing.json()

    # Duplicate channel_id is rejected without touching the manager or DB.
    duplicate = harness.client.post("/v1/channels", json=channel_body(), headers=AUTH)
    assert duplicate.status_code == 400
    assert len(harness.manager.added) == 1
    assert len(harness.store.list_channels()) == 1

    # Duplicate alias under another channel_id is rejected too.
    duplicate_alias = harness.client.post(
        "/v1/channels",
        json=channel_body(channel_id="feishu:acme:support", alias="sales"),
        headers=AUTH,
    )
    assert duplicate_alias.status_code == 400

    # Alias/name changes are applied in place: no upstream restart.
    renamed = harness.client.patch(
        "/v1/channels/sales", json={"alias": "sales2", "name": "Renamed"}, headers=AUTH
    )
    assert renamed.status_code == 200
    assert renamed.json()["alias"] == "sales2"
    assert len(harness.manager.added) == 1
    assert harness.client.get("/v1/channels/sales2", headers=AUTH).json()["name"] == "Renamed"

    # Disabling stops the upstream instance and keeps the DB row.
    disabled = harness.client.patch("/v1/channels/sales2", json={"enabled": False}, headers=AUTH)
    assert disabled.status_code == 200
    assert disabled.json()["state"] == "disabled"
    assert harness.manager.removed == [CHANNEL_ID]
    stored = harness.store.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.enabled is False
    assert harness.client.get("/readyz").status_code == 200

    # Re-enabling with a new config starts a fresh upstream instance.
    updated = harness.client.patch(
        "/v1/channels/sales2",
        json={"enabled": True, "config": {"app_id": "cli_y", "app_secret": "other"}},
        headers=AUTH,
    )
    assert updated.status_code == 200
    assert updated.json()["state"] == "running"
    assert len(harness.manager.added) == 2
    assert harness.manager.added[1]["config"]["app_id"] == "cli_y"

    # Empty / unknown PATCH payloads are 400s, not silent no-ops.
    assert harness.client.patch("/v1/channels/sales2", json={}, headers=AUTH).status_code == 400

    # Deleting requires the channel to be logged out first (409 guard).
    blocked = harness.client.delete("/v1/channels/sales2", headers=AUTH)
    assert blocked.status_code == 409
    assert "logout" in blocked.json()["detail"]
    harness.client.patch("/v1/channels/sales2", json={"enabled": False}, headers=AUTH)

    # Deleting stops the channel, removes the DB row and honours --purge.
    deleted = harness.client.delete("/v1/channels/sales2", params={"purge": "true"}, headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["purged"] is True
    assert harness.store.get_channel(CHANNEL_ID) is None
    assert harness.manager.removed[-1] == CHANNEL_ID
    assert harness.client.get("/v1/channels/sales2", headers=AUTH).status_code == 404


def test_channel_patch_config_merges_masked_and_partial(harness: Harness) -> None:
    add_channel(harness)
    updated = harness.client.patch(
        "/v1/channels/sales",
        json={"config": {"app_secret": "***"}},
        headers=AUTH,
    )
    assert updated.status_code == 200
    stored = harness.store.get_channel(CHANNEL_ID)
    assert stored is not None
    # "***" keeps the stored secret; omitted keys are preserved (partial PATCH).
    assert stored.config["app_secret"] == "literal-secret"
    assert stored.config["app_id"] == "cli_x"

    # A masked value with no stored fallback is a 400, not a silent literal "***".
    missing = harness.client.patch(
        "/v1/channels/sales",
        json={"config": {"token": "***"}},
        headers=AUTH,
    )
    assert missing.status_code == 400


def test_channel_add_failure_returns_400_without_db_row(harness: Harness) -> None:
    harness.manager.fail_add_kinds.add("wecom")
    response = harness.client.post(
        "/v1/channels",
        json=channel_body(channel_id="wecom:acme:ops", kind="wecom", alias="ops"),
        headers=AUTH,
    )
    assert response.status_code == 400
    assert "detail" in response.json()
    assert harness.store.get_channel("wecom:acme:ops") is None
    assert harness.store.list_channels() == []


def test_unknown_channel_kind_is_400(harness: Harness) -> None:
    response = harness.client.post(
        "/v1/channels",
        json=channel_body(channel_id="mystery:1", kind="mystery", alias=None),
        headers=AUTH,
    )
    assert response.status_code == 400
    assert "unknown channel kind" in response.json()["detail"]


def test_unknown_constraint_is_400(harness: Harness) -> None:
    response = harness.client.post(
        "/v1/channels",
        json=channel_body(channel_id="feishu:acme:typo", alias=None, constraints={"bogus": 1}),
        headers=AUTH,
    )
    assert response.status_code == 400


def test_probe_success_and_credential_failure(harness: Harness) -> None:
    body = {
        "channel_id": "feishu:probe",
        "kind": "feishu",
        "tenant_id": "acme",
        "config": {"app_id": "cli_x", "app_secret": "literal-secret"},
    }
    ok = harness.client.post("/v1/channels/probe", json=body, headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    assert len(harness.manager.probes) == 1
    assert harness.manager.probes[0]["config"] == body["config"]
    assert harness.store.list_channels() == []

    harness.manager.fail_probe_kinds.add("wecom")
    bad = harness.client.post(
        "/v1/channels/probe",
        json={"channel_id": "wecom:probe", "kind": "wecom", "config": {"bot_id": "x"}},
        headers=AUTH,
    )
    assert bad.status_code == 400
    assert "凭证" in bad.json()["detail"]


# ---------------------------------------------------------------------------
# Config export / import
# ---------------------------------------------------------------------------


def test_config_export_masks_literal_secrets(harness: Harness) -> None:
    imported = import_doc(harness, INITIAL_DOC)
    assert imported["applied"] == {"channels": 1, "backends": 1, "settings": 1}
    assert len(harness.manager.added) == 1

    exported = harness.client.get("/v1/config/export", headers=AUTH).json()
    assert exported["version"] == 1
    channel = exported["channels"][0]
    assert channel["channel_id"] == CHANNEL_ID
    assert channel["config"]["app_secret"] == MASKED_SECRET
    assert channel["config"]["verification_token"] == "${FEISHU_VERIFICATION_TOKEN}"
    assert exported["backends"][0]["token_ref"] == MASKED_SECRET
    assert exported["settings"] == {"workers_per_channel": 2}

    raw = harness.client.get("/v1/config/export", params={"with_secrets": "true"}, headers=AUTH).json()
    assert raw["channels"][0]["config"]["app_secret"] == "literal-secret"
    assert raw["backends"][0]["token_ref"] == "literal-backend-token"


def test_config_import_keeps_masked_values_and_deletes_absent_entries(
    harness: Harness,
) -> None:
    import_doc(harness, INITIAL_DOC)
    masked = harness.client.get("/v1/config/export", headers=AUTH).json()
    masked["channels"][0]["name"] = "Renamed via import"
    masked["channels"][0]["alias"] = "sales2"
    applied = import_doc(harness, masked)
    assert applied == {"applied": {"channels": 1, "backends": 1, "settings": 1}, "warnings": []}

    stored = harness.store.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.name == "Renamed via import"
    assert stored.alias == "sales2"
    # ``***`` means "keep the current value", so neither secret was lost.
    assert stored.config["app_secret"] == "literal-secret"
    backend = harness.store.get_backend("acme")
    assert backend is not None
    assert backend.token_ref == "literal-backend-token"
    # Alias/name-only import does not restart the channel.
    assert len(harness.manager.added) == 1

    # Adding a channel via import registers it.
    extended = dict(masked)
    extended["channels"] = [
        *masked["channels"],
        {
            "channel_id": "feishu:acme:support",
            "kind": "feishu",
            "tenant_id": "acme",
            "alias": "support",
            "name": "Support",
            "enabled": True,
            "config": {"app_id": "cli_support"},
            "constraints": {},
        },
    ]
    assert import_doc(harness, extended)["applied"]["channels"] == 2
    assert len(harness.manager.added) == 2

    # Imports delete channels/backends missing from the document.
    only_support = dict(extended)
    only_support["channels"] = [extended["channels"][1]]
    import_doc(harness, only_support)
    assert harness.store.get_channel(CHANNEL_ID) is None
    assert CHANNEL_ID in harness.manager.removed


def test_config_import_invalid_document_is_400_and_writes_nothing(harness: Harness) -> None:
    import_doc(harness, INITIAL_DOC)
    channels_before = harness.store.list_channels()
    backends_before = harness.store.list_backends()
    settings_before = harness.store.get_settings()

    invalid = {
        "version": 1,
        "channels": [{"channel_id": "bad:1", "kind": "not-a-kind"}],
        "backends": [{"tenant_id": "", "base_url": "http://127.0.0.1:1", "token_ref": "${T}"}],
    }
    response = harness.client.post("/v1/config/import", json=invalid, headers=AUTH)
    assert response.status_code == 400
    assert "detail" in response.json()

    assert harness.store.list_channels() == channels_before
    assert harness.store.list_backends() == backends_before
    assert harness.store.get_settings() == settings_before
    assert len(harness.manager.added) == 1

    # A masked secret on a brand-new channel has nothing to resolve to.
    new_masked = {
        "version": 1,
        "channels": [
            {
                "channel_id": "feishu:acme:new",
                "kind": "feishu",
                "tenant_id": "acme",
                "enabled": True,
                "config": {"app_id": "cli_new", "app_secret": MASKED_SECRET},
                "constraints": {},
            }
        ],
        "backends": [{"tenant_id": "acme", "base_url": "http://127.0.0.1:9000", "token_ref": "${T}"}],
    }
    assert harness.client.post("/v1/config/import", json=new_masked, headers=AUTH).status_code == 400
    assert harness.store.get_channel("feishu:acme:new") is None


def test_config_import_manager_failure_warns_but_keeps_db(harness: Harness) -> None:
    """A manager failure is a warning + ``state=error``; the DB is not rolled back."""
    import_doc(harness, INITIAL_DOC)
    harness.manager.fail_add_kinds.add("wecom")

    extended = dict(INITIAL_DOC)
    extended["channels"] = [
        *INITIAL_DOC["channels"],
        {
            "channel_id": "wecom:acme:ops",
            "kind": "wecom",
            "tenant_id": "acme",
            "name": "Ops",
            "enabled": True,
            "config": {"bot_id": "bot", "secret": "literal-secret"},
            "constraints": {},
        },
    ]
    result = import_doc(harness, extended)
    assert result["applied"]["channels"] == 2
    assert len(result["warnings"]) == 1
    assert "wecom:acme:ops" in result["warnings"][0]

    stored = harness.store.get_channel("wecom:acme:ops")
    assert stored is not None
    assert stored.enabled is True
    state, last_error = harness.runtime.channel_state("wecom:acme:ops")
    assert state == "error"
    assert last_error
    assert harness.client.get("/readyz").status_code == 503


# ---------------------------------------------------------------------------
# Push / broadcast / history
# ---------------------------------------------------------------------------


def test_push_requires_idempotency_key_and_deduplicates(harness: Harness) -> None:
    add_channel(harness)
    payload = {"channel_id": "sales", "subject_id": "user-1", "text": "hello"}

    missing_key = harness.client.post("/v1/messages", json=payload, headers=AUTH)
    assert missing_key.status_code == 400
    assert "Idempotency-Key" in missing_key.json()["detail"]
    assert harness.manager.pushed == []

    first = harness.client.post("/v1/messages", json=payload, headers={**AUTH, "Idempotency-Key": "key-1"})
    assert first.status_code == 200
    assert first.json() == {
        "sent": True,
        "deduplicated": False,
        "channel_id": CHANNEL_ID,
        "subject_id": "user-1",
    }
    assert harness.manager.pushed == [(CHANNEL_ID, "user-1", "hello")]

    duplicate = harness.client.post(
        "/v1/messages", json=payload, headers={**AUTH, "Idempotency-Key": "key-1"}
    )
    assert duplicate.status_code == 200
    assert duplicate.json() == {"sent": True, "deduplicated": True}
    assert len(harness.manager.pushed) == 1

    second = harness.client.post("/v1/messages", json=payload, headers={**AUTH, "Idempotency-Key": "key-2"})
    assert second.status_code == 200
    assert len(harness.manager.pushed) == 2

    unknown = harness.client.post(
        "/v1/messages",
        json={"channel_id": "missing", "subject_id": "user-1", "text": "x"},
        headers={**AUTH, "Idempotency-Key": "key-3"},
    )
    assert unknown.status_code == 404


def test_push_to_disabled_channel_is_400(harness: Harness) -> None:
    add_channel(harness)
    harness.client.patch("/v1/channels/sales", json={"enabled": False}, headers=AUTH)
    response = harness.client.post(
        "/v1/messages",
        json={"channel_id": "sales", "subject_id": "user-1", "text": "x"},
        headers={**AUTH, "Idempotency-Key": "disabled-key"},
    )
    assert response.status_code == 400


def test_broadcast_counts_successes_and_failures(harness: Harness) -> None:
    add_channel(harness)
    harness.manager.subjects[CHANNEL_ID] = [
        ChannelSubject(subject_id="user-1"),
        ChannelSubject(subject_id="user-2"),
        ChannelSubject(subject_id="user-3"),
    ]
    harness.manager.fail_push_subjects.add("user-2")

    response = harness.client.post(
        "/v1/messages/broadcast", json={"channel_id": "sales", "text": "hi all"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json() == {"sent": 2, "failed": 1}
    assert {subject_id for _, subject_id, _ in harness.manager.pushed} == {"user-1", "user-3"}

    harness.manager.fail_push_subjects.clear()
    again = harness.client.post(
        "/v1/messages/broadcast", json={"channel_id": CHANNEL_ID, "text": "again"}, headers=AUTH
    )
    assert again.json() == {"sent": 3, "failed": 0}


def test_history_resolves_alias_and_filters(harness: Harness) -> None:
    add_channel(harness)
    for index in range(2):
        harness.client.post(
            "/v1/messages",
            json={"channel_id": "sales", "subject_id": f"user-{index}", "text": f"msg {index}"},
            headers={**AUTH, "Idempotency-Key": f"history-key-{index}"},
        )

    payload = harness.client.get(
        "/v1/history", params={"channel_id": "sales", "limit": 10}, headers=AUTH
    ).json()
    messages = payload["messages"]
    assert len(messages) == 2
    assert all(message["channel_id"] == CHANNEL_ID for message in messages)
    assert all(message["direction"] == "out" for message in messages)
    assert [message["content"][0]["text"] for message in messages] == ["msg 1", "msg 0"]

    filtered = harness.client.get(
        "/v1/history", params={"channel_id": "sales", "subject_id": "user-1"}, headers=AUTH
    ).json()["messages"]
    assert len(filtered) == 1
    assert filtered[0]["subject_id"] == "user-1"

    assert harness.client.get("/v1/history", params={"channel_id": "nope"}, headers=AUTH).status_code == 404


def test_subjects_merge_db_and_memory(harness: Harness) -> None:
    add_channel(harness)
    harness.manager.subjects[CHANNEL_ID] = [
        ChannelSubject(subject_id="live-only", chat_type="group", display_name="Live")
    ]
    harness.store.upsert_subject(
        SubjectRecord(
            channel_id=CHANNEL_ID,
            subject_id="db-only",
            chat_type="direct",
            display_name="DB",
            metadata={"durable": True},
            first_seen=1.0,
            last_seen=2.0,
        )
    )
    subjects = harness.client.get("/v1/channels/sales/subjects", headers=AUTH).json()["subjects"]
    by_id = {subject["subject_id"]: subject for subject in subjects}
    assert by_id["live-only"]["display_name"] == "Live"
    assert by_id["db-only"]["metadata"] == {"durable": True}
    assert harness.client.get("/v1/channels/nope/subjects", headers=AUTH).status_code == 404


def test_events_sse_handshake_and_alias_filter(harness: Harness) -> None:
    add_channel(harness)
    assert harness.client.get("/v1/events").status_code == 401

    with harness.client.stream("GET", "/v1/events", params={"channel_id": "sales"}, headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert next(response.iter_lines()) == ": connected"
        response.close()


# ---------------------------------------------------------------------------
# Readiness / maintenance
# ---------------------------------------------------------------------------


def test_readyz_reports_channel_state(harness: Harness) -> None:
    add_channel(harness)
    assert harness.client.get("/readyz").status_code == 200

    harness.runtime._set_state(CHANNEL_ID, "error", "registration failed")
    not_ready = harness.client.get("/readyz")
    assert not_ready.status_code == 503
    body = not_ready.json()
    assert body["detail"]
    assert body["channels"] == [
        {"channel_id": CHANNEL_ID, "state": "error", "last_error": "registration failed"}
    ]

    harness.runtime._set_state(CHANNEL_ID, "running")
    assert harness.client.get("/readyz").status_code == 200


def test_db_maintenance_prune_and_vacuum(harness: Harness) -> None:
    add_channel(harness)
    harness.client.post(
        "/v1/messages",
        json={"channel_id": "sales", "subject_id": "user-1", "text": "prune me"},
        headers={**AUTH, "Idempotency-Key": "maintenance-key"},
    )
    harness.store.set_setting("retention", {"chat_days": 0})

    pruned = harness.client.post("/v1/db/maintenance", json={"action": "prune"}, headers=AUTH)
    assert pruned.status_code == 200
    assert pruned.json()["action"] == "prune"
    assert pruned.json()["deleted"]["messages"] == 1

    vacuumed = harness.client.post("/v1/db/maintenance", json={"action": "vacuum"}, headers=AUTH)
    assert vacuumed.status_code == 200
    assert vacuumed.json() == {"action": "vacuum", "ok": True}

    unsupported = harness.client.post("/v1/db/maintenance", json={"action": "explode"}, headers=AUTH)
    assert unsupported.status_code == 400


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------


async def test_event_bus_publish_reaches_subscriber() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    received: list[dict[str, Any]] = []

    async def consume() -> None:
        iterator = bus.subscribe()
        try:
            async for event in iterator:
                received.append(event)
                return
        finally:
            await iterator.aclose()

    consumer = asyncio.create_task(consume())
    for _ in range(100):
        if bus.subscriber_count:
            break
        await asyncio.sleep(0)
    assert bus.subscriber_count == 1

    bus.publish({"type": "channel.state", "channel_id": CHANNEL_ID, "state": "running"})
    await asyncio.wait_for(consumer, timeout=2)
    assert received == [{"type": "channel.state", "channel_id": CHANNEL_ID, "state": "running"}]
    assert bus.subscriber_count == 0


async def test_event_bus_delivers_to_every_subscriber() -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    received: list[list[dict[str, Any]]] = [[], []]

    async def consume(sink: list[dict[str, Any]]) -> None:
        iterator = bus.subscribe()
        try:
            async for event in iterator:
                sink.append(event)
                return
        finally:
            await iterator.aclose()

    consumers = [asyncio.create_task(consume(sink)) for sink in received]
    for _ in range(100):
        if bus.subscriber_count == 2:
            break
        await asyncio.sleep(0)
    assert bus.subscriber_count == 2

    bus.publish({"type": "channel.state", "channel_id": CHANNEL_ID, "state": "running"})
    await asyncio.wait_for(asyncio.gather(*consumers), timeout=2)
    expected = [{"type": "channel.state", "channel_id": CHANNEL_ID, "state": "running"}]
    assert received == [expected, expected]
    assert bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# Factory / dev processor
# ---------------------------------------------------------------------------


def test_build_config_passthrough_and_unknown_kind() -> None:
    config = {"app_id": "cli_x", "app_secret": "s"}
    built = build_config("feishu", config)
    assert built == config
    assert built is not config
    with pytest.raises(ValueError, match="unknown channel kind"):
        build_config("mystery", {})


def test_build_config_weixin_builds_accounts() -> None:
    from octop_gateway.channels.weixin import WeixinConfig

    built = build_config(
        "weixin",
        {
            "tenant_id": "acme",
            "media_dir": "~/media",
            "accounts": [
                {"account_id": "acct-1", "token": "tok", "unknown_field": "ignored"},
            ],
            "unknown_top_level": "ignored",
        },
    )
    assert isinstance(built, WeixinConfig)
    assert built.tenant_id == "acme"
    assert built.media_dir == "~/media"
    assert [account.account_id for account in built.accounts] == ["acct-1"]


def test_constraints_of_validates_and_returns_none() -> None:
    entry = ChannelConfigEntry(channel_id="c1", kind="feishu", constraints={})
    assert constraints_of(entry) is None
    override = constraints_of(
        ChannelConfigEntry(channel_id="c1", kind="feishu", constraints={"show_thinking": True})
    )
    assert isinstance(override, ChannelConstraints)
    assert override.show_thinking is True
    with pytest.raises(ValueError, match="unknown channel constraint"):
        constraints_of(ChannelConfigEntry(channel_id="c1", kind="feishu", constraints={"bogus": 1}))


async def test_echo_processor_is_an_async_generator() -> None:
    message = InboundMessage(channel_id="c1", content=[TextContent(text="hello")])
    events: list[MessageEvent] = [event async for event in echo_processor(message)]
    assert [event.type for event in events] == [
        MessageEventType.TYPING,
        MessageEventType.MESSAGE,
        MessageEventType.COMPLETED,
    ]
    assert events[1].content[0].text == "echo: hello"


# ---------------------------------------------------------------------------
# run_daemon listen-address parsing
# ---------------------------------------------------------------------------


@pytest.fixture()
def daemon_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Capture ``uvicorn.run`` kwargs and keep bootstrap files in ``tmp_path``."""
    monkeypatch.setenv("IMGW_TOKEN_FILE", str(tmp_path / "token"))
    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    captured["db"] = str(tmp_path / "runtime.db")
    return captured


def test_run_daemon_host_port(daemon_env: dict[str, Any]) -> None:
    run_daemon(host="127.0.0.1", port=9811, db=daemon_env["db"])
    assert daemon_env["kwargs"] == {"host": "127.0.0.1", "port": 9811}
    assert daemon_env["app"].state.runtime is not None


def test_run_daemon_explicit_listen_wins(daemon_env: dict[str, Any]) -> None:
    run_daemon(
        listen="tcp://0.0.0.0:1234",
        host="127.0.0.1",
        port=9999,
        db=daemon_env["db"],
    )
    assert daemon_env["kwargs"] == {"host": "0.0.0.0", "port": 1234}


def test_run_daemon_unix_socket(daemon_env: dict[str, Any], tmp_path: Path) -> None:
    socket_path = tmp_path / "imgw.sock"
    run_daemon(listen=f"unix://{socket_path}", db=daemon_env["db"])
    assert daemon_env["kwargs"] == {"uds": str(socket_path)}


def test_run_daemon_defaults_to_paths_default_listen(
    monkeypatch: pytest.MonkeyPatch, daemon_env: dict[str, Any]
) -> None:
    from im_gateway.gateway import app as app_module

    monkeypatch.setattr(app_module.paths, "default_listen", lambda: "tcp://127.0.0.1:8765")
    run_daemon(db=daemon_env["db"])
    assert daemon_env["kwargs"] == {"host": "127.0.0.1", "port": 8765}


def test_parse_listen_rejects_garbage() -> None:
    from im_gateway.gateway.app import _parse_listen_address

    with pytest.raises(ValueError, match="invalid listen address"):
        _parse_listen_address(listen="nonsense", host=None, port=None)


def test_config_document_roundtrips_from_export(harness: Harness) -> None:
    import_doc(harness, INITIAL_DOC)
    exported = harness.client.get("/v1/config/export", headers=AUTH).json()
    document = ConfigDocument.model_validate(exported)
    assert [entry.channel_id for entry in document.channels] == [CHANNEL_ID]
    assert [entry.tenant_id for entry in document.backends] == ["acme"]


# ---------------------------------------------------------------------------
# Lead review fixes
# ---------------------------------------------------------------------------


def _running_channel(harness: Harness) -> None:
    add_channel(harness)
    assert harness.runtime.channel_state(CHANNEL_ID) == ("running", None)


def test_broadcast_targets_db_subjects_after_restart(harness: Harness) -> None:
    """Broadcast must not depend on upstream's in-memory subject registry."""
    _running_channel(harness)
    harness.store.upsert_subject(
        SubjectRecord(
            channel_id=CHANNEL_ID,
            subject_id="ou_1",
            chat_type="direct",
            display_name="Zhang",
            metadata={"to_handle": "ou_1"},
            first_seen=1.0,
            last_seen=2.0,
        )
    )
    assert harness.manager.subjects == {}  # nothing in memory (fresh daemon)

    response = harness.client.post(
        "/v1/messages/broadcast",
        json={"channel_id": "sales", "text": "hello"},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"sent": 1, "failed": 0}
    assert harness.manager.pushed == [(CHANNEL_ID, "ou_1", "hello")]


def test_update_channel_rolls_back_when_new_config_cannot_start(harness: Harness) -> None:
    """A failed PATCH must not leave an unstartable config in the database."""
    _running_channel(harness)
    harness.manager.fail_add_config_marker = "boom"

    response = harness.client.patch(
        "/v1/channels/sales",
        json={"config": {**CHANNEL_CONFIG, "boom": "1"}},
        headers=AUTH,
    )
    assert response.status_code == 400
    record = harness.store.get_channel(CHANNEL_ID)
    assert record is not None
    assert record.config == CHANNEL_CONFIG
    assert harness.runtime.channel_state(CHANNEL_ID)[0] == "running"
    # The previous instance was brought back after the rollback.
    assert harness.manager.added[-1]["config"] == CHANNEL_CONFIG
