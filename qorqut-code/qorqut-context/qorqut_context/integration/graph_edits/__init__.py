"""Controlled deterministic graph edits as metadata for the one Context queue (not another queue or input mode).

``schema`` is the immutable companion binding and its database invariants; ``binding`` admits one edit (the binding, then
its GENERAL input) as part of a host adapter's prepared operation and reads a verified binding back; ``ExecutionFacts`` is the
queue's ``executions`` contribution, so every read of an edit's input exposes the same execution facts."""

from qorqut_context.integration.graph_edits import binding, schema
from qorqut_context.integration.graph_edits.binding import (BINDING_ERRORS, COMPOSITION, INTEGRITY, UNBOUND, Admission, ExecutionFacts, admit,
                                                          check_delegation, check_relation, delegation_of, execution_facts, is_reserved, load,
                                                          reserved_event_ids, source_path, taken_event_ids)
from qorqut_context.integration.graph_edits.schema import EXECUTION, METADATA, PREFIX, storage_contribution

__all__ = ["Admission", "BINDING_ERRORS", "COMPOSITION", "EXECUTION", "ExecutionFacts", "INTEGRITY", "METADATA", "PREFIX", "UNBOUND", "admit",
           "binding", "check_delegation", "check_relation", "delegation_of", "execution_facts", "is_reserved", "load", "reserved_event_ids", "schema",
           "source_path", "storage_contribution", "taken_event_ids"]
