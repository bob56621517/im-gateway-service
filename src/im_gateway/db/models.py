"""Typed row objects for the SQLite persistence layer (HANDOVER §13.2).

These are deliberately plain frozen dataclasses rather than pydantic models:
the persistence layer must not depend on the wire contract, and callers
(gateway / CLI) are free to build records by hand or map them from pydantic.

Conventions:

- JSON columns are exposed as plain Python values (``dict`` / ``list``);
  serialization to ``*_json`` TEXT columns happens inside :class:`Store`.
- Timestamps are UNIX epoch seconds (``REAL``).  They are supplied by the
  caller; the store never invents them for record-based writes.
- ``enabled`` is a real ``bool`` here and stored as ``INTEGER`` in SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "DIRECTIONS",
    "BackendRecord",
    "ChannelRecord",
    "MessageRecord",
    "SubjectRecord",
]

#: ``messages.direction`` accepts exactly these two values (HANDOVER §13.2).
DIRECTIONS: Final[frozenset[str]] = frozenset({"in", "out"})


@dataclass(frozen=True, slots=True)
class ChannelRecord:
    """One row of ``channels`` — a single running channel instance."""

    channel_id: str
    kind: str
    tenant_id: str | None
    alias: str | None
    name: str
    config: dict[str, Any]
    constraints: dict[str, Any]
    enabled: bool
    created_at: float
    updated_at: float
    last_started_at: float | None = None


@dataclass(frozen=True, slots=True)
class BackendRecord:
    """One row of ``backends`` — an agent backend webhook destination.

    ``tenant_id == ""`` is the default single-backend deployment.
    """

    tenant_id: str
    base_url: str
    token_ref: str
    timeout_ms: int
    enabled: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class SubjectRecord:
    """One row of ``subjects`` — a known push target (outbound address book)."""

    channel_id: str
    subject_id: str
    chat_type: str
    display_name: str
    metadata: dict[str, Any]
    first_seen: float
    last_seen: float


@dataclass(frozen=True, slots=True)
class MessageRecord:
    """One row of ``messages`` — inbound message or final outbound content.

    ``direction`` is ``"in"`` or ``"out"``.  Delta fragments are not stored
    here; they belong to ``turn_events`` (diagnostics only).
    """

    channel_id: str
    subject_id: str
    session_key: str
    direction: str
    kind: str
    content: list[dict[str, Any]]
    metadata: dict[str, Any]
    platform_msg_id: str | None
    ts: float
    error: str | None
    turn_id: str | None = None
