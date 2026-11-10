"""Per-kind credential fields and the "is this channel bound?" summary.

The control plane must describe channel readiness (``creds ok/missing``) without
ever returning secret values (HANDOVER §13.5 / §13.8).  This module owns the
per-kind field names; the masking itself stays in ``gateway/app.py`` so every
view goes through the same ``_mask_secrets`` implementation.

``weixin`` is special: its credentials live in a list of account dicts
(``accounts[].token``), so "configured" means "at least one account".
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "REQUIRED_CREDENTIALS",
    "SILENT_CREDENTIAL_FIELDS",
    "SILENT_LOGIN_KINDS",
    "credential_status",
    "silent_login_config",
]

#: Fields that must be present (non-empty) for a channel kind to be "bound".
#: This is deliberately narrower than the upstream config dataclass: it only
#: lists the fields a login flow fills in.
REQUIRED_CREDENTIALS: Final[dict[str, tuple[str, ...]]] = {
    "feishu": ("app_id", "app_secret"),
    "wecom": ("bot_id", "secret"),
    "dingtalk": ("app_key", "app_secret"),
    "qq": ("app_id", "secret"),
    "weixin": ("accounts",),
}

#: Neutral CLI credentials (``--bot-key`` / ``--bot-token``) per channel kind.
#: Upstream config field names are platform-specific, so the CLI maps the neutral
#: pair onto them instead of exposing six different flag names (HANDOVER §16).
#: ``weixin`` is absent on purpose: a WeChat account is bound one-to-one by the
#: platform, so it can only be claimed by scanning the QR/production URL.
SILENT_CREDENTIAL_FIELDS: Final[dict[str, tuple[str, str]]] = {
    "feishu": ("app_id", "app_secret"),
    "wecom": ("bot_id", "secret"),
    "dingtalk": ("app_key", "app_secret"),
    "qq": ("app_id", "secret"),
}

SILENT_LOGIN_KINDS: Final[frozenset[str]] = frozenset(SILENT_CREDENTIAL_FIELDS)


def silent_login_config(kind: str, bot_key: str, bot_token: str) -> dict[str, str]:
    """Map the neutral credential pair onto one channel kind's config fields."""
    fields = SILENT_CREDENTIAL_FIELDS.get(kind)
    if fields is None:
        raise ValueError(f"channel kind {kind!r} does not support silent credentials")
    return {fields[0]: bot_key, fields[1]: bot_token}


def _present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | dict):
        return len(value) > 0
    return value is not None


def credential_status(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return ``{configured, required, missing}`` for one channel kind.

    ``configured`` is ``None`` for kinds this table does not know (for example
    ``telegram``): the caller should render those as ``n/a`` instead of
    claiming the channel is broken.
    """
    required = REQUIRED_CREDENTIALS.get(kind)
    if required is None:
        return {"configured": None, "required": [], "missing": []}
    missing = [field for field in required if not _present(config.get(field))]
    return {"configured": not missing, "required": list(required), "missing": missing}
