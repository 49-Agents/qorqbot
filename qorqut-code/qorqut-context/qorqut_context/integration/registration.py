"""The feature declarations Context contributes to a host composition: storage, authority metadata, history exports, policy
fragments and the delegated creation group.

A host splices these at the positions its own aggregate keeps (storage after Actions, catalog entries beside its own, history
beside its other contributions, policy rules between its phases); nothing here registers a handler or touches a database.
Serialized owner labels (``context-queue``, ``context-filing``, ``context-graph-edits``, ``context-usage``,
``context-action-digest``, ``host.context_filing``) are part of stored and exported data and do not change. The reading, source, filing and question
operations declare their own metadata beside their handlers; this module only gathers it."""

from __future__ import annotations

from qorqut_context.integration import action_digest, filing, graph_edits, queue, usage
from qorqut_context.integration.filing import operations as filing_operations
from qorqut_context.integration.filing import source_operations
from qorqut_context.integration.graph_edits import personal_edit
from qorqut_context.integration.questions import operations as question_operations
from qorqut_context.integration.reading import operations as reading_operations
from qorqut_operations.application import DelegatedCreationGroup
from qorqut_operations.contracts import PolicyDecision
from qorqut_operations.history_exports import (Integer, HistoryContribution, PRIVATE_EXCLUDED, SEPARATE_HISTORY, TECHNICAL_EXCLUDED,
                                               TableClassification, TableExport, Text)


def storage_contributions():
    """The five Context storage contributions in dependency order: the queue (on the Action revisions), then the four
    companions that sit on that one queue (filings and receipts, controlled graph edits, the model-call usage ledger and
    the daily Action digest)."""
    return (queue.storage_contribution(), filing.storage_contribution(), graph_edits.storage_contribution(),
            usage.storage_contribution(), action_digest.storage_contribution())


# -- authority metadata ---------------------------------------------------------------------------------------------------

CONTEXT_ENTRIES = (*queue.QUEUE_ENTRIES, *reading_operations.ENTRIES, *source_operations.ENTRIES, *question_operations.ENTRIES,
                   *filing_operations.ENTRIES)


# -- delegation -----------------------------------------------------------------------------------------------------------

CONTEXT_CREATION_GROUPS = (
    # An explicit Context graph edit: its queue input and the graph-edit binding that carries it, created together.
    DelegatedCreationGroup("context_edit", frozenset({"context_graph_edits", "context_inputs"})),
)
PERSONAL_EDIT = personal_edit.NAME


# -- policy ---------------------------------------------------------------------------------------------------------------

SHARED_CONTEXT_READS = frozenset(reading_operations.AGENT_READS)
CONTEXT_QUESTION_OPERATIONS = frozenset({question_operations.READ, question_operations.EDIT})


def question_rule(name, defaults, overrides, registry, *, role_id, scope, resolve, learning_applier_role):
    """The Learning Applier-only question tools, decided by the caller's current ``context_edit`` decision (``resolve``
    re-enters the evaluator). ``None`` leaves every other operation to the host's remaining rules."""
    if name not in CONTEXT_QUESTION_OPERATIONS:
        return None
    if role_id != learning_applier_role:
        return PolicyDecision("deny", "context_questions_role")
    if "deny" in (overrides.get(name), defaults.get(name)):
        return PolicyDecision("deny", "context_questions_denied")
    edit = resolve("context_edit", scope=scope)
    return PolicyDecision("allow" if edit.mode == "allow" else "deny", "context_edit_authority")


def personal_rule(name, defaults, overrides, registry, *, role_id, scope, resolve, coordinator_role):
    """``personal_set`` is the coordinator's and follows its current ``context_edit`` decision: the tool builds a
    ``context_edit`` plan, so it grants nothing that decision does not. An explicit deny on its own name wins, and the handler
    still requires the caller to be the configured Ordubashy. ``None`` leaves every other operation to the remaining rules."""
    if name != PERSONAL_EDIT:
        return None
    if role_id != coordinator_role:
        return PolicyDecision("deny", "personal_record_role")
    if "deny" in (overrides.get(name), defaults.get(name)):
        return PolicyDecision("deny", "personal_record_denied")
    edit = resolve("context_edit", scope=scope)
    return PolicyDecision("allow" if edit.mode == "allow" else "deny", "context_edit_authority")


