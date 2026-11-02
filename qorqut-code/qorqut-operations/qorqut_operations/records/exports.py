"""History-export coverage of the Context entities: the exact scalar columns the operational snapshot carries.

The host adds this export to its fixed catalog (in the ``host.relationships`` contribution, between the relationship and the
review exports, where the committed catalog places it); nothing here discovers a table at runtime.
"""

from qorqut_operations.history_exports import Integer, TableExport, Text

EXPORTS = (
    TableExport('context_entities', {
        'business_id': Text(),
        'id': Text(),
        'label': Text(),
        'url': Text(),
        'archived': Integer(),
        'revision': Integer(),
        'created_at': Text(),
        'updated_at': Text(),
    }, ('business_id', 'id')),
)
