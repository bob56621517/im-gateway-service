"""Extra unit tests for per-kind config construction (gateway/factory.py)."""

from __future__ import annotations

import pytest
from octop_gateway.channels.weixin import WeixinConfig
from octop_gateway.constraints import ChannelConstraints

from im_gateway.core.config import ChannelConfigEntry
from im_gateway.gateway.factory import build_config, constraints_of

# ---------------------------------------------------------------------------
# build_config
# ---------------------------------------------------------------------------


def test_unknown_kind_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown channel kind"):
        build_config("not-a-channel", {})


def test_non_weixin_kind_returns_an_independent_dict_copy() -> None:
    original = {"app_id": "cli_x", "app_secret": "s"}
    built = build_config("feishu", original)
    assert built == original
    assert built is not original
    built["app_id"] = "mutated"
    assert original["app_id"] == "cli_x"


def test_weixin_builds_accounts_and_ignores_unknown_fields() -> None:
    built = build_config(
        "weixin",
        {
            "tenant_id": "acme",
            "media_dir": "~/media",
            "accounts": [
                {
                    "account_id": "acct-1",
                    "token": "tok-1",
                    "account_name": "first",
                    "unknown_account_field": "ignored",
                },
                {"account_id": "acct-2", "token": "tok-2"},
            ],
            "unknown_top_level_field": "ignored",
        },
    )
    assert isinstance(built, WeixinConfig)
    assert built.tenant_id == "acme"
    assert built.media_dir == "~/media"
    assert [account.account_id for account in built.accounts] == ["acct-1", "acct-2"]
    assert built.accounts[0].account_name == "first"
    assert built.accounts[1].token == "tok-2"


def test_weixin_converts_group_context_dict() -> None:
    built = build_config(
        "weixin",
        {"accounts": [], "group_context": {"visibility": "all", "activation": "mention"}},
    )
    assert isinstance(built, WeixinConfig)
    assert built.group_context.visibility == "all"
    assert built.group_context.activation == "mention"


def test_weixin_requires_accounts_to_be_a_list() -> None:
    with pytest.raises(ValueError, match=r"accounts must be a list"):
        build_config("weixin", {"accounts": "not-a-list"})


def test_weixin_rejects_non_object_account_entry() -> None:
    with pytest.raises(ValueError, match=r"accounts\[0\] must be an object"):
        build_config("weixin", {"accounts": ["not-a-dict"]})


def test_weixin_account_type_error_becomes_value_error() -> None:
    # Missing the required ``account_id``/``token`` fields -> TypeError upstream.
    with pytest.raises(ValueError, match="invalid weixin account"):
        build_config("weixin", {"accounts": [{}]})


# ---------------------------------------------------------------------------
# constraints_of
# ---------------------------------------------------------------------------


def test_empty_constraints_means_upstream_defaults() -> None:
    entry = ChannelConfigEntry(channel_id="c1", kind="feishu", constraints={})
    assert constraints_of(entry) is None


def test_valid_constraints_are_built() -> None:
    entry = ChannelConfigEntry(
        channel_id="c1",
        kind="feishu",
        constraints={"show_thinking": True, "reply_timeout": 3.5, "timeout_strategy": "none"},
    )
    built = constraints_of(entry)
    assert isinstance(built, ChannelConstraints)
    assert built.show_thinking is True
    assert built.reply_timeout == 3.5
    assert built.timeout_strategy == "none"


def test_unknown_constraint_key_is_reported() -> None:
    entry = ChannelConfigEntry(channel_id="c1", kind="feishu", constraints={"bogus": 1, "zzz": 2})
    with pytest.raises(ValueError, match="unknown channel constraint"):
        constraints_of(entry)
