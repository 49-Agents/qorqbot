"""Typed references, scoped identities and the recorded-source contract of the CRM domain.

A public reference always carries ``business_id`` (null is the Global scope): scope is never derived from a display name
or inferred from a missing field. Recorded sources are data. Nothing here fetches a URL, opens a path or contacts a
provider; a URL is only checked to be a plain http(s) link without embedded credentials.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from qorqut_operations.application.plans import IDENTITY, UUID, schema, timestamp
from qorqut_operations.contracts import DomainError

NULLABLE_BUSINESS = {"anyOf": [UUID, {"type": "null"}]}
RECORD_REF = schema({"type": {"const": "task"}, "id": IDENTITY, "business_id": NULLABLE_BUSINESS}, ("type", "id", "business_id"))
TABLE_REF = schema({"type": {"const": "table"}, "id": UUID, "business_id": NULLABLE_BUSINESS}, ("type", "id", "business_id"))

IDENTIFIER_LIMIT, URL_LIMIT, TITLE_LIMIT, EVENT_KEY_LIMIT = 500, 4000, 500, 300
EVENT_KINDS = ("email", "message", "development")
SOURCE_KINDS = ("email", "message", "external", "manual")
SOURCE_TEXT = {"type": "string", "minLength": 1, "maxLength": IDENTIFIER_LIMIT}
SOURCE = schema({"kind": {"enum": list(SOURCE_KINDS)}, "provider": SOURCE_TEXT, "account_key": SOURCE_TEXT,
                 "thread_id": SOURCE_TEXT, "message_id": SOURCE_TEXT,
                 "url": {"type": "string", "minLength": 1, "maxLength": URL_LIMIT},
                 "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LIMIT}}, ("kind",))
# An observation may not claim to have happened later than this beyond ordinary clock skew.
FUTURE_TOLERANCE = timedelta(minutes=5)


def namespace_of(business_id):
    return "global" if business_id is None else "business:" + business_id


def record_ref(business_id, task_id):
    return {"type": "task", "id": task_id, "business_id": business_id}


def table_ref(business_id, table_id):
    return {"type": "table", "id": table_id, "business_id": business_id}


def activity_id(kind, business_id, source_id):
    """The stable, globally unambiguous key of one activity event: its kind, the owning scope of its store and its identity."""
    return f"{kind}:{namespace_of(business_id)}:{source_id}"


def clean_text(value, label, limit):
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise DomainError("validation_error", f"{label} must be text of at most {limit} characters without control characters")
    return value


def safe_url(value):
    """A plain ``http``/``https`` link with a host, no embedded credentials and no whitespace or control characters.

    This mirrors the dashboard's safe-link rule. It is a data check only: the URL is stored and shown, never fetched."""
    clean_text(value, "A source URL", URL_LIMIT)
    if any(char.isspace() or char == "\\" for char in value):
        raise DomainError("validation_error", "A source URL cannot contain whitespace")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - an invalid port raises
    except ValueError as exc:
        raise DomainError("validation_error", "A source URL must be a valid http or https link") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise DomainError("validation_error", "A source URL must be an http or https link with a host")
    if "@" in parts.netloc:
        raise DomainError("validation_error", "A source URL cannot contain embedded credentials")
    return value


def source_checked(source):
    """The normalized recorded source: only the declared bounded fields, each cleaned; ``manual`` needs no provider data."""
    if not isinstance(source, Mapping) or source.get("kind") not in SOURCE_KINDS:
        raise DomainError("validation_error", "A recorded source names its kind")
    checked = {"kind": source["kind"]}
    for key in ("provider", "account_key", "thread_id", "message_id"):
        if source.get(key) is not None:
            checked[key] = clean_text(source[key], f"Source {key}", IDENTIFIER_LIMIT)
    if source.get("title") is not None:
        checked["title"] = clean_text(source["title"], "Source title", TITLE_LIMIT)
    if source.get("url") is not None:
        checked["url"] = safe_url(source["url"])
    return checked


def event_key_checked(value):
    return clean_text(value, "An event key", EVENT_KEY_LIMIT)


def observed_at(value, now):
    """The authentic observation time as a normalized UTC timestamp; it must be timezone-aware and not in the future."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        normalized = timestamp(lambda: parsed.astimezone(timezone.utc))
    except (ValueError, AttributeError, OverflowError) as exc:
        raise DomainError("validation_error", "occurred_at must be a timestamp with a timezone") from exc
    ceiling = datetime.fromisoformat(now.replace("Z", "+00:00")) + FUTURE_TOLERANCE
    if parsed.astimezone(timezone.utc) > ceiling:
        raise DomainError("validation_error", "occurred_at cannot be in the future")
    return normalized
