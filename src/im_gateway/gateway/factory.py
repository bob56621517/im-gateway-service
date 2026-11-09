"""Per-kind channel config construction for octop-gateway.

Why this module exists (HANDOVER §11.2 #14): ``ChannelManager.add_channel``
feeds a plain ``dict`` through ``ChannelConfig.from_dict``, which **silently
drops unknown keys** — a typo in a config name would never be reported.  More
importantly, ``weixin``'s ``accounts: list[WeixinAccountConfig]`` cannot be
expressed by a dict of scalars at all.

So the control plane builds the upstream config object itself:

* every kind except ``weixin`` is passed through as a dict and converted by
  upstream (that path is already validated by ``from_dict``);
* ``weixin`` is special-cased and a real :class:`WeixinConfig` object is built
  from ``config.accounts`` (unknown fields are ignored, as upstream does);
* ``constraints`` are validated here too, so a typo in the constraints dict is
  a ``400`` instead of a silently ignored override.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Final

from octop_gateway.channels import SUPPORTED_CHANNEL_KINDS
from octop_gateway.constraints import ChannelConstraints

from im_gateway.core.config import ChannelConfigEntry

__all__ = ["build_config", "constraints_of"]

#: Valid ``ChannelConstraints`` field names (used to reject typos).
_CONSTRAINT_FIELD_NAMES: Final[frozenset[str]] = frozenset(field.name for field in fields(ChannelConstraints))


def build_config(kind: str, config: dict[str, Any]) -> Any:
    """Build the upstream config object for one channel instance.

    Returns an upstream ``ChannelConfig`` subclass for kinds that need it
    (currently only ``weixin``) and a plain dict for the rest — ``ChannelManager``
    will run that dict through the kind's ``from_dict``.

    Raises:
        ValueError: for an unknown channel kind or a malformed ``weixin``
            config/account entry.
    """
    if kind not in SUPPORTED_CHANNEL_KINDS:
        raise ValueError(f"unknown channel kind: {kind!r}")
    if kind != "weixin":
        return dict(config)
    return _build_weixin_config(config)


def _build_weixin_config(config: dict[str, Any]) -> Any:
    """Build a ``WeixinConfig`` because its ``accounts`` list cannot be a dict."""
    from octop_gateway.channels.weixin import WeixinAccountConfig, WeixinConfig
    from octop_gateway.group_context import GroupContextConfig

    known = {field.name for field in fields(WeixinConfig)}
    values = {key: value for key, value in config.items() if key in known and key != "accounts"}
    group_context = values.get("group_context")
    if isinstance(group_context, dict):
        values["group_context"] = GroupContextConfig.from_dict(group_context)

    account_fields = {field.name for field in fields(WeixinAccountConfig)}
    raw_accounts = config.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("weixin config.accounts must be a list")
    accounts: list[Any] = []
    for index, raw in enumerate(raw_accounts):
        if not isinstance(raw, dict):
            raise ValueError(f"weixin config.accounts[{index}] must be an object")
        filtered = {key: value for key, value in raw.items() if key in account_fields}
        try:
            accounts.append(WeixinAccountConfig(**filtered))
        except TypeError as exc:
            raise ValueError(f"invalid weixin account at accounts[{index}]: {exc}") from exc
    return WeixinConfig(accounts=accounts, **values)


def constraints_of(entry: ChannelConfigEntry) -> ChannelConstraints | None:
    """Build a ``ChannelConstraints`` override from a config entry.

    An empty ``entry.constraints`` means "use the channel's upstream defaults"
    and yields ``None``.  Unknown keys raise ``ValueError`` so typos are
    reported instead of being dropped (upstream ignores unknown keys).
    """
    if not entry.constraints:
        return None
    unknown = set(entry.constraints) - _CONSTRAINT_FIELD_NAMES
    if unknown:
        raise ValueError(f"unknown channel constraint(s): {', '.join(sorted(unknown))}")
    return ChannelConstraints(**entry.constraints)
