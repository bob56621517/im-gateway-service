"""Idempotency keys for inbound messages (HANDOVER §11.4 / §13.2).

``octop-gateway`` normalizes every channel into the same ``InboundMessage``
shape but offers **no unified message id**: each channel stores its platform
id under a channel-specific ``metadata`` key (see the per-kind table below).
Platform redeliveries must not start a second turn, so the processor claims a
key in ``inbound_dedup`` before doing any work.

Key format:

- platform id available: ``{channel_id}:{platform_msg_id}``
- no platform id:        ``{channel_id}:h:{sha256(channel_id|subject_id|timestamp|text)[:32]}``

The fallback hash is deterministic for the same inbound message, which is what
makes a redelivery of a channel without message ids (``wecom`` / ``mqtt``) a
duplicate.  It is a deliberate compromise: two *different* messages with the
same text at the same ``timestamp`` (e.g. two MQTT publishes in the same second)
would collide, so those channels should carry their own id in the future.
"""

from __future__ import annotations

import hashlib
from typing import Final

from octop_gateway.models import InboundMessage

__all__ = [
    "PLATFORM_MESSAGE_ID_KEYS",
    "build_idempotency_key",
    "extract_platform_message_id",
]

#: Channel kind -> ``InboundMessage.metadata`` key holding the platform id.
#: Channels absent from this table (``wecom``, ``mqtt``) expose no id at all.
PLATFORM_MESSAGE_ID_KEYS: Final[dict[str, str]] = {
    "feishu": "message_id",
    "telegram": "message_id",
    "discord": "message_id",
    "xiaoyi": "message_id",
    "qq": "msg_id",
    "dingtalk": "msg_id",
    "yuanbao": "msg_id",
    "weixin": "_weixin_msg_id",
}

#: Length of the truncated SHA-256 hex digest used by the fallback key.
_DIGEST_LENGTH: Final[int] = 32


def extract_platform_message_id(message: InboundMessage) -> str | None:
    """Return the channel's own message id, or ``None`` when it has none.

    Empty / whitespace-only values count as missing so the caller falls back to
    the content hash instead of producing a key such as ``"feishu:...:"`` that
    would deduplicate *every* id-less message of the channel.
    """
    metadata_key = PLATFORM_MESSAGE_ID_KEYS.get(message.channel_type)
    if metadata_key is None:
        return None
    value = (message.metadata or {}).get(metadata_key)
    if value is None:
        return None
    platform_message_id = str(value).strip()
    return platform_message_id or None


def _fallback_digest(message: InboundMessage) -> str:
    """Hash the stable parts of an id-less message into a dedup digest."""
    subject = message.channel_subject
    subject_id = subject.subject_id if subject is not None else ""
    source = "|".join((message.channel_id, subject_id, str(message.timestamp), message.text))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def build_idempotency_key(message: InboundMessage) -> str:
    """Build the dedup key the processor claims in ``inbound_dedup``."""
    platform_message_id = extract_platform_message_id(message)
    if platform_message_id is not None:
        return f"{message.channel_id}:{platform_message_id}"
    return f"{message.channel_id}:h:{_fallback_digest(message)}"
