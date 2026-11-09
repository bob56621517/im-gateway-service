"""Tests for the portable config document (HANDOVER §13.4)."""

from __future__ import annotations

import pytest

from im_gateway.core.config import BackendConfigEntry, ChannelConfigEntry, ConfigDocument


def base_doc(**overrides: object) -> ConfigDocument:
    values: dict[str, object] = {
        "backends": [BackendConfigEntry(base_url="http://127.0.0.1:8080", token_ref="${TOKEN}")],
        "channels": [
            ChannelConfigEntry(
                channel_id="feishu:acme:sales",
                kind="feishu",
                tenant_id="",
                alias="feishu-sales",
                config={"app_id": "cli_x", "app_secret": "${FEISHU_SECRET}"},
            )
        ],
    }
    values.update(overrides)
    return ConfigDocument.model_validate(values)


def test_roundtrip() -> None:
    doc = base_doc()
    payload = doc.model_dump_json()
    assert ConfigDocument.model_validate_json(payload) == doc


def test_duplicate_channel_id_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate channel_id"):
        base_doc(
            channels=[
                ChannelConfigEntry(channel_id="a", kind="feishu"),
                ChannelConfigEntry(channel_id="a", kind="feishu"),
            ]
        )


def test_duplicate_alias_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate channel alias"):
        base_doc(
            channels=[
                ChannelConfigEntry(channel_id="a", kind="feishu", alias="x"),
                ChannelConfigEntry(channel_id="b", kind="feishu", alias="x"),
            ]
        )


def test_channel_without_backend_rejected() -> None:
    with pytest.raises(ValueError, match="no backend configured"):
        base_doc(backends=[], channels=[ChannelConfigEntry(channel_id="a", kind="feishu")])


def test_tenant_falls_back_to_default_backend() -> None:
    # A channel may carry tenant_id purely for grouping; with a default
    # backend (tenant_id="") present, it is served by that backend.
    doc = base_doc(channels=[ChannelConfigEntry(channel_id="a", kind="feishu", tenant_id="acme")])
    assert doc.channels[0].tenant_id == "acme"


def test_tenant_without_default_or_matching_backend_rejected() -> None:
    with pytest.raises(ValueError, match="no backend defines it"):
        base_doc(
            backends=[BackendConfigEntry(tenant_id="other", base_url="http://a", token_ref="x")],
            channels=[ChannelConfigEntry(channel_id="a", kind="feishu", tenant_id="acme")],
        )


def test_tenant_backend_ok() -> None:
    doc = base_doc(
        backends=[
            BackendConfigEntry(tenant_id="", base_url="http://a", token_ref="x"),
            BackendConfigEntry(tenant_id="acme", base_url="http://b", token_ref="y"),
        ],
        channels=[ChannelConfigEntry(channel_id="a", kind="feishu", tenant_id="acme")],
    )
    assert doc.channels[0].tenant_id == "acme"


def test_disabled_channel_skips_backend_check() -> None:
    doc = base_doc(
        backends=[],
        channels=[ChannelConfigEntry(channel_id="a", kind="feishu", enabled=False)],
    )
    assert doc.channels[0].enabled is False
