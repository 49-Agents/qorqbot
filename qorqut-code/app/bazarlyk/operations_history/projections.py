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


def public_signoff_notes(uow, request):
    """Discussion on a personal WhatsApp send quotes its exact recipient or text; it stays in the authoritative Signoff
    notes and is never exported to the operational Git snapshot."""
    return request.select(uow, "signoff_id NOT IN (SELECT id FROM signoffs WHERE operation_name = ?)", ("whatsapp_send",))


def public_message_attributions(uow, request):
    """Export attribution only for the Signoff notes already included in general history.

    Agent DMs, private owner conversations and personal WhatsApp-send notes stay excluded.
    """
    return request.select(uow, "message_type = 'signoff' AND message_id IN ("
        "SELECT id FROM signoff_notes WHERE signoff_id NOT IN ("
        "SELECT id FROM signoffs WHERE operation_name = ?))", ("whatsapp_send",))


def failed_routine_runs(uow, request):
    """Successful and in-progress execution bookkeeping stays in SQLite only."""
    return request.select(uow, "state = ?", ("failed",))
