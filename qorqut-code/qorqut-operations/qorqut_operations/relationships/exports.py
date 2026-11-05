"""History-export coverage of the relationship edges and their reviews: the exact scalar columns the operational snapshot
carries.

The host adds them to its fixed catalog, in its ``host.relationships`` contribution: ``EDGE_EXPORTS``, then the Context
entity export of ``records.exports``, then ``REVIEW_EXPORTS`` (the committed order); nothing here discovers a table at
runtime.
"""

from qorqut_operations.history_exports import Integer, TableExport, Text

EDGE_EXPORTS = (
    TableExport('relationships', {
        'business_id': Text(nullable=True),
        'id': Text(),
        'from_type': Text(),
        'from_id': Text(),
        'to_type': Text(),
        'to_id': Text(),
        'role': Text(),
        'context': Text(),
        'revision': Integer(),
        'created_at': Text(),
        'updated_at': Text(),
        'created_by': Text(nullable=True),
    }, ('business_id', 'id')),
)

REVIEW_EXPORTS = (
    TableExport('context_reviews', {
        'business_id': Text(),
        'id': Text(),
        'relationship_id': Text(),
        'item_type': Text(),
        'item_id': Text(),
        'source_type': Text(),
        'source_id': Text(),
        'source_token': Text(),
        'relationship_revision': Integer(),
        'result': Text(),
        'explanation': Text(),
        'actor_id': Text(),
        'prepared_at': Text(),
        'created_at': Text(),
    }, ('business_id', 'id')),
    TableExport('context_review_states', {
        'business_id': Text(),
        'relationship_id': Text(),
        'item_type': Text(),
        'item_id': Text(),
        'review_id': Text(),
        'revision': Integer(),
        'created_at': Text(),
        'updated_at': Text(),
    }, ('business_id', 'relationship_id', 'item_type', 'item_id')),
)
