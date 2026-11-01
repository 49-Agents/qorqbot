"""Action vocabulary and validation: the retained v1 text, category and evidence rules, and Context pointers."""

from __future__ import annotations

import re
import uuid

from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.actions.schema import CATEGORIES, ORIGINS

GLOBAL_AGENT_CATEGORIES = ("global-operational", "business-operational")
AGENT_WORD_LIMIT = 20
TEXT_LIMIT = 2000
EVIDENCE_KEYS = ("url", "commit", "pull_request", "signoff", "task", "document", "request", "conversation", "upload")
EVIDENCE = {**schema({key: {"type": "string", "minLength": 1, "maxLength": 2000} for key in EVIDENCE_KEYS}),
            "description": "Optional typed evidence of the result (URL, commit, pull request or record IDs)."}
ACTION_TEXT = {"type": "string", "minLength": 1, "maxLength": TEXT_LIMIT}
POINTER_ID = re.compile(r"^[a-z0-9]{5}$")
RELATED_OBJECTS = {"type": "array", "maxItems": 50, "items": {
    **schema({"kind": {"enum": ["node", "edge"]}, "id": {"type": "string", "pattern": "^[a-z0-9]{5}$"},
              "explanation": {"type": "string", "maxLength": 2000}}, ("kind", "id")),
    "description": "A suggested Context object this Action relates to. Pointers are suggestions, never required graph subjects."}}
SOURCE_NAMESPACE = uuid.UUID("3f0b7c52-5a3c-4f7e-9d6b-2f1a0e8c4b71")
SHARING = ("A business-operational action names one or more businesses; each named business sees the same shared "
           "text once, and scoped reads never reveal other businesses or unrelated evidence.")


def invalid(message, **fields):
    raise DomainError("validation_error", message, **({"field_errors": fields} if fields else {}))


def action_key(storage, identity) -> str:
    return "global/" + identity if storage is None else "business/" + storage + "/" + identity


def source_identity(storage, source_key: str) -> str:
    """The stable Action id of an automatic source, so a retried confirmed source never records twice."""
    return str(uuid.uuid5(SOURCE_NAMESPACE, ("" if storage is None else storage) + ":" + source_key))


def word_count(text) -> int:
    return len(text.split())


def one_line(text, limit=200) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def concise(text, limit=AGENT_WORD_LIMIT) -> str:
    """Generated automatic text: at most ``limit`` words, full detail stays in evidence."""
    words = text.split()
    return " ".join(words) if len(words) <= limit else " ".join(words[:limit - 1]) + " …"


def origin_for(ctx) -> str:
    return {"agent": "agent", "human": "manual"}.get(ctx.principal_kind, "automatic")


def validate_text(text, origin, field="text") -> str:
    """The Action text: not blank, within the limit, and at most 20 words when an agent records it."""
    if not isinstance(text, str) or not text.strip():
        invalid("Action text must not be blank", **{field: "blank"})
    text = text.strip()
    if len(text) > TEXT_LIMIT:
        invalid(f"Action text is limited to {TEXT_LIMIT} characters", **{field: "too long"})
    if origin == "agent" and word_count(text) > AGENT_WORD_LIMIT:
        invalid(f"Agent actions are limited to {AGENT_WORD_LIMIT} words; state the result concisely", **{field: "more than 20 words"})
    return text


def related_objects(values) -> list[dict]:
    """Context pointers: ``{kind: node|edge, id: 5 lowercase letters/digits, explanation?}``, each once, order kept."""
    if not isinstance(values, (list, tuple)):
        invalid("related_objects must be a list")
    found, seen = [], set()
    for value in values:
        if (not isinstance(value, dict) or not {"kind", "id"} <= value.keys() or set(value) - {"kind", "id", "explanation"}
                or value["kind"] not in ("node", "edge")):
            invalid("A related object requires kind node/edge, id and an optional explanation")
        if not isinstance(value["id"], str) or not POINTER_ID.fullmatch(value["id"]):
            invalid("Context object IDs have five lowercase letters or digits")
        if "explanation" in value and not isinstance(value["explanation"], str):
            invalid("A related object explanation is text")
        if (value["kind"], value["id"]) in seen:
            invalid("Use distinct related objects")
        seen.add((value["kind"], value["id"]))
        found.append({key: value[key] for key in ("kind", "id", "explanation") if key in value})
    return found


REF_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def refs(values) -> dict:
    """Named string references kept with an Action (typed evidence, the linked task, delegation provenance, source facts)."""
    if not isinstance(values, dict) or any(not isinstance(key, str) or not REF_KEY.fullmatch(key) or not isinstance(value, str)
                                           or not value or len(value) > 4000 for key, value in values.items()):
        invalid("Action references must be named non-empty strings")
    return dict(sorted(values.items()))


def snapshot(row, *, businesses, tags, operation) -> dict:
    """The exact accepted state of one revision, from the Action row after the change. Stored immutably.

    ``tags`` are the ``{id, name, business_id}`` snapshots (stable catalog id and display label; business_id is always
    None because ordinary tags are shared labels). They are frozen here so nothing later consults the catalog."""
    return {"text": row["action_text"], "category": row["category"], "origin": row["origin"], "tag_snapshots": list(tags),
            "related_objects": row["related_objects_json"], "business_ids": sorted(businesses), "refs": row["refs_json"],
            "occurred": {"at": row["occurred_at"], "date": row["occurred_date"], "hour": row["occurred_hour"],
                         "precision": row["time_precision"], "time_zone": row["time_zone"]},
            "withdrawn": operation == "remove"}


__all__ = ["ACTION_TEXT", "AGENT_WORD_LIMIT", "CATEGORIES", "EVIDENCE", "EVIDENCE_KEYS", "GLOBAL_AGENT_CATEGORIES", "ORIGINS",
           "REF_KEY", "RELATED_OBJECTS", "SHARING", "TEXT_LIMIT", "action_key", "concise", "invalid", "one_line", "origin_for",
           "refs", "related_objects", "snapshot", "source_identity", "validate_text", "word_count"]
