"""Format-2 input/output validation; meaning remains the caller's responsibility."""
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


def action_document(action_ref, revision_ref, action_text, related_objects, limit):
    utf8(action_text, "Action text", limit)
    document = {"action_ref": action_ref, "text": action_text, "pointers": related_objects}
    if revision_ref is not None:
        document["revision_ref"] = revision_ref
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
