"""Small, explicit validators for the accepted context format."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import PurePosixPath
import re


class ContextError(Exception):
    """An invalid request or context operation; published data is retained."""


class Conflict(ContextError):
    """An event, revision, identifier, or writer conflicts with this request."""


def encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]{5}", value):
        raise ContextError("Context IDs must have five lowercase letters/digits")
    return value


def text(value, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ContextError(f"{name} must be {'nonempty ' if nonempty else ''}text")
    return value


def value_key(value) -> str:
    # Python's True == 1 must not collapse distinct attribute values.
    return type(value).__name__ + ":" + json.dumps(value, ensure_ascii=False,
                                                   allow_nan=False)


def attributes(values, *, node: bool = False) -> dict:
    if not isinstance(values, dict):
        raise ContextError("attributes must be an object of lists")
    result = {}
    for name, members in values.items():
        text(name, "attribute name")
        if not isinstance(members, list):
            raise ContextError(f"{name} must be a list")
        found, normalized = set(), []
        for value in members:
            if type(value) not in (str, int, float, bool):
                raise ContextError("Attribute members must be text, numbers, or booleans")
            if type(value) is float and not math.isfinite(value):
                raise ContextError("Numbers must be finite JSON numbers")
            key = value_key(value)
            if key not in found:
                found.add(key)
                normalized.append(value)
        if normalized:
            result[name] = normalized
    if node:
        categories = result.get("basic_entity_type", [])
        if not categories or any(not isinstance(v, str) or not v.strip() for v in categories):
            raise ContextError("basic_entity_type requires nonempty category strings")
    return result


def object_record(kind: str, value: dict) -> dict:
    if kind not in ("node", "edge") or not isinstance(value, dict):
        raise ContextError("Expected a node or edge record")
    fields = {"id", "summary", "attributes"}
    if kind == "edge":
        fields |= {"from_id", "to_id"}
    if set(value) != fields:
        raise ContextError(f"{kind} fields must be {sorted(fields)}")
    result = {"id": identifier(value["id"]),
              "summary": text(value["summary"], "summary", nonempty=False),
              "attributes": attributes(value["attributes"], node=kind == "node")}
    if kind == "edge":
        result.update(from_id=identifier(value["from_id"]), to_id=identifier(value["to_id"]))
    return result


def normalize_date(value, *, unknown: bool = True):
    if type(value) is int and value == 0 and unknown:
        return 0
    if not isinstance(value, str):
        raise ContextError("A date must be ISO text, or 0 for an unknown development")
    match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", value)
    try:
        if match:
            year, month, day = match.groups()
            parsed = datetime(int(year), int(month or 1), int(day or 1), tzinfo=timezone.utc)
        else:
            # datetime.fromisoformat fills omitted smaller time components.
            if not re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}", value):
                raise ValueError("Expected ISO calendar/time notation")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ContextError(f"Invalid date: {value}") from error
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds").replace("+00:00", "Z")


def source_path(path: str) -> str:
    text(path, "source path")
    parts = path.split("/")
    if (len(parts) < 2 or parts[0] != "foundation" or
            any(p in ("", ".", "..", ".git") for p in parts) or
            "\\" in path or "\x00" in path or
            PurePosixPath(path).suffix.lower() not in (".md", ".txt")):
        raise ContextError("Source paths must stay under foundation/ and end in .md or .txt")
    return path


def metadata(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ContextError("source metadata must be an object")
    required = {"kind", "title", "origin"}
    if not required <= set(value) or set(value) - required - {"origin_ref", "created_at"}:
        raise ContextError("Metadata requires kind/title/origin and optional origin_ref/created_at")
    result = {key: text(value[key], key) for key in required}
    if "origin_ref" in value:
        result["origin_ref"] = text(value["origin_ref"], "origin_ref")
    if "created_at" in value:
        result["created_at"] = normalize_date(value["created_at"], unknown=False)
    return result


def references(values, *, limited: bool = False, required: bool = False) -> list[str]:
    if not isinstance(values, list):
        raise ContextError("References must be a list")
    result = []
    for value in values:
        if not isinstance(value, str) or (value != "self" and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)):
            raise ContextError("Evidence references must be full Git hashes or event-local self")
        if value not in result:
            result.append(value)
    if required and not result:
        raise ContextError("An object change must cite supporting source events")
    if limited and len(result) > 15:
        raise ContextError("An explanation may have at most 15 citations")
    return result


def change_details(explanation, citations, support_after, developments) -> dict:
    explanation = text(explanation, "explanation")
    if len(explanation.split()) > 150:
        raise ContextError("An explanation may have at most 150 words")
    if not isinstance(developments, list):
        raise ContextError("developments must be a list")
    normalized = []
    for item in developments:
        if not isinstance(item, dict) or set(item) != {"date", "description", "citations"}:
            raise ContextError("A development requires date, description, and citations")
        normalized.append({"date": normalize_date(item["date"]),
                           "description": text(item["description"], "development description"),
                           "citations": references(item["citations"], limited=True, required=True)})
    return {"explanation": explanation,
            "citations": references(citations, limited=True, required=True),
            "support_after": references(support_after), "developments": normalized}
