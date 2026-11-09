"""Per-channel-kind capability table (HANDOVER §12.7).

These values are **hand-maintained**, not derived from octop-gateway: the
upstream ``ChannelConstraints`` only covers rate limits / timeouts / display
flags, not rendering capabilities. They are the initial best-effort values and
must be calibrated per channel by real-platform smoke tests (see §12.7).
"""

from __future__ import annotations

from im_gateway.core.contracts import ChannelCapabilities

__all__ = ["CAPABILITIES", "capabilities_for"]

#: Initial table. `streaming="native"` means the channel renders token-by-token
#: updates itself (WeCom reply stream / QQ blocks / Telegram edit); `buffered`
#: means deltas are accumulated and sent once at COMPLETED.
CAPABILITIES: dict[str, ChannelCapabilities] = {
    "feishu": ChannelCapabilities(
        streaming="buffered",
        markdown="native",  # post + tag:"md"
        media={"image": True, "video": False, "audio": True, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "wecom": ChannelCapabilities(
        streaming="native",  # reply_stream
        markdown="native",
        media={"image": True, "video": False, "audio": True, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "dingtalk": ChannelCapabilities(
        streaming="buffered",
        markdown="native",
        media={"image": True, "video": False, "audio": False, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "qq": ChannelCapabilities(
        streaming="native",  # stream blocks
        markdown="native",  # falls back to plain text on 400
        media={"image": True, "video": False, "audio": False, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "telegram": ChannelCapabilities(
        streaming="native",  # message editing
        markdown="converted",  # markdown -> Telegram HTML
        media={"image": True, "video": True, "audio": True, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "discord": ChannelCapabilities(
        streaming="buffered",
        markdown="native",
        media={"image": True, "video": False, "audio": False, "file": True},
        max_text_len=2000,
        native_ops=[],
    ),
    "weixin": ChannelCapabilities(
        streaming="buffered",
        markdown="plain",
        media={"image": True, "video": False, "audio": True, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "yuanbao": ChannelCapabilities(
        streaming="buffered",
        markdown="plain",
        media={"image": True, "video": False, "audio": False, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "xiaoyi": ChannelCapabilities(
        streaming="buffered",
        markdown="plain",
        media={"image": True, "video": False, "audio": False, "file": True},
        max_text_len=4000,
        native_ops=[],
    ),
    "mqtt": ChannelCapabilities(
        streaming="buffered",
        markdown="plain",
        media={},
        max_text_len=1024,
        native_ops=[],
    ),
}


def capabilities_for(kind: str) -> ChannelCapabilities:
    """Return capabilities for a channel kind; unknown kinds are conservative."""
    return CAPABILITIES.get(kind, ChannelCapabilities())
