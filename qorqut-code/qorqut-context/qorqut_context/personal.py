"""The installation owner's canonical person node; attributes remain ordinary Context data."""
from __future__ import annotations

from .schema import ContextError


def initialize(context):
    """Called only by fresh initialization, before the installation serves readers.

    Seed through the ordinary event machinery so even the blank person has valid
    source support and a Global creation scope. No historical profile is inferred.
    """
    from . import graph_edits
    from .git_store import CONFIG_PATH, CURRENT_CONFIG
    from .schema import encode

    snapshot = context.snapshot()
    if snapshot.personal_context_node is not None or snapshot.nodes or snapshot.events:
        raise ContextError("Personal context is seeded only during fresh initialization")
    node_id = context._allocate(snapshot.used_ids)
    event_id = context._allocate(snapshot.used_ids | {node_id})
    explanation = "Provision the installation owner's blank canonical person node; no personal facts have been supplied."
    operation = {"op": "put_node", "record": {
        "id": node_id, "summary": "Installation owner", "attributes": {"basic_entity_type": ["person"]}},
        "explanation": explanation, "citations": ["self"], "support_after": ["self"]}
    plan = {"format": graph_edits.FORMAT, "expected_revision": snapshot.revision,
            "rationale": explanation, "operations": [operation]}
    context.begin_edit(plan, submitted_by="system:personal-context-initialization", event_id=event_id,
                       record_context={"scope": {"kind": "global"},
                                       "provenance": {"operation": "personal_context_initialize"}})
    graph_edits.stage(context, event_id, operation)
    context.stage_handoff_note(event_id, "")
    context.publish(event_id)
    with context._lock():
        before = context.snapshot().revision
        commit = context.git.commit({CONFIG_PATH: encode({**CURRENT_CONFIG, "personal_context_node": node_id})},
                                    context.runtime, before, "Identify the canonical personal context node")
        context.git.publish(commit, before)
        context.git.checkout(before, commit)
        context._ensure_index(context.snapshot())


def touches(node_id, kind, object_id, before, after):
    """A profile edit includes changes to either end of its relationships."""
    if node_id is None:
        return False
    if kind == "node":
        return object_id == node_id
    return kind == "edge" and any(
        record and node_id in (record["from_id"], record["to_id"])
        for record in (before, after))


def edits_profile(snapshot, plan):
    affected = False
    for operation in plan["operations"]:
        verb, kind = operation["op"].split("_", 1)
        after = operation.get("record")
        object_id = after["id"] if after is not None else operation["id"]
        before = snapshot.records(kind).get(object_id)
        if touches(snapshot.personal_context_node, kind, object_id, before, after):
            if kind == "node" and verb == "remove":
                raise ContextError("The canonical personal context node cannot be removed")
            affected = True
    return affected


def validate_change(snapshot, kind, object_id, before, after, *, controlled_edit):
    if not touches(snapshot.personal_context_node, kind, object_id, before, after):
        return
    if kind == "node":
        if after is None:
            raise ContextError("The canonical personal context node cannot be removed")
        if "person" not in after["attributes"].get("basic_entity_type", []):
            raise ContextError("The canonical personal context node must remain a person")
    if not controlled_edit:
        raise ContextError("Personal context is maintained by Ordubashy through context_edit")
