"""Material classification, independent of file layout and access policy."""

MATERIAL_TYPES = ("conversation", "document", "excerpt", "post", "note", "mixed", "content")
ENCOUNTER_ORIGINS = ("call", "meeting")
DOCUMENT_KINDS = {"context_note", "context", "pillar", "content", "context_upload", "context_dump", "email_message"}
RAW_KINDS = {"context_upload", "context_dump"}
TAG = {"type": "string", "minLength": 1, "maxLength": 100, "pattern": r"\S"}
TAGS = {"type": "array", "items": TAG, "maxItems": 50, "uniqueItems": True}
FILTERS = {"material_type": {"enum": list(MATERIAL_TYPES)}, "origin": {"type": "string", "maxLength": 100}, "tag": TAG}


def classification(row, resource="documents"):
    if resource != "documents" or row.get("kind") not in DOCUMENT_KINDS or row.get("owning_agent_id") \
            or not (row.get("business_id") or row.get("namespace") == "global"):
        return {}
    kind, meta = row["kind"], row.get("metadata_json", {})
    default_type = "mixed" if kind in RAW_KINDS else "content" if kind == "content" else "conversation" if kind == "email_message" else "document"
    origin = "upload" if kind in RAW_KINDS or any(source.get("resource_type") == "documents" for source in row.get("source_refs_json", [])) else "email" if kind == "email_message" else "authored"
    return {"material_type": meta.get("material_type", default_type), "origin": meta.get("origin", origin), "tags": list(meta.get("tags", []))}


def matches(value, args):
    return (all(value.get(key) == args[key] for key in ("material_type", "origin") if key in args)
            and ("tag" not in args or args["tag"] in value.get("tags", [])))
