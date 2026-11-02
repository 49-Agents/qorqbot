"""Closed projections and item-only selectors for the general history snapshot."""
from qorqut_operations.history_exports import Arr, Boolean, Integer, Obj, StructuredField, Text, declared_json


def action_snapshot():
    schema = Obj(fields={
        "text": Text(), "category": Text(nullable=True), "origin": Text(), "withdrawn": Boolean(),
        "business_ids": Arr(item=Text(), max_items=10000),
        "tag_snapshots": Arr(item=Obj(fields={"id": Text(), "name": Text(), "business_id": Text(nullable=True)}), max_items=10000),
        "occurred": Obj(fields={"at": Text(), "date": Text(nullable=True), "hour": Integer(nullable=True),
                                "precision": Text(), "time_zone": Text()}),
    })
    return StructuredField(schema, declared_json(schema))


def attribution():
    # Historical comments can have only migration provenance, without a relay
    # kind. Preserve that absence rather than inventing an attribution.
    schema = Obj(fields={"kind": Text(nullable=True), "owner_id": Text(nullable=True), "agent_id": Text(nullable=True),
                         "statement_text": Text(nullable=True), "report_text": Text(nullable=True)})
    return StructuredField(schema, declared_json(schema))


# Closed list: a future event source does not automatically export a private
# message summary. Delivery state is filtered through the exact same event set.
ITEM_SOURCES = ("task", "tasks", "action", "action_logs", "signoff", "signoffs", "milestone", "milestones",
                "table", "data_tables", "document", "documents", "routine", "routines")
MARKS = ",".join("?" for _ in ITEM_SOURCES)


def item_inbox(uow, request):
    return request.select(uow, f"source_type IN ({MARKS})", ITEM_SOURCES)


def item_deliveries(uow, request):
    return request.select(uow, f"event_id IN (SELECT id FROM inbox_events WHERE source_type IN ({MARKS}))", ITEM_SOURCES)


def item_subscriptions(uow, request):
    return request.select(uow, f"source_type IN ({MARKS})", ITEM_SOURCES)
