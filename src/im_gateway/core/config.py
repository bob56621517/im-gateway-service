"""Declarative config document used by ``config export`` / ``config import``.

The database is the single source of truth (HANDOVER §13); this document is the
portable JSON form used for git, backups and cross-environment migration. It is
**not** a runtime config file: the daemon never reads it on boot.

Secret handling: ``token_ref`` / config values may contain ``${ENV_VAR}``
references. ``config export`` masks literal secrets as ``"***"`` unless
``with_secrets`` is requested; ``config import`` treats ``"***"`` as "keep the
current value" (HANDOVER §13.4).

Version 2 removed the tenant/backend *routing* layer (GitHub issue #3): the
gateway now has exactly one backend URL, supplied by ``--backend-url`` /
``IMGW_BACKEND_URL``. Consequently a document version 1 (or a version 2
document that still carries ``backends`` / ``tenant_id``) is rejected with an
explicit error instead of being silently accepted.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "DOCUMENT_VERSION",
    "MASKED_SECRET",
    "ChannelConfigEntry",
    "ConfigDocument",
    "ConfigValidationError",
]

MASKED_SECRET = "***"

#: Current config-document version. Version 1 (tenant/backends routing) is gone.
DOCUMENT_VERSION: Final[int] = 2

#: Top-level document keys that only existed in version 1.
_REMOVED_DOCUMENT_FIELDS: Final[tuple[str, ...]] = ("backends", "tenants")


class ConfigValidationError(ValueError):
    """Raised when a config document is structurally valid but semantically broken."""


class ChannelConfigEntry(BaseModel):
    """One channel in the portable config document (v2).

    ``tenant_id`` was removed in v2; a document that still contains it is
    rejected by :meth:`_reject_removed_fields` with a clear message.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    kind: str
    alias: str | None = None
    name: str = ""
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and "tenant_id" in data:
            channel_id = data.get("channel_id", "?")
            raise ValueError(
                f"channel {channel_id!r}: `tenant_id` was removed in config document "
                "version 2 (the gateway no longer routes per tenant)"
            )
        return data


class ConfigDocument(BaseModel):
    """Portable configuration document, version :data:`DOCUMENT_VERSION` only."""

    model_config = ConfigDict(extra="forbid")

    version: int = DOCUMENT_VERSION
    channels: list[ChannelConfigEntry] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _check_version(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        version = data.get("version", DOCUMENT_VERSION)
        if version != DOCUMENT_VERSION:
            if version == 1:
                raise ValueError(
                    "config document version 1 is no longer supported: the tenant/backends "
                    "routing layer was removed in version 2 (the gateway now has one backend "
                    "URL from --backend-url / IMGW_BACKEND_URL). Re-export the document from "
                    "the current daemon, or bump `version` to 2 and delete `backends` / "
                    "`tenant_id`."
                )
            raise ValueError(
                f"unsupported config document version {version!r}; expected {DOCUMENT_VERSION}"
            )
        removed = [key for key in _REMOVED_DOCUMENT_FIELDS if key in data]
        if removed:
            raise ValueError(
                f"config document field(s) removed in version 2: {', '.join(removed)} "
                "(the gateway now uses one backend URL: --backend-url / IMGW_BACKEND_URL)"
            )
        return data

    @model_validator(mode="after")
    def _check_references(self) -> ConfigDocument:
        channel_ids = [entry.channel_id for entry in self.channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("duplicate channel_id in config document")

        aliases = [entry.alias for entry in self.channels if entry.alias]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate channel alias in config document")
        return self
