"""The frozen source document a filing adapter submits to Context: one canonical UTF-8 JSON object.

``{"format": "qorqut-source-v1", "body": <exact text>, "metadata": <explicit object>}``. The adapter chooses the body and
the metadata it wants Context to see (an explicit allowlist its own code documents); this module owns only the encoding, so
an admission and a later fingerprint check of the same current source produce byte-identical text. The body is kept exactly
as supplied; nothing is truncated, normalized or inferred, and a source that does not fit the Context byte limit is refused
before admission with an error that says so.

The digest is the SHA-256 of those exact bytes. It is the source identity of one accepted version and the thing a completion
compares against the current source, so a change to the body or to any selected metadata value is a different source, while
a field the adapter did not select (processing status, receipts, attempts, row revisions, timestamps of bookkeeping) cannot
affect it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_bytes

FORMAT = "qorqut-source-v1"
_KEYS = frozenset({"format", "body", "metadata"})


def source_document(body: Any, metadata: Any) -> str:
    """The canonical text of one source. ``body`` must be text and ``metadata`` a JSON object."""
    if not isinstance(body, str):
        raise DomainError("validation_error", "A source body must be text")
    if not isinstance(metadata, dict):
        raise DomainError("validation_error", "Source metadata must be an object")
    try:
        text = canonical_bytes({"format": FORMAT, "body": body, "metadata": metadata}).decode("utf-8")
        text.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise DomainError("validation_error", "The source must be valid UTF-8 text with plain JSON metadata") from error
    return text


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint_of(body: Any, metadata: Any) -> str:
    """The digest of the source ``body`` and ``metadata`` would be admitted as."""
    return fingerprint(source_document(body, metadata))


def check_size(text: str, limit: int | None) -> int:
    size = len(text.encode("utf-8"))
    if limit is not None and size > limit:
        raise DomainError("source_too_large",
                          f"The source is {size} bytes with its metadata envelope, over the Context limit of {limit} bytes; "
                          "split or shorten it before filing", details={"size": size, "limit": limit})
    return size


def parse(text: str) -> dict:
    """The decoded source, checked to be exactly what ``source_document`` writes."""
    try:
        value = json.loads(text)
    except ValueError as error:
        raise DomainError("validation_error", "The stored source is not valid JSON") from error
    if (not isinstance(value, dict) or set(value) != _KEYS or value["format"] != FORMAT
            or source_document(value["body"], value["metadata"]) != text):
        raise DomainError("validation_error", "The stored source is not a canonical qorqut-source-v1 document")
    return value
