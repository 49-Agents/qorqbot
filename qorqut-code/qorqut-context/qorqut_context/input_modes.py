"""Format-2 input/output validation; meaning remains the caller's responsibility."""
import math
import re

from .schema import ContextError, encode, identifier, normalize_date, text


GENERAL = "GENERAL"
ACTION = "OPERATIONAL_ACTION"
SOURCE_BYTES = 256 * 1024


def utf8(value, name, limit=SOURCE_BYTES):
    text(value, name, nonempty=False)
    try:
        content = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContextError(f"{name} must be valid UTF-8") from error
    if len(content) > limit:
        raise ContextError(f"{name} exceeds the configured byte limit")
    return content


def opaque_ref(value, name):
    text(value, name)
    utf8(value, name, 256)
    return value


def pointers(values):
    if not isinstance(values, list):
        raise ContextError("Related-object pointers must be a list")
    result = []
    for item in values:
        if (not isinstance(item, dict) or not {"kind", "id"} <= item.keys() or
                set(item) - {"kind", "id", "explanation"} or item["kind"] not in ("node", "edge")):
            raise ContextError("A pointer requires kind node/edge, id, and optional explanation")
        entry = {"kind": item["kind"], "id": identifier(item["id"])}
        if "explanation" in item:
            entry["explanation"] = text(item["explanation"], "pointer explanation", nonempty=False)
        result.append(entry)
    return result


def tag_snapshots(values):
    """Validate revision metadata without consulting a mutable tag catalog or graph."""
    if not isinstance(values, list):
        raise ContextError("Tags must be a list of revision snapshots")
    result, seen = [], set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"id", "name", "business_id"}:
            raise ContextError("A tag snapshot requires id, name, and business_id")
        tag_id = opaque_ref(item["id"], "tag id")
        name = text(item["name"], "tag name")
        utf8(name, "tag name")
        business_id = item["business_id"]
        if business_id is not None:
            opaque_ref(business_id, "tag business_id")
        if tag_id in seen:
            raise ContextError("Duplicate tag id")
        seen.add(tag_id)
        result.append({"id": tag_id, "name": name, "business_id": business_id})
    return result


def _json_value(value, limit):
    """A deep copy of ``value`` if it is strictly JSON-compatible; nothing is converted or stringified."""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ContextError("record_context numbers must be finite JSON numbers")
        return value
    if type(value) is str:
        utf8(value, "record_context text", limit)
        return value
    if type(value) is list:
        return [_json_value(member, limit) for member in value]
    if type(value) is dict:
        result = {}
        for key, member in value.items():
            if type(key) is not str:
                raise ContextError("record_context object keys must be strings")
            utf8(key, "record_context key", limit)
            result[key] = _json_value(member, limit)
        return result
    raise ContextError("record_context may contain only JSON-compatible values")


def record_context(value, limit=SOURCE_BYTES):
    """Trusted caller evidence ``{scope, provenance}``; it grants no permission, ownership or write capability.

    ``None`` means no supplied record context. The canonical serialization obeys the source byte limit.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"scope", "provenance"}:
        raise ContextError("record_context requires exactly scope and provenance")
    scope = value["scope"]
    if not isinstance(scope, dict):
        raise ContextError("record_context scope must be an object")
    if scope.get("kind") == "global" and set(scope) == {"kind"}:
        normalized_scope = {"kind": "global"}
    elif scope.get("kind") == "business" and set(scope) == {"kind", "business_id"}:
        normalized_scope = {"kind": "business", "business_id": opaque_ref(scope["business_id"], "record_context business_id")}
    else:
        raise ContextError("record_context scope must be {kind: 'global'} or {kind: 'business', business_id}")
    if not isinstance(value["provenance"], dict):
        raise ContextError("record_context provenance must be a JSON object")
    try:
        result = {"scope": normalized_scope, "provenance": _json_value(value["provenance"], limit)}
        size = len(encode(result))
    except RecursionError as error:
        raise ContextError("record_context is nested too deeply") from error
    if size > limit:
        raise ContextError("record_context exceeds the configured byte limit")
    return result


def full_commit(value, name):
    """A full Git commit hash; never ``self``, a branch name or another revision expression."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ContextError(f"{name} must be a full published commit")
    return value


def action_document(action_ref, revision_ref, action_text, related_objects, tags, limit, record_context=None):
    utf8(action_text, "Action text", limit)
    document = {"action_ref": action_ref, "text": action_text, "pointers": related_objects,
                "tags": tags}
    if revision_ref is not None:
        document["revision_ref"] = revision_ref
    if record_context is not None:
        document["record_context"] = record_context
    try:
        content = encode(document)
    except UnicodeEncodeError as error:
        raise ContextError("Action evidence must be valid UTF-8") from error
    if len(content) > limit:
        raise ContextError("Serialized action source exceeds the configured byte limit")
    return content


def handoff_note(value, limit):
    utf8(value, "Handoff note", limit)
    return {"disposition": "prepared" if value else "empty", "text": value}


def action_associations(date, summary, associations):
    text(summary, "Action summary")
    if len(summary.split()) > 150:
        raise ContextError("Action summary may have at most 150 words")
    if not isinstance(associations, list):
        raise ContextError("Associations must be a list")
    result, seen = [], set()
    for item in associations:
        if (not isinstance(item, dict) or set(item) != {"kind", "id", "explanation"} or
                item["kind"] not in ("node", "edge")):
            raise ContextError("An association requires kind node/edge, id, and explanation")
        key = (item["kind"], identifier(item["id"]))
        explanation = text(item["explanation"], "Association explanation")
        if len(explanation.split()) > 150:
            raise ContextError("Association explanation may have at most 150 words")
        if key in seen:
            raise ContextError("Duplicate action association")
        seen.add(key)
        result.append({**item, "citations": ["self"]})
    return {"date": normalize_date(date), "summary": summary,
            "associations": sorted(result, key=lambda item: (item["kind"], item["id"]))}


def collection_page(items, offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ContextError("offset must be nonnegative and limit between 1 and 100")
    selected = items[offset:offset + limit]
    more = offset + len(selected) < len(items)
    return {"items": selected, "offset": offset, "limit": limit, "total": len(items),
            "has_more": more, "next_offset": offset + len(selected) if more else None}
