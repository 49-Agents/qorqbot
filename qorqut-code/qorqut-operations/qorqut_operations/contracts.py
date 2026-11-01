"""Transport-independent values; no storage or runtime initialization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import secrets
import string
from typing import Any, Literal
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid4())


def new_agent_id() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def _plain(value: Any) -> Any:
    """Adapted from bazarlyk.contracts: reject ambiguous JSON representations."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)
                if not f.name.startswith("_")}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.details = _plain(details) if details is not None else None

    def to_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details is not None:
            result["details"] = _plain(self.details)
        return result


@dataclass(frozen=True)
class Actor:
    id: str
    kind: Literal["owner", "agent", "system"]
    native_session_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Actor id must be a nonempty string")
        if self.kind not in ("owner", "agent", "system"):
            raise ValueError("Actor kind must be owner, agent or system")
        if self.native_session_id is not None and (
            not isinstance(self.native_session_id, str) or not self.native_session_id.strip()
        ):
            raise ValueError("Native session id must be a nonempty string when supplied")
