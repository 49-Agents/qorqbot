"""Table specifications. The engine owns the identity, authority and receipt tables below; retained
domains contribute their own through the trusted assembly, never through a caller-supplied value."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableSpec:
    keys: tuple[str, ...]
    scope_column: str | None = None
    mutable: bool = True
    revision_column: str | None = "revision"


# Tables whose rows the extracted authority, application and receipt code reads or writes itself.
# Keys, scope columns and revision columns are unchanged from the deployed v1 catalog.
CORE_TABLES = {
    "installation": TableSpec(("id",)),
    "principals": TableSpec(("id",)),
    "businesses": TableSpec(("id",)),
    "business_memberships": TableSpec(("business_id", "principal_id"), "business_id"),
    "agent_profiles": TableSpec(("agent_id",), "home_business_id"),
    "signoffs": TableSpec(("id",), "review_business_id", True, "status_revision"),
    "idempotency_receipts": TableSpec(("principal_id", "target_namespace", "operation_name", "idempotency_key"),
                                     mutable=False, revision_column=None),
    "apply_receipts": TableSpec(("id",)),
    "resource_claims": TableSpec(("target_namespace", "resource_type", "resource_id"), revision_column=None),
    "maintenance_state": TableSpec(("id",)),
    "routines": TableSpec(("namespace", "id"), "business_id"),
    "routine_runs": TableSpec(("id",), "business_id"),
    "routine_slots": TableSpec(("namespace", "routine_id", "target_namespace")),
    "jobs": TableSpec(("id",), "business_id"),
    # The shared item relationships and their explicit review decisions (append-only decisions, one mutable state
    # row per reviewing item and relationship).
    "relationships": TableSpec(("business_id", "id"), "business_id"),
    "context_reviews": TableSpec(("business_id", "id"), "business_id", False, None),
    "context_review_states": TableSpec(("business_id", "relationship_id", "item_type", "item_id"), "business_id"),
}

CORE_IMMUTABLE_FIELDS = {
    "signoffs": frozenset({"review_scope_kind", "review_business_id", "proposer_principal_id", "proposer_agent_id",
        "initiated_by_principal_id", "operation_name", "operation_version", "proposal_revision",
        "payload_hash", "change_set", "preview_json", "expected_targets_json", "policy_binding_json",
        "affected_scope_json", "supersedes_id", "request_key", "category", "execution_job_id", "legacy_evidence_json"}),
    "apply_receipts": frozenset({"principal_id", "target_namespace", "operation_name", "payload_hash",
                                  "change_set", "signoff_id"}),
    "jobs": frozenset({"business_id", "target_namespace", "kind", "principal_id", "initiated_by_principal_id",
                        "arguments_json", "definition_ref_json", "dedupe_key"}),
}
