"""Small shared helpers: hashing, JSON, YAML, identifiers and time."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Callable

import yaml

from .errors import InvalidRequestError

ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_ID_RE = re.compile(ID_PATTERN)
REF_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$"
_REF_RE = re.compile(REF_PATTERN)
OPERATION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
_OPERATION_ID_RE = re.compile(OPERATION_ID_PATTERN)
SHA256_PATTERN = r"^[0-9a-f]{64}$"

Clock = Callable[[], datetime]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    """Deterministic JSON text used for fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def json_digest(value: Any) -> str:
    return sha256_hex(canonical_json(value).encode("utf-8"))


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=False, ensure_ascii=False, allow_nan=False)


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID_RE.match(value) is not None


def is_ref(value: Any) -> bool:
    return isinstance(value, str) and _REF_RE.match(value) is not None


def split_ref(ref: str) -> tuple[str, str]:
    if not is_ref(ref):
        raise InvalidRequestError(f"Not an entry reference (package-id/entry-id): {ref!r}")
    package_id, entry_id = ref.split("/", 1)
    return package_id, entry_id


def check_operation_id(value: Any, field: str = "operation_id") -> str:
    if not isinstance(value, str) or not _OPERATION_ID_RE.match(value):
        raise InvalidRequestError(f"{field} must match {OPERATION_ID_PATTERN}", details={"field": field})
    return value


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys and keeps dates/times as strings."""


_StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "found an unhashable key", key_node.start_mark
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _check_json_compatible(value: Any, where: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{where}: mapping keys must be strings, found {key!r}")
            _check_json_compatible(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_compatible(item, f"{where}[{index}]")
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{where}: non-finite numbers are not valid JSON")
    elif value is None or isinstance(value, (str, int, bool)):
        return
    else:
        raise ValueError(f"{where}: unsupported YAML value of type {type(value).__name__}")


def load_yaml(text: str) -> Any:
    """Parse YAML strictly into JSON-compatible data; raise ``ValueError`` otherwise."""
    try:
        value = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    _check_json_compatible(value)
    return value


def dump_yaml(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(frontmatter, error)``. ``(None, None)`` means no frontmatter block."""
    if not (text.startswith("---\n") or text.startswith("---\r\n")):
        return None, None
    lines = text.splitlines()
    for index in range(1, len(lines)):
        if lines[index].rstrip() in ("---", "..."):
            block = "\n".join(lines[1:index])
            try:
                data = load_yaml(block)
            except ValueError as exc:
                return None, str(exc)
            if data is None:
                return {}, None
            if not isinstance(data, dict):
                return None, "frontmatter must be a mapping"
            return data, None
    return None, "frontmatter block is not closed"
