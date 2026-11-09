"""Declarative config document used by ``config export`` / ``config import``.

The database is the single source of truth (HANDOVER §13); this document is the
portable JSON form used for git, backups and cross-environment migration. It is
**not** a runtime config file: the daemon never reads it on boot.

Secret handling: ``token_ref`` / config values may contain ``${ENV_VAR}``
references. ``config export`` masks literal secrets as ``"***"`` unless
``with_secrets`` is requested; ``config import`` treats ``"***"`` as "keep the
current value" (HANDOVER §13.4).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "MASKED_SECRET",
    "BackendConfigEntry",
    "ChannelConfigEntry",
    "ConfigDocument",
    "ConfigValidationError",
]

MASKED_SECRET = "***"


class ConfigValidationError(ValueError):
    """Raised when a config document is structurally valid but semantically broken."""


class ChannelConfigEntry(BaseModel):
    channel_id: str
    kind: str
    tenant_id: str | None = None
    alias: str | None = None
    name: str = ""
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class BackendConfigEntry(BaseModel):
    tenant_id: str = ""
    base_url: str
    token_ref: str
    timeout_ms: int = 120_000
    enabled: bool = True


class ConfigDocument(BaseModel):
    version: int = 1
    channels: list[ChannelConfigEntry] = Field(default_factory=list)
    backends: list[BackendConfigEntry] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_references(self) -> ConfigDocument:
        channel_ids = [entry.channel_id for entry in self.channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("duplicate channel_id in config document")

        aliases = [entry.alias for entry in self.channels if entry.alias]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate channel alias in config document")

        tenants = {entry.tenant_id for entry in self.backends}
        for entry in self.channels:
            if not entry.enabled:
                continue
            tenant = entry.tenant_id or ""
            if not self.backends:
                raise ValueError(f"channel {entry.channel_id!r} enabled but no backend configured")
            if tenant not in tenants and "" not in tenants:
                raise ValueError(
                    f"channel {entry.channel_id!r} references tenant {tenant!r} but no backend defines it"
                )
        return self
