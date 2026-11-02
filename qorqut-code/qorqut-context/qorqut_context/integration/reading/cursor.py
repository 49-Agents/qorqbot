"""Opaque continuation tokens that pin one published revision.

A token carries the revision a paged read started at, the next offset and a binding hash of the operation and its
filters. Presenting it with different filters, another operation, a malformed body or a revision that is not a full
Git hash is a validation error; the token itself grants nothing, since every registered agent reads the same
published graph."""

from __future__ import annotations

import base64
import json
import re

from qorqut_operations.contracts import DomainError, canonical_hash

REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
MAX_OFFSET = 1_000_000
VERSION = 1


def binding(operation, scope) -> str:
    """The hash of what a token may be reused for: the operation and every argument except paging."""
    return canonical_hash({"operation": operation, "scope": scope})


def encode(operation, scope, revision, offset) -> str:
    payload = json.dumps({"v": VERSION, "b": binding(operation, scope), "r": revision, "o": offset},
                         separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii").rstrip("=")


def decode(token, operation, scope) -> tuple[str, int]:
    """The ``(revision, offset)`` a token for exactly this operation and scope pins."""
    try:
        if not isinstance(token, str) or len(token) > 1024 or not token.isascii():
            raise ValueError
        padded = token + "=" * (-len(token) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if (not isinstance(value, dict) or set(value) != {"v", "b", "r", "o"} or value["v"] != VERSION
                or value["b"] != binding(operation, scope) or not isinstance(value["r"], str)
                or REVISION.fullmatch(value["r"]) is None or type(value["o"]) is not int
                or not 0 <= value["o"] <= MAX_OFFSET):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise DomainError("validation_error", "Invalid or differently scoped Context cursor",
                          field_errors={"cursor": "invalid or differently scoped"}) from None
    return value["r"], value["o"]
