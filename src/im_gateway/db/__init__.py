"""SQLite persistence layer (config + runtime data), see HANDOVER §13.2.

Gateway code should import :class:`Store` and the record types from here::

    from im_gateway.db import ChannelRecord, Store
"""

from im_gateway.db.models import (
    DIRECTIONS,
    ChannelRecord,
    MessageRecord,
    SubjectRecord,
)
from im_gateway.db.schema import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
)
from im_gateway.db.store import Store

__all__ = [
    "DIRECTIONS",
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "ChannelRecord",
    "MessageRecord",
    "Store",
    "SubjectRecord",
]
