"""History-export coverage of the agent-maintained tables.

``EXPORTS`` selects the exact scalar columns of ``data_tables`` that the operational snapshot carries (the column
definitions, tags and every row stay out of it); ``CLASSIFICATIONS`` records that rows and revisions are versioned by
the separate per-table history. The host adds them to its fixed catalog beside the documents export and the other
separately versioned tables; nothing here discovers a table at runtime.
"""

from qorqut_operations.history_exports import Integer, SEPARATE_HISTORY, TableClassification, TableExport, Text

EXPORTS = (
    TableExport('data_tables', {
        'business_id': Text(nullable=True),
        'id': Text(),
        'title': Text(),
        'description': Text(),
        'archived': Integer(),
        'created_by': Text(),
        'revision': Integer(),
        'created_at': Text(),
        'updated_at': Text(),
    }, ('business_id', 'id')),
)

CLASSIFICATIONS = (
    TableClassification('data_rows', SEPARATE_HISTORY, 'Exact per-table snapshots and rows are versioned by the existing table history.', 'qorqut-tables'),
    TableClassification('table_versions', SEPARATE_HISTORY, 'Exact per-table snapshots and rows are versioned by the existing table history.', 'qorqut-tables'),
)
