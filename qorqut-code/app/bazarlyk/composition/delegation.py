"""The retained v1 delegation rows and vocabulary, supplied to the Operations delegated-authority check.

The checks (binding, generation, live authentication, assignment operations, business and record boundaries, the
explicit-selector rule for reads) are the engine's. This provider only answers from the host's stored assignment and
binding rows, its worker control operations and its role support, and records a committed delegated action through
the existing ``OperationalDelegation`` store, which stays the host's (native delegated execution and its fencing).

The three reviewed review-scope exceptions are also v1 data: the only cases where a globally owned operation may
stage effects on business rows.

The one delegated creation group is the host's trusted vocabulary for ``context_edit``: the queue input and its
companion binding are two new records under one identity allocated while the edit prepares, so an assignment cannot
list them in advance. Only that complete group, for that operation, skips the assignment's record-ID membership; the
Operations authority path enforces it and every other delegated check still applies."""

from __future__ import annotations

from qorqut_operations.application import DelegatedCreationGroup
from qorqut_operations.application.review import BoundResource, ReviewScopeException

from bazarlyk.agents.operational_delegation import (OperationalDelegation, WORKER_OPERATIONS,
                                                    supports_durable_delegation_version)

# Record selector argument names a delegated read or write may carry, and the assignment boundary each is checked against.
SELECTORS = {"task_id": "tasks", "document_id": "documents", "thread_id": "message_threads"}
CONTEXT_SCOPED_READS = frozenset({"agent_config_get", "tasks_config_get"})


class HostDelegationProvider:
    worker_operations = WORKER_OPERATIONS
    context_scoped_reads = CONTEXT_SCOPED_READS

    def binding(self, uow, binding_id):
        return uow.repository("operational_delegation_bindings").get(binding_id)

    def authenticate_binding(self, uow, credential_hash):
        return OperationalDelegation.authenticate_binding(uow, credential_hash)

    def assignment(self, uow, assignment_id):
        return uow.repository("operational_assignments").get(assignment_id)

    def supports_role(self, role):
        return supports_durable_delegation_version(role.version, role.id)

    def required_capabilities(self, role):
        required = {"manage_delegated_assignments"}
        if role.id == "global-marketing":
            required.add("coordinate_businesses")
        return frozenset(required)

    def selector_kinds(self, operation_name):
        selectors = dict(SELECTORS)
        if operation_name == "focus_get":
            selectors["item_id"] = "focus_cards"
        return selectors

    def thread_has_assignment_message(self, uow, thread_id, assignment_id):
        return uow.connection.execute(
            "SELECT 1 FROM agent_messages WHERE thread_id=? AND delegation_assignment_id=? LIMIT 1",
            (thread_id, assignment_id)).fetchone() is not None

    def record_committed_action(self, uow, assignment_id, **action):
        # v1 used a fresh default store here, never an injected clock.
        return OperationalDelegation().record_committed_action(uow, assignment_id, **action)


HOST_REVIEW_EXCEPTIONS = (
    # Execution records of a global routine definition.
    ReviewScopeException(frozenset({"routines_run", "routines_stop"}), frozenset({"jobs", "routine_runs"})),
    # Membership grants of a global agent profile.
    ReviewScopeException(frozenset({"agents_create", "agents_update", "agents_deactivate"}),
                         frozenset({"business_memberships"})),
    # One owner-selected document in the unified journal, by a human principal only.
    ReviewScopeException(frozenset({"notes_update", "notes_delete"}),
                         bound_resources={"documents": BoundResource("document_id"),
                                          "managed_document_files": BoundResource("document_id", "document_id")},
                         principal_kinds=frozenset({"human"}), allow_delegated=False),
)

HOST_DELEGATED_CREATION_GROUPS = (
    # An explicit Context graph edit: its queue input and the graph-edit binding that carries it, created together.
    DelegatedCreationGroup("context_edit", frozenset({"context_graph_edits", "context_inputs"})),
)
