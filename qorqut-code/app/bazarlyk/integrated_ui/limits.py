"""Explicit map and value size limits, and the strict argument vocabulary they check.

These are implementation limits of this feature, not a storage product: an oversize request fails before anything is
written and is never truncated. ``request`` validates a whole commit at once (counts, key spelling, finite JSON, the
per-request and per-value limits); the other helpers validate one field of another operation. Every refusal is the
stable ``invalid_argument`` domain error (or the registry's ``validation_error`` for a schema mismatch), so a caller
can tell a bad request from a revision conflict.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_bytes, plain

KIB = 1024
MAX_SOURCE_BYTES = 5 * KIB * KIB           # one accepted executable index.html
MAX_VALUE_BYTES = 256 * KIB                # one saved JSON value
MAX_REQUEST_BYTES = 1024 * KIB             # one mutation request
MAX_CHANGED_KEYS = 100                     # keys set or removed by one commit
MAX_VIEW_BYTES = 16 * KIB                  # one saved presentation state
DEFAULT_PAGE = 50
MAX_PAGE = 100
MAX_KEY_CHARS = 200
MAX_TITLE_CHARS = 200
MAX_EVENT_TYPE_CHARS = 120
MAX_NOTIFICATION_KEY_CHARS = 300


def invalid(message: str, *, field: str | None = None, **details: Any) -> DomainError:
    return DomainError("invalid_argument", message,
                       field_errors={field: message} if field else None,
                       details=details or None)


def text(value: Any, *, field: str, maximum: int) -> str:
    """One non-blank single-line text field: no control characters, at most ``maximum`` characters."""
    if not isinstance(value, str):
        raise invalid("This value must be text", field=field)
    stripped = value.strip()
    if not stripped:
        raise invalid("This value cannot be blank", field=field)
    if len(value) > maximum:
        raise invalid(f"This value must be at most {maximum} characters", field=field)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise invalid("This value cannot contain control characters", field=field)
    return stripped


def title(value: Any) -> str:
    return text(value, field="title", maximum=MAX_TITLE_CHARS)


def event_type(value: Any, *, field: str = "type") -> str:
    return text(value, field=field, maximum=MAX_EVENT_TYPE_CHARS)


def data_key(value: Any) -> str:
    """A data key is an opaque, short, printable string (``person/123``, ``settings``), never a path or expression."""
    if not isinstance(value, str) or not value:
        raise invalid("A data key must be one non-empty string", field="key")
    if len(value) > MAX_KEY_CHARS:
        raise invalid(f"A data key must be at most {MAX_KEY_CHARS} characters", field="key")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise invalid("A data key cannot contain control characters", field="key")
    return value


def notification_key(value: Any) -> str:
    return text(value, field="notification_key", maximum=MAX_NOTIFICATION_KEY_CHARS)


def json_value(value: Any, *, field: str) -> Any:
    """A finite JSON value within the per-value limit; the exact plain value that will be stored."""
    try:
        normalized = plain(value)
        encoded = canonical_bytes(normalized)
    except (ValueError, TypeError) as exc:
        raise invalid("This value is not finite JSON", field=field) from exc
    if len(encoded) > MAX_VALUE_BYTES:
        raise invalid(f"One saved value must be at most {MAX_VALUE_BYTES} bytes", field=field)
    return normalized


def event_data(value: Any, *, field: str = "data") -> Any:
    """The optional data of a semantic event: finite JSON within the same per-value bound."""
    if value is None:
        return None
    return json_value(value, field=field)


def view_state(value: Any) -> dict:
    """Presentation preferences: a bounded JSON object, never a list or a scalar."""
    if not isinstance(value, Mapping):
        raise invalid("View state must be a JSON object", field="state")
    try:
        normalized = plain(value)
        encoded = canonical_bytes(normalized)
    except (ValueError, TypeError) as exc:
        raise invalid("View state is not finite JSON", field="state") from exc
    if len(encoded) > MAX_VIEW_BYTES:
        raise invalid(f"View state must be at most {MAX_VIEW_BYTES} bytes", field="state")
    return normalized


def page_limit(arguments: Mapping[str, Any]) -> int:
    value = arguments.get("limit", DEFAULT_PAGE)
    if type(value) is not int or not 1 <= value <= MAX_PAGE:
        raise invalid(f"limit must be between 1 and {MAX_PAGE}", field="limit")
    return value





def commit(arguments: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The normalized ``set`` map and ``remove`` list of one commit, with every limit already enforced.

    Nothing has been written: an oversize request, an unknown key spelling, a non-finite value or too many changed
    keys refuses the whole commit before the domain prepares any effect.
    """
    values = arguments.get("set") or {}
    removed = arguments.get("remove") or []
    if not isinstance(values, Mapping):
        raise invalid("set must be a map of keys to complete values", field="set")
    if not isinstance(removed, (list, tuple)):
        raise invalid("remove must be a list of keys", field="remove")
    normalized = {}
    for key, value in values.items():
        normalized[data_key(key)] = json_value(value, field=f"set.{key}")
    keys = [data_key(key) for key in removed]
    if len(set(keys)) != len(keys):
        raise invalid("remove names a key twice", field="remove")
    overlap = sorted(set(normalized) & set(keys))
    if overlap:
        raise invalid("A commit cannot both set and remove the same key", field="remove",
                      keys=overlap[:10])
    if len(normalized) + len(keys) > MAX_CHANGED_KEYS:
        raise invalid(f"One commit changes at most {MAX_CHANGED_KEYS} keys", field="set")
    request_size(arguments)
    return normalized, keys


def request_size(arguments: Mapping[str, Any]) -> None:
    """Refuse a mutation request whose complete arguments exceed the per-request limit."""
    try:
        encoded = canonical_bytes(plain(dict(arguments)))
    except (ValueError, TypeError) as exc:
        raise invalid("The request is not finite JSON") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise invalid(f"One mutation request must be at most {MAX_REQUEST_BYTES} bytes")


def observed(value: Any, *, current: int) -> int:
    """A revision the caller observed: it must already exist and may not be in the future."""
    if type(value) is not int or value < 1:
        raise invalid("The observed revision must be a positive integer", field="revision")
    if value > current:
        raise invalid("The observed revision does not exist yet", field="revision",
                      current_revision=current)
    return value