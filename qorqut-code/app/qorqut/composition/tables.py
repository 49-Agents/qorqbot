"""Repository specifications of the retained host tables, unchanged from the deployed v1 catalog.

The Operations engine owns the identity, authority and receipt entries (``qorqut_operations.storage.specs``);
these are the remaining retained-domain tables (Tasks, Actions, the tag catalog, the item discussion with its inbox
persistence and the Context queue are the engine's operational-record packages and declare their own). Each table belongs to exactly one schema owner in
``schema_ddl.OWNER_TABLES``, which ``composition.schema`` checks.
"""

from qorqut_operations import tables as operations_tables
from qorqut_operations.documents.schema import TABLES as DOCUMENT_TABLES
from qorqut_operations.inbox.schema import TABLE_SPECS as INBOX_TABLES
from qorqut_operations.records.schema import TABLES as RECORD_TABLES
from qorqut_operations.storage.specs import TableSpec

TABLE_SPECS = {
    "journal_reviews": TableSpec(("id",)),
    "journal_reconciliations": TableSpec(("id",)),
    "terminal_commands": TableSpec(("id",)),
    "terminal_actions": TableSpec(("id",)),
    "agent_focus": TableSpec(("agent_id",)),
    "working_memory_versions": TableSpec(("document_id", "version"), "business_id", False, None),
    "working_memory_provenance": TableSpec(("document_id", "version"), "business_id", False, None),
    "dev_tickets": TableSpec(("id",)),
    "dev_ticket_events": TableSpec(("id",), mutable=False, revision_column=None),
    # No revision column exists in the deployed v1 table; its rows are written by direct SQL, never through a repository.
    "system_incidents": TableSpec(("id",), revision_column=None),
    "system_incident_observations": TableSpec(("id",), mutable=False, revision_column=None),
    "system_incident_impacts": TableSpec(("incident_id", "business_id", "agent_id", "session_id", "task_id", "operation", "job_id", "receipt_id"), mutable=False, revision_column=None),
    "system_incident_recoveries": TableSpec(("incident_id", "observation_id"), mutable=False, revision_column=None),
    "system_incident_history": TableSpec(("id",), mutable=False, revision_column=None),
    "agent_messages": INBOX_TABLES["agent_messages"],
    "broadcasts": INBOX_TABLES["broadcasts"],
    "broadcast_deliveries": INBOX_TABLES["broadcast_deliveries"],
    "inbox_changes": INBOX_TABLES["inbox_changes"],
    "native_session_owners": TableSpec(("agent_id",), revision_column=None),
    **RECORD_TABLES,
    **operations_tables.TABLES,
    "management_routing": TableSpec(("id",)),
    "artifacts": TableSpec(("kind", "artifact_id", "version"), mutable=False, revision_column=None),
    "agent_activity": TableSpec(("agent_id", "session_id")),
    "agent_viewbox_boards": TableSpec(("agent_id",), revision_column=None),
    "agent_viewbox_items": TableSpec(("id",), revision_column=None),
    "operational_assignments": TableSpec(("id",)),
    "operational_assignment_events": TableSpec(("id",), mutable=False, revision_column=None),
    "operational_delegation_bindings": TableSpec(("id",)),
    "operational_action_receipts": TableSpec(("id",)),
    "operational_event_claims": TableSpec(("event_id",), revision_column="revision"),
    "browser_turns": TableSpec(("id",)),
    "browser_turn_actions": TableSpec(("id",)),
    "credentials": TableSpec(("id",)),
    "workspace_presentations": TableSpec(("business_id", "id"), "business_id"),
    "machine_registrations": TableSpec(("id",)),
    "session_registrations": TableSpec(("id",)),
    "session_deliveries": TableSpec(("id",)),
    "business_configs": TableSpec(("business_id", "domain"), "business_id"),
    **DOCUMENT_TABLES,
    "learning_candidates": TableSpec(("business_id", "id"), "business_id"),
    "learning_decisions": TableSpec(("business_id", "candidate_id"), "business_id"),
    "learning_history": TableSpec(("business_id", "id"), "business_id", False, None),
    "learning_runs": TableSpec(("business_id", "id"), "business_id", False, None),
    "learning_scan_state": TableSpec(("business_id", "session_id"), "business_id"),
    "imports": TableSpec(("id",)),
    "backup_runs": TableSpec(("id",)),
}

IMMUTABLE_FIELDS = {
    **operations_tables.IMMUTABLE_FIELDS,
}