def shared_read_decision(name):
    """The shared graph reads are the agent-facing surface of the published Context; no role republish needed."""
    if name in SHARED_CONTEXT_READS:
        return PolicyDecision("allow", "core_default")
    return None


# -- history --------------------------------------------------------------------------------------------------------------

FILING_HISTORY = HistoryContribution('host.context_filing', exports=(
    TableExport('context_source_versions', {
        'id': Text(),
        'project_id': Text(nullable=True),
        'source_kind': Text(),
        'source_id': Text(),
        'source_revision': Integer(),
        'operation': Text(),
        'source_path': Text(),
        'source_digest': Text(),
        'retracts_revision': Integer(nullable=True),
        'created_by': Text(),
        'created_at': Text(),
    }, ('id',)),
    TableExport('context_source_bindings', {
        'source_kind': Text(),
        'source_key': Text(),
        'project_id': Text(nullable=True),
        'bound_by_input': Text(),
        'bound_at': Text(),
    }, ('source_kind', 'source_key')),
    TableExport('context_filings', {
        'id': Text(),
        'project_id': Text(nullable=True),
        'source_version_id': Text(),
        'request_id': Text(),
        'job_id': Text(),
        'program_id': Text(),
        'program_version': Integer(),
        'definition_digest': Text(),
        'worker_id': Text(),
        'initiated_by': Text(nullable=True),
        'input_id': Text(),
        'created_at': Text(),
    }, ('id',)),
    TableExport('context_completions', {
        'id': Text(),
        'project_id': Text(nullable=True),
        'filing_id': Text(),
        'job_id': Text(),
        'input_id': Text(),
        'source_version_id': Text(),
        'outcome': Text(),
        'context_event_id': Text(nullable=True),
        'published_commit': Text(nullable=True),
        'outcome_digest': Text(),
        'state': Text(),
        'revision': Integer(),
        'created_at': Text(),
        'resolved_at': Text(nullable=True),
    }, ('id',)),
    TableExport('context_inputs', {
        'sequence': Integer(),
        'id': Text(),
        'project_id': Text(nullable=True),
        'input_kind': Text(),
        'item_id': Text(nullable=True),
        'revision': Integer(nullable=True),
        'operation': Text(),
        'submitted_by': Text(),
        'created_at': Text(),
        'state': Text(),
        'context_event_id': Text(nullable=True),
        'attempt': Integer(),
    }, ('sequence',)),
))

# Classified beside the host's other private-excluded and separately-preserved tables.
PRIVATE_CLASSIFICATIONS = (
    TableClassification('context_graph_edits', PRIVATE_EXCLUDED, 'Raw evidence, application payloads and learning work packages can contain private source data.'),
    TableClassification('context_input_attempts', PRIVATE_EXCLUDED, 'Raw evidence, application payloads and learning work packages can contain private source data.'),
    TableClassification('context_input_controls', PRIVATE_EXCLUDED, 'Raw evidence, application payloads and learning work packages can contain private source data.'),
)
SEPARATE_HISTORY_CLASSIFICATIONS = (
    TableClassification('context_usage_calls', SEPARATE_HISTORY, 'Model usage is preserved in its bounded usage ledger and complete Context recovery.', 'context-usage-ledger'),
    TableClassification('context_usage_sync', SEPARATE_HISTORY, 'Model usage is preserved in its bounded usage ledger and complete Context recovery.', 'context-usage-ledger'),
)
TECHNICAL_CLASSIFICATIONS = (
    TableClassification('context_action_backlog', TECHNICAL_EXCLUDED, 'Scheduling machinery of the daily Action digest: which Action revisions it has still to read; the Actions and their revisions are exported separately.'),
    TableClassification('context_action_digests', TECHNICAL_EXCLUDED, 'Scheduling machinery of the daily Action digest: its cursor and the queue input of each part; the inputs are in the Context queue.'),
)
