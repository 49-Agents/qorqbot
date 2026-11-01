"""Immutable document versions and append-only business activity for Qorqut hosts."""

from ._store import (
    MAX_PAGE_LIMIT,
    BusinessEvent,
    DocumentVersion,
    DocumentVersionInfo,
    EventConflict,
    EventPage,
    HistoryError,
    HistoryStore,
    NotFound,
    TransactionRequired,
    VersionConflict,
    VersionPage,
    initialize_schema,
)

__version__ = "0.1.0"

__all__ = [
    "MAX_PAGE_LIMIT",
    "BusinessEvent",
    "DocumentVersion",
    "DocumentVersionInfo",
    "EventConflict",
    "EventPage",
    "HistoryError",
    "HistoryStore",
    "NotFound",
    "TransactionRequired",
    "VersionConflict",
    "VersionPage",
    "initialize_schema",
]
