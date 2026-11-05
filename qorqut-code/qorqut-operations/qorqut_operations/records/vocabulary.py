"""The neutral record vocabulary: item kinds, the Operations resource table each kind is stored in, and the document classification.

A leaf module (it imports nothing else from the package), so documents, Tasks and host lookups can all use it. A host
adds the resource tables of its own integrations to ``RESOURCE_TYPES`` in its lookup; they are not part of this vocabulary."""

ITEM_TYPES = ("person", "topic", "task", "table", "agent", "material", "action", "signoff", "entity")
RESOURCE_TYPES = {"tasks": "task", "data_tables": "table", "agent_profiles": "agent",
                  "action_logs": "action", "signoffs": "signoff", "context_entities": "entity"}
RAW_KINDS = {"context_upload", "context_dump", "data_upload", "asset"}


def document_type(row):
    return {"context_topic": "topic", "context_person": "person"}.get(row["kind"], "material")
