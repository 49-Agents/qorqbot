"""History-export coverage of the two CRM tables, classified explicitly (nothing is discovered at runtime).

Memberships (their local cells and scoped Task references) are versioned in the exact per-table snapshots. Recorded
external message bodies stay out of every broad Git export, like private and raw messages; the normal database backup
retains them."""

from qorqut_operations.history_exports import PRIVATE_EXCLUDED, SEPARATE_HISTORY, TableClassification

CLASSIFICATIONS = (
    TableClassification('crm_entries', SEPARATE_HISTORY,
                        'Exact per-table snapshots version memberships, local cells and scoped Task references.', 'qorqut-tables'),
    TableClassification('crm_activity_events', PRIVATE_EXCLUDED,
                        'Recorded external email/message bodies stay out of broad history exports; database backups retain them.'),
)
