"""The inbox's general-history export: selected columns of events, deliveries and subscriptions about the closed item sources.

The contribution keeps the serialized name ``host.inbox``, which the stored history manifest records, and the exact fields, keys
and selectors the host exported before. ``ITEM_SOURCES`` is a closed list: a future event source does not automatically export a
private message summary, a visibility document, an owner-conversation row or a native session state. Delivery state is filtered
through the exact same event set."""

from __future__ import annotations

from qorqut_operations.history_exports import HistoryContribution, Integer, TableExport, Text

# Closed list: a future event source does not automatically export a private
# message summary. Delivery state is filtered through the exact same event set.
ITEM_SOURCES = ("task", "tasks", "action", "action_logs", "signoff", "signoffs",
                "table", "data_tables", "document", "documents", "routine", "routines")
MARKS = ",".join("?" for _ in ITEM_SOURCES)


def item_inbox(uow, request):
    return request.select(uow, f"source_type IN ({MARKS})", ITEM_SOURCES)


def item_deliveries(uow, request):
    return request.select(uow, f"event_id IN (SELECT id FROM inbox_events WHERE source_type IN ({MARKS}))", ITEM_SOURCES)


def item_subscriptions(uow, request):
    return request.select(uow, f"source_type IN ({MARKS})", ITEM_SOURCES)


CONTRIBUTION = HistoryContribution('host.inbox', exports=(
    TableExport('inbox_events', {
        'id': Integer(),
        'business_id': Text(nullable=True),
        'source_type': Text(),
        'source_id': Text(),
        'source_revision': Integer(nullable=True),
        'actor_id': Text(nullable=True),
        'event_type': Text(),
        'summary': Text(),
        'created_at': Text(),
    }, ('id',), selector=item_inbox),
    TableExport('inbox_deliveries', {
        'agent_id': Text(),
        'event_id': Integer(),
        'acknowledged_at': Text(nullable=True),
        'outcome': Text(nullable=True),
        'required': Integer(),
    }, ('agent_id', 'event_id'), selector=item_deliveries),
    TableExport('inbox_subscriptions', {
        'id': Text(),
        'agent_id': Text(),
        'business_id': Text(nullable=True),
        'source_type': Text(),
        'source_id': Text(),
        'include_related': Integer(),
        'created_at': Text(),
    }, ('id',), selector=item_subscriptions),
))
