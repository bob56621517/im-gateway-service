"""Tests for the developer echo backend kit (``imgw run-dev``)."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from im_gateway.core.contracts import (
    ChannelCapabilities,
    ChannelRef,
    FrameKind,
    SubjectRef,
    TextContent,
    TurnFrame,
)
from im_gateway.dev.backend import create_dev_backend_app, summary_text
from im_gateway.gateway.processor import resolve_backend_spec


def start_frame() -> TurnFrame:
    return TurnFrame(
        turn_id="turn-1",
        seq=0,
        kind=FrameKind.START,
        channel=ChannelRef(channel_id="weixin:wx", kind="weixin", tenant_id=""),
        subject=SubjectRef(subject_id="user-1", chat_type="direct", display_name="Alice"),
        session_key="session-1",
        idempotency_key="idem-1",
        deadline_ms=1000,
        capabilities=ChannelCapabilities(),
        content=[TextContent(text="hello dev backend")],
    )


def _sse_payloads(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_dev_backend_echoes_start_frame() -> None:
    client = TestClient(create_dev_backend_app())
    response = client.post("/v1/turns", json=start_frame().model_dump(mode="json"))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _sse_payloads(response.text)
    assert [frame["kind"] for frame in frames] == ["message", "completed"]
    text = frames[0]["content"][0]["text"]
    assert "channel_id      weixin:wx" in text
    assert "channel_kind    weixin" in text
    assert "subject_id      user-1" in text
    assert "subject_name    Alice" in text
    assert "chat_type       direct" in text
    assert "session_key     session-1" in text
    assert "turn_id         turn-1" in text
    assert "idempotency_key idem-1" in text
    assert "message         hello dev backend" in text
    assert frames[0]["seq"] == 1
    assert frames[1]["turn_id"] == "turn-1"
    assert frames[1]["seq"] == 2


def test_dev_backend_rejects_bad_frames() -> None:
    client = TestClient(create_dev_backend_app())
    assert client.post("/v1/turns", json={"nope": True}).status_code == 400

    completed = TurnFrame(turn_id="turn-1", seq=1, kind=FrameKind.COMPLETED)
    response = client.post("/v1/turns", json=completed.model_dump(mode="json"))
    assert response.status_code == 400
    assert "kind=start" in response.json()["detail"]


def test_dev_backend_token_and_health() -> None:
    client = TestClient(create_dev_backend_app(token="dev-token"))
    assert client.get("/healthz").json()["mode"] == "dev-backend"

    body = start_frame().model_dump(mode="json")
    assert client.post("/v1/turns", json=body).status_code == 401
    ok = client.post("/v1/turns", json=body, headers={"Authorization": "Bearer dev-token"})
    assert ok.status_code == 200


def test_summary_text_contains_everything() -> None:
    text = summary_text(start_frame())
    for needle in (
        "weixin:wx",
        "weixin",
        "user-1",
        "Alice",
        "direct",
        "session-1",
        "turn-1",
        "idem-1",
        "hello dev backend",
    ):
        assert needle in text


def test_resolve_backend_spec_points_at_dev_backend() -> None:
    """``imgw run-dev`` just overrides the global URL to the echo backend."""
    spec = resolve_backend_spec(backend_url="http://127.0.0.1:9900/", env={})
    assert spec.base_url == "http://127.0.0.1:9900"
