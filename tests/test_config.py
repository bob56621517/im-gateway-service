"""Tests for the portable config document (HANDOVER §13.4, §16).

Version 2 removed tenant/backend routing: documents from version 1 (or version 2
documents that still carry the removed fields) are rejected with a clear error.
"""

from __future__ import annotations

import pytest

from im_gateway.core.config import DOCUMENT_VERSION, ChannelConfigEntry, ConfigDocument


def base_doc(**overrides: object) -> ConfigDocument:
    values: dict[str, object] = {
        "channels": [
            ChannelConfigEntry(
                channel_id="feishu:acme:sales",
                kind="feishu",
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


def test_document_defaults_to_current_version() -> None:
    doc = ConfigDocument.model_validate({"channels": []})
    assert DOCUMENT_VERSION == 2
    assert doc.version == 2
    assert doc.settings == {}


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


def test_version_1_document_rejected_with_removed_fields_hint() -> None:
    with pytest.raises(ValueError, match="tenant/backends"):
        ConfigDocument.model_validate(
            {
                "version": 1,
                "channels": [{"channel_id": "a", "kind": "feishu"}],
                "backends": [],
            }
        )


def test_unknown_version_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported config document version"):
        ConfigDocument.model_validate({"version": 3, "channels": []})


def test_version_2_backends_field_rejected() -> None:
    with pytest.raises(ValueError, match="removed in version 2: backends"):
        ConfigDocument.model_validate(
            {"version": 2, "channels": [], "backends": [{"base_url": "http://x"}]}
        )


def test_channel_tenant_id_rejected() -> None:
    with pytest.raises(ValueError, match="was removed in config document"):
        ConfigDocument.model_validate(
            {
                "version": 2,
                "channels": [{"channel_id": "a", "kind": "feishu", "tenant_id": "acme"}],
            }
        )


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ConfigDocument.model_validate({"version": 2, "channels": [], "mystery": True})


def test_settings_are_preserved() -> None:
    doc = base_doc(settings={"workers_per_channel": 2})
    assert doc.settings == {"workers_per_channel": 2}
