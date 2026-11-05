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
    "telegram_state": TableSpec(("id",)),
    "telegram_updates": TableSpec(("bot_id", "update_id")),
    "telegram_messages": TableSpec(("id",)),
    "telegram_outbox": TableSpec(("id",)),
    "telegram_events": TableSpec(("producer_id", "event_key")),
    "telegram_signoff_posts": TableSpec(("id",)),
    "telegram_owner_inputs": TableSpec(("id",)),
    "telegram_cleanup_jobs": TableSpec(("id",)),
    "agent_messages": INBOX_TABLES["agent_messages"],
    "broadcasts": INBOX_TABLES["broadcasts"],
    "broadcast_deliveries": INBOX_TABLES["broadcast_deliveries"],
    "inbox_changes": INBOX_TABLES["inbox_changes"],
    "native_session_owners": TableSpec(("agent_id",), revision_column=None),
    **RECORD_TABLES,
    **operations_tables.TABLES,
    "management_routing": TableSpec(("id",)),
    "business_sandboxes": TableSpec(("business_id",)),
    "business_previews": TableSpec(("id",), "business_id"),
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
    "telegram_state": frozenset({"bot_id", "installation_id", "activation_at"}),
    "telegram_updates": frozenset({"kind", "received_at"}),
    "telegram_messages": frozenset({"direction", "chat_name", "chat_id", "message_thread_id", "agent_id", "purpose", "occurred_at", "request_key", "input_mode"}),
    "telegram_outbox": frozenset({"chat_name", "chat_id", "message_thread_id", "kind", "source_kind", "source_key", "part", "message_id", "payload_json", "event_producer", "event_key", "event_sequence"}),
    "telegram_signoff_posts": frozenset({"signoff_id", "generation", "category", "proposal_revision", "payload_hash", "expected_status_revision", "input_form"}),
    "telegram_owner_inputs": frozenset({"purpose", "correlation_id", "message_id", "agent_id", "sensitive", "owner_principal_id", "config_generation", "binding_fingerprint", "target_fingerprint"}),
    "telegram_cleanup_jobs": frozenset({"message_id"}),
}
