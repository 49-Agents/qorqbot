"""Factual-write guard for the retained learning operations and their historical procedural rows.

The factual Context route keeps working exactly as before: extraction may still retain ``bucket='context'`` items and
the owner (or, for one instructed decision, the Learning Applier) may still review, edit and file them through the
existing Context authority. New procedural learning deltas, their decisions, edits, work transitions,
supersession/reconciliation writes and engine imports are refused. Old rows and reads stay available, and a bounded
read-only inventory of retained deltas is provided for the owner's later migration decision.

``validate_saved_plan`` is the shared guard registered through ``OperationDescriptor.validate_targets`` on every
retained learning mutator and published for the offline transition as ``validation.validate_factual_plan``. It builds a
read-only view of the plan's exact prepared effects keyed by full scope/table/identity, resolves stored and prepared
candidate/decision states, and inspects the typed semantics of each retained operation: allowed actions and fields,
jobs, runs, scan states, files, history targets and ordinary/nested reconciliation and supersession structures. Unknown,
unprovable, procedural, engine-linked or foreign effects are refused before the PreparedFileExecutor applies any file
and again before the domain apply.
"""

import base64
import hashlib
import json

from qorqut_context.integration.filing import envelope
from qorqut_context.integration.filing.admission import versions as source_lineage
from qorqut_context.integration.filing.filing import document_metadata, event_metadata
from qorqut_context.integration.queue.queue import general_arguments
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.documents.serialization import document_bytes, split_document

from .intake import snapshot as intake_snapshot

PROCEDURAL_BUCKET = "delta"
CONTEXT_BUCKET = "context"
RETIRED_MESSAGE = (
    "The procedural learning-delta route is retired in this target; new proposals, decisions, edits, work transitions, "
    "reconciliations and engine imports must use Overseer. Retained old records stay readable and the read-only legacy "
    "pending inventory lists what an owner migration decision would need.")

# The seven removed engine tables: a retained learning mutator never writes one, and nothing here reads them.
ENGINE_TABLES = frozenset({"learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links",
                           "learning_engine_packages", "learning_engine_enrollments", "learning_engine_state"})
ENGINE_KEYS = frozenset({"engine", "engine_id", "engine_scope_id", "issue_id", "issue_revision", "package_revision",
                         "package_hash", "package_json", "engine_status", "material_evidence_revision"})

OWNER_EDIT_FIELDS = frozenset({"scope", "bucket", "tag", "text"})
DECISION_KEYS = frozenset({"candidate_id", "state", "feedback", "changes_json", "decided_by", "created_at", "updated_at"})
HISTORY_KEYS = frozenset({"id", "candidate_id", "event", "actor_id", "before_json", "after_json", "created_at"})
JOB_KEYS = frozenset({"id", "business_id", "target_namespace", "kind", "principal_id", "initiated_by_principal_id",
                      "arguments_json", "definition_ref_json", "dedupe_key", "state"})
RUN_KEYS = frozenset({"id", "cores", "started", "finished", "supersession_mode", "processed", "processed_by_source",
                      "items", "dedup", "errors", "diagnostics", "partial", "status", "source_states", "paused_sources",
                      "cost_usd", "max_usd", "job_id"})
SCAN_KEYS = frozenset({"session_id", "transcript_hash", "state_json", "updated_at"})

# The exact record effects each retained learning operation may prepare, including the fields it may declare.
LEARNING_EFFECTS = {
    "learnings_review": {
        "learning_candidates": {"update": frozenset({"candidate_json", "updated_at", "filing_json"})},
        "learning_decisions": {"insert": DECISION_KEYS, "update": DECISION_KEYS},
        "learning_history": {"insert": HISTORY_KEYS},
    },
    "learnings_edit": {
        "learning_candidates": {"update": frozenset({"candidate_json", "updated_at", "filing_json"})},
        "learning_decisions": {"insert": DECISION_KEYS, "update": DECISION_KEYS},
        "learning_history": {"insert": HISTORY_KEYS},
    },
    "learnings_decide": {
        "learning_candidates": {"update": frozenset({"candidate_json", "updated_at", "filing_json"})},
        "learning_decisions": {"insert": DECISION_KEYS, "update": DECISION_KEYS},
        "learning_history": {"insert": HISTORY_KEYS},
    },
    "learnings_keep_both": {
        "learning_candidates": {"insert": frozenset({"id", "candidate_json"}),
                                "update": frozenset({"candidate_json"})},
        "learning_decisions": {"update": frozenset({"state", "feedback", "changes_json", "decided_by", "updated_at"})},
        "learning_history": {"insert": HISTORY_KEYS},
    },
    "learnings_record_batch": {
        "learning_candidates": {"insert": frozenset({"id", "candidate_json"}),
                                "update": frozenset({"candidate_json"})},
        "learning_decisions": {"insert": DECISION_KEYS},
        "learning_scan_state": {"insert": SCAN_KEYS, "update": SCAN_KEYS},
        "learning_runs": {"insert": frozenset({"id", "result_json"})},
        "learning_history": {"insert": HISTORY_KEYS},
    },
    "learnings_extract": {
        "jobs": {"insert": JOB_KEYS},
    },
}
# Mutators whose plan legitimately carries the existing Context intake composition; the composed documents validator
# checks those document targets. No other retained learning mutator writes files or Context filing rows.
FILE_MUTATORS = frozenset({"learnings_review", "learnings_edit", "learnings_decide"})
CONTEXT_FILING_TABLES = frozenset({"documents", "context_filings", "context_inputs", "context_source_versions", "jobs"})
# The supported composed Context intake effect contracts. Each value is the exact stored row shape prepared by the
# documents/Context filing composition; a retained learning plan may not invent an action or a field outside it.
DOCUMENT_INSERT_FIELDS = frozenset({"id", "business_id", "namespace", "owning_agent_id", "kind", "path", "content_hash",
                                    "metadata_json", "source_refs_json", "availability", "created_at", "updated_at"})
DOCUMENT_UPDATE_FIELDS = frozenset({"kind", "path", "content_hash", "metadata_json", "source_refs_json", "updated_at"})
AUXILIARY_EFFECTS = {
    "documents": {"insert": DOCUMENT_INSERT_FIELDS, "update": DOCUMENT_UPDATE_FIELDS},
    "context_source_versions": {"insert": frozenset({"id", "business_id", "source_kind", "source_id", "source_revision",
                                                     "operation", "source_path", "source_digest", "retracts_revision",
                                                     "created_by"})},
    "context_filings": {"insert": frozenset({"id", "business_id", "source_version_id", "request_id", "job_id",
                                             "program_id", "program_version", "definition_digest", "worker_id",
                                             "initiated_by", "input_id"})},
    "context_inputs": {"insert": frozenset({"id", "business_id", "input_kind", "operation", "payload_json",
                                            "submitted_by", "created_at"})},
    "jobs": {"insert": JOB_KEYS},
}
AUXILIARY_REQUIRED = {
    "documents": {"insert": DOCUMENT_INSERT_FIELDS, "update": DOCUMENT_UPDATE_FIELDS},
    "context_source_versions": {"insert": AUXILIARY_EFFECTS["context_source_versions"]["insert"]},
    "context_filings": {"insert": AUXILIARY_EFFECTS["context_filings"]["insert"]},
    "context_inputs": {"insert": AUXILIARY_EFFECTS["context_inputs"]["insert"]},
    "jobs": {"insert": JOB_KEYS},
}
# The repository's stored primary key for every table a retained learning plan may write; all these specs exclude the
# business scope column from the key, and an insert's values (not its declared resource) decide the row it creates.
PRIMARY_KEYS = {"learning_candidates": "id", "learning_decisions": "candidate_id", "learning_history": "id",
                "learning_runs": "id", "learning_scan_state": "session_id", "documents": "id", "context_filings": "id",
                "context_inputs": "id", "context_source_versions": "id", "jobs": "id"}
SCOPE_FIELD = "business_id"
UPLOAD_DOCUMENT_KINDS = frozenset({"context_upload"})
INDEX_DOCUMENT_KINDS = frozenset({"context_index"})
# Jobs a retained plan may queue: extraction itself, or the existing Context filing completion for an accepted intake.
JOB_KINDS = {"learnings_extract": frozenset({"learnings.extract"}), "intake": frozenset({"context.file"})}
# Raw candidate snapshots in these audit rows may legitimately omit a bucket when their target is factual.
BUCKETLESS_HISTORY_OPERATIONS = frozenset({"learnings_keep_both", "learnings_record_batch"})
MAX_GRAPH_DEPTH = 100


def is_procedural(item):
    return isinstance(item, dict) and item.get("bucket") == PROCEDURAL_BUCKET


def refuse():
    raise DomainError("forbidden", RETIRED_MESSAGE)


def _forbidden(message):
    raise DomainError("forbidden", RETIRED_MESSAGE + " " + message)


def require_context(item, what="learning candidate"):
    """Refuse one stored or proposed procedural candidate; context items pass through unchanged."""
    if not is_procedural(item):
        return item
    refuse()


def context_only_items(items):
    """The factual subset of one producer batch; every procedural item is dropped before it can be emitted."""
    return [item for item in items if not is_procedural(item)]


def guard_batch(items, evidence, lookup):
    """Refuse a mixed producer batch atomically before any record is prepared.

    ``lookup`` resolves one candidate id to its stored candidate JSON (None when it does not exist yet).
    """
    procedural = sorted(str(item.get("id")) for item in items if is_procedural(item))
    for line in evidence:
        if is_procedural(lookup(line.get("of"))):
            procedural.append("relation:" + str(line.get("of")))
        if is_procedural(lookup(line.get("into"))):
            procedural.append("relation:" + str(line.get("into")))
    if procedural:
        raise DomainError("forbidden", RETIRED_MESSAGE + " Refused: " + ", ".join(procedural[:20]))


def guard_candidate(row, what="learning candidate"):
    require_context((row or {}).get("candidate_json"), what)


def effective(candidate, decision):
    """The candidate's effective state: the retained decision's edits applied over the stored extraction output."""
    value = dict(candidate or {})
    changes = plain((decision or {}).get("changes_json"))
    if isinstance(changes, dict):
        value.update(changes)
    return value


def compose(*validators):
    """Compose one descriptor's target validators; either may be absent, and each runs in registration order."""
    present = tuple(validator for validator in validators if validator is not None)
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    def validate(context, plan, uow):
        for validator in present:
            validator(context, plan, uow)

    return validate


def _engine_marker(value, depth=0):
    """Whether one stored/prepared value carries an engine marker anywhere in its bounded structure."""
    if depth > MAX_GRAPH_DEPTH:
        raise DomainError("forbidden", "A prepared learning value is nested too deeply to inspect")
    if isinstance(value, dict):
        if ENGINE_KEYS & set(value):
            return True
        return any(_engine_marker(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(_engine_marker(item, depth + 1) for item in value)
    return False


def _plain_mapping(values, what):
    try:
        value = plain(values)
    except (TypeError, ValueError) as exc:
        raise DomainError("forbidden", RETIRED_MESSAGE + " " + what + " is not plain JSON.") from exc
    if not isinstance(value, dict):
        _forbidden(what + " is not an object.")
    return value


def _field_check(table, action, values, allowed):
    if action == "delete":
        _forbidden("A retained learning change may not delete a stored record.")
    fields = allowed.get(action)
    if fields is None:
        _forbidden("This learning operation never prepares a " + action + " on " + table + ".")
    unsupported = sorted(set(values) - fields)
    if unsupported:
        _forbidden("A prepared " + table + " effect carries unsupported fields: " + ", ".join(unsupported) + ".")


def _json_object(value, what):
    value = plain(value)
    if not isinstance(value, dict):
        _forbidden(what + " is not a JSON object.")
    return value


def _required_fields(table, required, values):
    missing = sorted(set(required) - set(values))
    if missing:
        _forbidden("A prepared " + table + " effect omits required fields: " + ", ".join(missing) + ".")


def _text(value, what):
    if not isinstance(value, str) or not value:
        _forbidden(what + " is not a non-empty string.")
    return value


def _identifier(value, what):
    text = _text(value, what)
    if len(text) != 36:
        _forbidden(what + " does not have the Context identifier shape.")
    return text


def _bounded_text(value, what, maximum):
    text = _text(value, what)
    if len(text) > maximum:
        _forbidden(what + " is longer than its stored contract permits.")
    return text


def _positive_int(value, what):
    if type(value) is not int or value < 1:
        _forbidden(what + " is not a positive integer.")
    return value


def _hex_digest(value, what):
    text = _text(value, what)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        _forbidden(what + " is not a SHA-256 digest.")
    return text


class _Effects:
    """The exact prepared effects of one plan, resolved against stored rows in the caller's own scope."""

    def __init__(self, ctx, plan, uow, *, source_bytes=None):
        self.ctx, self.plan, self.uow, self.source_bytes = ctx, plan, uow, source_bytes
        self.business = ctx.business_id
        self.candidate_changes = {}
        self.decision_changes = {}
        self.history = []
        self.learning_jobs = []
        self.auxiliary_jobs = []
        self.auxiliary = []
        self.files = []
        self.accepted_ids = set()
        self.accepted = False
        self._candidate_rows = {}
        self._decision_rows = {}
        self._validate_effects()
        if (self.auxiliary or self.auxiliary_jobs) and (
                type(self.source_bytes) is not int or self.source_bytes < 1):
            _forbidden("A prepared Context filing requires the actual positive configured Context source limit.")

    # -- typing ---------------------------------------------------------------------------------------------------
    def _validate_effects(self):
        allowed = LEARNING_EFFECTS.get(self.plan.operation_name)
        if allowed is None:
            _forbidden("A saved change names an operation outside the factual learning contract.")
        file_mutator = self.plan.operation_name in FILE_MUTATORS
        seen, seen_files = set(), set()
        for change in self.plan.changes:
            table = change.resource.resource_type
            if change.kind == "file":
                if not file_mutator:
                    _forbidden("This learning operation has no prepared file effect.")
                key = (change.resource.resource_type, change.resource.business_id, change.resource.resource_id)
                if key in seen_files:
                    _forbidden("A saved learning change prepares the same file twice.")
                seen_files.add(key)
                self.files.append(change)
                continue
            if change.kind != "record":
                _forbidden("A retained learning change may only carry record or document effects.")
            if change.resource.business_id != self.business:
                _forbidden("A prepared learning effect targets another scope.")
            values = _plain_mapping(change.values, "A prepared " + table + " effect")
            if table in ENGINE_TABLES:
                _forbidden("A saved learning change targets a removed procedural engine record.")
            if table not in PRIMARY_KEYS:
                _forbidden("A saved learning change targets a table outside the supported learning contract.")
            identity = (table, change.resource.business_id, change.resource.resource_id)
            if identity in seen:
                _forbidden("A saved learning change prepares the same stored record twice.")
            seen.add(identity)
            self._bind_primary_key(table, change, values)
            if table in allowed:
                _field_check(table, change.action, values, allowed[table])
                self._typed(table, change, values)
            elif table in CONTEXT_FILING_TABLES and file_mutator:
                _field_check(table, change.action, values, AUXILIARY_EFFECTS[table])
                if table == "jobs":
                    self.auxiliary_jobs.append((change, values))
                else:
                    self.auxiliary.append((table, change, values))
            else:
                _forbidden("A saved learning change targets a record this operation never writes.")

    def _bind_primary_key(self, table, change, values):
        """The stored row an effect will create/update must be the one its declared resource names."""
        key = PRIMARY_KEYS[table]
        if change.action == "insert":
            if values.get(key) is None:
                _forbidden("A prepared " + table + " insert omits its primary key.")
            if values.get(key) != change.resource.resource_id:
                _forbidden("A prepared " + table + " insert names another stored record.")
        elif change.action == "update":
            if key in values:
                _forbidden("A prepared " + table + " update may not mutate its primary key.")
            if SCOPE_FIELD in values:
                _forbidden("A prepared " + table + " update may not mutate its stored scope.")
        else:
            _forbidden("This learning operation never prepares the action " + change.action + " on " + table + ".")
        if SCOPE_FIELD in values and values[SCOPE_FIELD] != change.resource.business_id:
            _forbidden("A prepared " + table + " effect names another scope.")

    def _typed(self, table, change, values):
        if table == "learning_candidates":
            candidate = _json_object(values.get("candidate_json"), "A prepared candidate")
            if _engine_marker(candidate):
                _forbidden("A retained learning change may not write a procedural engine projection.")
            self.candidate_changes[(change.resource.business_id, change.resource.resource_id)] = (change, candidate)
        elif table == "learning_decisions":
            changes = _json_object(values.get("changes_json", {}), "A prepared decision change set")
            unsupported = sorted(set(changes) - OWNER_EDIT_FIELDS)
            if unsupported:
                _forbidden("A prepared decision carries unsupported change fields: " + ", ".join(unsupported) + ".")
            if changes.get("bucket", CONTEXT_BUCKET) != CONTEXT_BUCKET:
                _forbidden("A prepared decision may not classify a factual record as procedural.")
            if _engine_marker(changes):
                _forbidden("A prepared decision carries a procedural engine projection.")
            self.decision_changes[(change.resource.business_id, change.resource.resource_id)] = (change, values)
        elif table == "learning_history":
            self.history.append((change, values))
        elif table == "learning_runs":
            result = _json_object(values.get("result_json"), "A prepared learning run")
            if result.get("id") != change.resource.resource_id:
                _forbidden("A prepared learning run names another identity.")
            if not isinstance(result.get("cores"), dict):
                _forbidden("A prepared learning run has no extraction cores.")
            unsupported = sorted(set(result) - RUN_KEYS)
            if unsupported:
                _forbidden("A prepared learning run carries unsupported fields: " + ", ".join(unsupported) + ".")
            if _engine_marker(result):
                _forbidden("A prepared learning run carries a procedural engine projection.")
        elif table == "learning_scan_state":
            state = _json_object(values.get("state_json"), "A prepared scan state")
            if change.action == "insert" and values.get("session_id") != change.resource.resource_id:
                _forbidden("A prepared scan state names another session.")
            if _engine_marker(state):
                _forbidden("A prepared scan state carries a procedural engine projection.")
        elif table == "jobs":
            self.learning_jobs.append((change, values))

    # -- stored/prepared state ------------------------------------------------------------------------------------
    def candidate_row(self, business, identity):
        key = (business, identity)
        if key not in self._candidate_rows:
            row = self.uow.repository("learning_candidates", business_id=business).get(identity)
            change = self.candidate_changes.get(key)
            if change is not None:
                if change[0].action == "insert" and row is not None:
                    _forbidden("A prepared learning change inserts an existing record.")
                if change[0].action == "update" and row is None:
                    _forbidden("A prepared learning change updates an unknown record.")
            self._candidate_rows[key] = row
        return self._candidate_rows[key]

    def decision_row(self, business, identity):
        key = (business, identity)
        if key not in self._decision_rows:
            row = self.uow.repository("learning_decisions", business_id=business).get(identity)
            change = self.decision_changes.get(key)
            if change is not None:
                if change[0].action == "insert" and row is not None:
                    _forbidden("A prepared learning change inserts an existing decision.")
                if change[0].action == "update" and row is None:
                    _forbidden("A prepared learning change updates an unknown decision.")
            self._decision_rows[key] = row
        return self._decision_rows[key]

    def candidate_json_after(self, business, identity):
        change = self.candidate_changes.get((business, identity))
        if change is not None:
            return change[1]
        row = self.candidate_row(business, identity)
        return plain(row["candidate_json"]) if row else None

    def after_decision(self, business, identity):
        change = self.decision_changes.get((business, identity))
        if change is not None:
            values = change[1]
            if change[0].action == "update":
                return {**(self.decision_row(business, identity) or {}), **values}
            return values
        return self.decision_row(business, identity)

    def after_state(self, business, identity):
        candidate = self.candidate_json_after(business, identity)
        if candidate is None:
            return None
        return effective(candidate, self.after_decision(business, identity))

    def before_state(self, business, identity):
        row = self.candidate_row(business, identity)
        if row is None:
            return None
        return effective(row["candidate_json"], self.decision_row(business, identity))

    # -- validation -----------------------------------------------------------------------------------------------
    def validate(self):
        written = sorted(set(self.candidate_changes) | set(self.decision_changes))
        self.accepted_ids = {(business, identity) for business, identity in written
                             if (self.after_decision(business, identity) or {}).get("state") == "accepted"
                             and (self.after_state(business, identity) or {}).get("bucket") == CONTEXT_BUCKET}
        self.accepted = bool(self.accepted_ids)
        if self.files or self.auxiliary or self.auxiliary_jobs:
            if not self.accepted:
                _forbidden("A prepared file or filing effect requires an accepted factual Context decision in the same plan.")
            self._validate_context()
        for business, identity in written:
            before_raw = (self.candidate_row(business, identity) or {}).get("candidate_json")
            after_raw = self.candidate_json_after(business, identity)
            before = self.before_state(business, identity)
            after = self.after_state(business, identity)
            if before_raw is not None and _engine_marker(before_raw):
                _forbidden("A retained learning change targets an engine-linked record.")
            if before is not None:
                if before.get("bucket") != CONTEXT_BUCKET:
                    _forbidden("A retained learning change may not modify a historical procedural or unclassified record.")
                _validate_links(self, business, identity, before)
            if after is None or after_raw is None:
                _forbidden("A prepared learning effect has no resolvable candidate.")
            if after.get("bucket") != CONTEXT_BUCKET:
                _forbidden("A retained learning change may only write a factual Context candidate.")
            _validate_links(self, business, identity, after)
            changes = (self.after_decision(business, identity) or {}).get("changes_json") or {}
            changes = _json_object(changes, "A prepared decision change set")
            unsupported = sorted(set(changes) - OWNER_EDIT_FIELDS)
            if unsupported:
                _forbidden("A prepared decision carries unsupported change fields: " + ", ".join(unsupported) + ".")
            if _engine_marker(changes):
                _forbidden("A prepared decision carries a procedural engine projection.")
        for change, values in self.history:
            self._history(values)
        for change, values in self.learning_jobs:
            self._job(values, JOB_KINDS["learnings_extract"])
        for change, values in self.auxiliary_jobs:
            self._job(values, JOB_KINDS["intake"])

    def _history(self, values):
        required = {"id", "candidate_id", "event", "actor_id", "before_json", "after_json", "created_at"}
        if not required <= set(values):
            _forbidden("A prepared learning history record is incomplete.")
        if values.get("event") != self.plan.operation_name:
            _forbidden("A prepared learning history record names another event.")
        if values.get("actor_id") != self.ctx.principal_id:
            _forbidden("A prepared learning history record attributes another actor.")
        identity = values.get("candidate_id")
        if not isinstance(identity, str) or not identity:
            _forbidden("A prepared learning history record has no target candidate.")
        after = self.after_state(self.business, identity)
        if after is None:
            _forbidden("A prepared learning history record targets an unknown learning record.")
        if after.get("bucket") != CONTEXT_BUCKET:
            _forbidden("A prepared learning history record targets a historical procedural record.")
        if _engine_marker(after):
            _forbidden("A prepared learning history record targets an engine-linked record.")
        for side in ("before_json", "after_json"):
            snapshot = _json_object(values.get(side), "A prepared history snapshot")
            if _engine_marker(snapshot):
                _forbidden("A prepared history snapshot carries a procedural engine projection.")
            if "bucket" in snapshot:
                if snapshot["bucket"] != CONTEXT_BUCKET:
                    _forbidden("A retained learning change may not record procedural history.")
            elif self.plan.operation_name not in BUCKETLESS_HISTORY_OPERATIONS:
                _forbidden("A prepared history snapshot omits the factual bucket of a full learning item.")
            _validate_links(self, self.business, identity, snapshot)

    def _job(self, values, kinds):
        required = {"id", "business_id", "target_namespace", "kind", "principal_id", "initiated_by_principal_id",
                    "arguments_json", "definition_ref_json", "dedupe_key", "state"}
        if not required <= set(values):
            _forbidden("A prepared learning job is incomplete.")
        if values.get("business_id") != self.business:
            _forbidden("A prepared learning job targets another scope.")
        if values.get("kind") not in kinds or values.get("kind") == "learnings.engine_extract":
            _forbidden("A prepared learning job queues an unsupported program.")
        definition = _json_object(values.get("definition_ref_json"), "A prepared job definition")
        if definition.get("id") != values.get("kind"):
            _forbidden("A prepared learning job definition names another program.")
        _json_object(values.get("arguments_json"), "A prepared job argument set")
        if _engine_marker(values):
            _forbidden("A prepared learning job carries a procedural engine projection.")


    def after_filing(self, business, identity):
        """The candidate's effective filing JSON (stored column plus any prepared candidate update)."""
        change = self.candidate_changes.get((business, identity))
        if change is not None:
            values = _plain_mapping(change[0].values, "A prepared candidate effect")
            if "filing_json" in values:
                return _json_object(values["filing_json"], "A prepared candidate filing")
        row = self.candidate_row(business, identity)
        return _json_object((row or {}).get("filing_json") or {}, "A stored candidate filing")

    def _document_state(self, change, values, business):
        """The effective stored-plus-prepared document row, including immutable scope for an existing index update."""
        stored = self.uow.repository("documents", business_id=business).get(change.resource.resource_id)
        if change.action == "insert":
            if stored is not None:
                _forbidden("A prepared Context document insert would replace an existing document.")
            return dict(values)
        if stored is None:
            _forbidden("A prepared Context document update names an unknown document.")
        if stored.get("kind") != values.get("kind"):
            _forbidden("A prepared Context document update changes its immutable kind.")
        effective = {**stored, **values}
        if effective.get("kind") != "context_index" or effective.get("path") != "context/INDEX.md":
            _forbidden("A prepared Context document update is not the generated index.")
        return effective

    def _validate_context(self):
        """The composed Context intake must be exactly one ordered provenance chain bound to the accepted factual record."""
        if len(self.accepted_ids) != 1:
            _forbidden("A prepared Context filing must correspond to exactly one accepted factual learning record.")
        business, identity = next(iter(self.accepted_ids))
        actor = self.ctx.principal_id
        expected_initiator = self.ctx.initiated_by_principal_id if self.ctx.principal_kind == "system" else actor
        expected_namespace = "business:" + business if business else "global"
        documents = {change.resource.resource_id: (change, values) for table, change, values in self.auxiliary
                     if table == "documents"}
        versions = [(change, values) for table, change, values in self.auxiliary
                    if table == "context_source_versions"]
        filings = [(change, values) for table, change, values in self.auxiliary if table == "context_filings"]
        inputs = [(change, values) for table, change, values in self.auxiliary if table == "context_inputs"]
        if len(versions) != 1 or len(filings) != 1 or len(inputs) != 1 or len(self.auxiliary_jobs) != 1:
            _forbidden("A prepared Context intake does not carry exactly one version, filing, input and job.")
        version_change, version = versions[0]
        filing_change, filing_values = filings[0]
        input_change, input_values = inputs[0]
        job_change, job = self.auxiliary_jobs[0]
        filing = self.after_filing(business, identity)
        upload_id, job_id, snapshot = filing.get("upload_id"), filing.get("job_id"), filing.get("snapshot_hash")
        if not all(isinstance(value, str) and value for value in (upload_id, job_id, snapshot)):
            _forbidden("An accepted factual record carries no complete Context filing identity.")
        upload = documents.get(upload_id)
        if upload is None or upload[0].action != "insert" or upload[1].get("kind") not in UPLOAD_DOCUMENT_KINDS:
            _forbidden("An accepted factual record has no prepared Context upload document.")
        index = [entry for document_id, entry in documents.items()
                 if document_id != upload_id and entry[1].get("kind") in INDEX_DOCUMENT_KINDS]
        if len(index) > 1 or any(entry[1].get("path") != "context/INDEX.md" for entry in index):
            _forbidden("A prepared Context index document is unsupported.")
        if len(documents) != 1 + len(index):
            _forbidden("A prepared Context intake carries an unsupported extra document.")

        # Every auxiliary insert/update shape has the real admission's required fields; optional repository defaults are
        # not silently accepted as a substitute for a prepared value.
        for table, change, values in self.auxiliary:
            required = AUXILIARY_REQUIRED[table].get(change.action)
            if required is None:
                _forbidden("A prepared Context effect has an unsupported action.")
            _required_fields(table, required, values)
            if change.action == "insert" and values.get("business_id") != business:
                _forbidden("A prepared Context effect targets another scope.")
        _required_fields("jobs", JOB_KEYS, job)

        # File/document pairing and the exact effective document values. An update has no immutable scope in its values;
        # its scope comes from the ResourceRef above and its current stored row, and the immutable stored/prepared state
        # is validated together exactly as execution will see it.
        upload_change, upload_values = upload
        upload_values = self._document_state(upload_change, upload_values, business)
        upload = (upload_change, upload_values)
        document_states = {document_id: self._document_state(change, values, business)
                           for document_id, (change, values) in documents.items()}
        metadata = _json_object(upload_values.get("metadata_json"), "A prepared upload metadata")
        refs = upload_values.get("source_refs_json")
        expected_refs = [{"resource_type": "learning_candidates", "resource_id": identity, "business_id": business}]
        if not isinstance(refs, list) or refs != expected_refs:
            _forbidden("A prepared Context upload does not reference exactly its accepted factual record.")
        expected_files = {}
        for document_id, values in document_states.items():
            _text(values.get("path"), "A prepared Context document path")
            _hex_digest(values.get("content_hash"), "A prepared Context document content hash")
            _json_object(values.get("metadata_json"), "A prepared Context document metadata")
            if not isinstance(values.get("source_refs_json"), list):
                _forbidden("A prepared Context document has no source reference list.")
            if values.get("business_id") != business or values.get("namespace") != (business or "global"):
                _forbidden("A prepared Context document has another scope or namespace.")
            if values.get("owning_agent_id") is not None:
                _forbidden("A prepared Context document has an unsupported owning agent.")
            if values.get("availability") not in {"ready", "applying"}:
                _forbidden("A prepared Context document is not in a live file state.")
            _text(values.get("created_at"), "A prepared Context document creation time")
            _text(values.get("updated_at"), "A prepared Context document update time")
            expected_files[document_id] = values["path"]
        if metadata.get("by") != actor:
            _forbidden("A prepared Context upload was authored by another principal.")
        if metadata.get("format") != "learning-v1" or metadata.get("source") != "accepted-learning":
            _forbidden("A prepared Context upload has unsupported source metadata.")
        if _hex_digest(snapshot, "The accepted learning snapshot") != _hex_digest(
                metadata.get("source_snapshot_hash"), "The prepared upload snapshot"):
            _forbidden("A prepared Context upload is not the accepted factual snapshot.")
        if not upload_values["path"].startswith("context/_inbox/") or not upload_values["path"].endswith(".md"):
            _forbidden("A prepared Context upload has an unsupported path.")
        if any(document_states[entry[0].resource.resource_id].get("source_refs_json") != [] for entry in index):
            _forbidden("A prepared Context index carries an unsupported reference.")
        for entry in index:
            index_metadata = _json_object(document_states[entry[0].resource.resource_id].get("metadata_json"),
                                          "A prepared index metadata")
            if index_metadata.get("generated") is not True or index_metadata.get("tree") != "context":
                _forbidden("A prepared Context index document is unsupported.")

        # Effective snapshot and exact accepted source.
        decision = self.after_decision(business, identity) or {}
        candidate = self.candidate_json_after(business, identity) or {}
        snapshot_item = {**effective(candidate, decision), "id": identity, "business_id": business,
                         "decision": {"state": decision.get("state"), "note": decision.get("feedback")}}
        expected_body, expected_digest = intake_snapshot(snapshot_item)
        if snapshot != expected_digest:
            _forbidden("An accepted factual record carries a different learning snapshot than its effective state.")
        expected_envelope = envelope.source_document(
            expected_body, document_metadata(upload_values["kind"], metadata, refs, None))
        expected_source_digest = envelope.fingerprint(expected_envelope)

        # Source version: first version of this new upload, with the actual caller attribution and exact values.
        _identifier(version_change.resource.resource_id, "A prepared Context version identity")
        _identifier(version.get("source_id"), "A prepared Context source id")
        if version.get("source_id") != upload_id or version.get("source_kind") != "document" \
                or version.get("operation") != "add" or version.get("retracts_revision") is not None:
            _forbidden("A prepared Context source version does not admit the accepted upload.")
        _positive_int(version.get("source_revision"), "A prepared Context source revision")
        if version["source_revision"] != 1:
            _forbidden("A prepared Context source version is not the first revision of its new source.")
        _text(version.get("created_by"), "A prepared Context source creator")
        if version.get("created_by") != actor:
            _forbidden("A prepared Context source version attributes another creator.")
        source_path = "foundation/documents/" + upload_id + ".txt"
        if version.get("source_path") != source_path or version.get("source_digest") != expected_source_digest:
            _forbidden("A prepared Context source version does not record its exact accepted source digest.")
        if source_lineage(self.uow, "document", upload_id):
            _forbidden("A prepared Context source already has stored lineage; this is not a new upload.")

        # Filing: exact program, attributed caller, worker, request identity and prepared definition.
        _identifier(filing_change.resource.resource_id, "A prepared Context filing identity")
        _bounded_text(filing_values.get("request_id"), "A prepared Context filing request", 200)
        for field in ("job_id", "source_version_id", "input_id"):
            _identifier(filing_values.get(field), "A prepared Context filing " + field)
        if filing_values.get("job_id") != job_change.resource.resource_id \
                or filing_values.get("source_version_id") != version_change.resource.resource_id \
                or filing_values.get("input_id") != input_change.resource.resource_id:
            _forbidden("A prepared Context filing is not bound to its prepared version, input and job.")
        if filing_values.get("program_id") != "context.file" or filing_values.get("worker_id") != job.get("principal_id") \
                or filing_values.get("initiated_by") != expected_initiator:
            _forbidden("A prepared Context filing is not bound to its registered program and attributed caller.")
        definition = _json_object(job.get("definition_ref_json"), "A prepared job definition")
        _identifier(job_change.resource.resource_id, "A prepared Context job identity")
        _positive_int(filing_values.get("program_version"), "A prepared Context program version")
        _positive_int(definition.get("version"), "A prepared job program version")
        _hex_digest(filing_values.get("definition_digest"), "A prepared filing definition digest")
        if filing_values.get("definition_digest") != definition.get("digest") \
                or filing_values.get("program_version") != definition.get("version"):
            _forbidden("A prepared Context filing does not match its prepared program definition.")

        # Job: the queued registered program for exactly this filing and attribution.
        _text(job.get("principal_id"), "A prepared job worker")
        _text(job.get("dedupe_key"), "A prepared job dedupe key")
        _json_object(job.get("arguments_json"), "A prepared job argument set")
        if job.get("business_id") != business or job.get("target_namespace") != expected_namespace:
            _forbidden("A prepared Context job targets another scope.")
        if job.get("kind") != "context.file" or job.get("state") != "queued" \
                or job.get("initiated_by_principal_id") != expected_initiator:
            _forbidden("A prepared Context job is not the attributed queued filing job.")
        if job.get("arguments_json") != {"filing_id": filing_change.resource.resource_id}:
            _forbidden("A prepared Context job is not bound to the exact filing it was prepared for.")

        # Queue input: exact admitted source, event metadata, caller attribution and submission shape.
        _identifier(input_change.resource.resource_id, "A prepared Context input identity")
        _text(input_values.get("submitted_by"), "A prepared Context input submitter")
        if input_values.get("input_kind") != "general" or input_values.get("operation") != "add" \
                or input_values.get("submitted_by") != actor:
            _forbidden("A prepared Context input is not the attributed accepted general source addition.")
        _text(input_values.get("created_at"), "A prepared Context input creation time")
        payload = _json_object(input_values.get("payload_json"), "A prepared Context input payload")
        if set(payload) != {"operation", "path", "content", "metadata", "submitted_by"}:
            _forbidden("A prepared Context input payload has an unsupported shape.")
        if payload.get("operation") != "add" or payload.get("path") != source_path \
                or payload.get("submitted_by") != actor:
            _forbidden("A prepared Context input payload is not the attributed accepted source addition.")
        content = payload.get("content")
        if not isinstance(content, str):
            _forbidden("A prepared Context input payload has no source document.")
        parsed = envelope.parse(content)
        expected_metadata = document_metadata(upload_values["kind"], metadata, refs, None)
        if parsed["body"] != expected_body or parsed["metadata"] != expected_metadata \
                or envelope.fingerprint(content) != expected_source_digest:
            _forbidden("A prepared Context input payload is not the exact accepted learning source.")
        if payload.get("metadata") != event_metadata(metadata, upload_id):
            _forbidden("A prepared Context input payload does not carry the accepted document event metadata.")
        try:
            envelope.check_size(content, self.source_bytes)
            general_arguments("add", source_path, content, event_metadata(metadata, upload_id),
                              self.source_bytes, controlled=True)
        except DomainError as exc:
            raise DomainError("forbidden", RETIRED_MESSAGE + " The prepared Context source is not admissible.") from exc

        # The same ordering apply_records will visit: upload, job, version, filing, input, then the generated index.
        def position(table, resource_id):
            for index_value, change in enumerate(self.plan.changes):
                if change.kind == "record" and change.resource.resource_type == table \
                        and change.resource.resource_id == resource_id:
                    return index_value
            _forbidden("A prepared Context effect is missing from the plan.")
        upload_position = position("documents", upload_id)
        job_position = position("jobs", job_change.resource.resource_id)
        version_position = position("context_source_versions", version_change.resource.resource_id)
        filing_position = position("context_filings", filing_change.resource.resource_id)
        input_position = position("context_inputs", input_change.resource.resource_id)
        if not upload_position < job_position < version_position < filing_position < input_position:
            _forbidden("A prepared Context intake has an unsupported record order.")
        for entry in index:
            if position("documents", entry[0].resource.resource_id) < input_position:
                _forbidden("A prepared Context index is ordered before its accepted source.")

        # File effects are the supported managed-document write shape and carry the exact prepared bytes and hashes.
        seen_files = set()
        for change in self.files:
            if change.action != "write" or change.resource.resource_type != "managed_document_files":
                _forbidden("A prepared Context effect may only write a managed document file.")
            try:
                reference = json.loads(change.resource.resource_id)
            except (TypeError, ValueError) as exc:
                raise DomainError("forbidden", RETIRED_MESSAGE + " A prepared Context file has no document reference.") from exc
            if not isinstance(reference, dict) or set(reference) != {"document_id", "path"} \
                    or reference.get("document_id") not in expected_files:
                _forbidden("A prepared Context file does not belong to a prepared document.")
            if change.resource.business_id != business:
                _forbidden("A prepared Context file targets another scope.")
            if reference.get("path") != expected_files[reference["document_id"]]:
                _forbidden("A prepared Context file writes another path than its document.")
            if reference["document_id"] in seen_files:
                _forbidden("A prepared Context document has more than one file effect.")
            seen_files.add(reference["document_id"])
            values = _plain_mapping(change.values, "A prepared Context file")
            if set(values) != {"content_b64", "before_hash", "after_hash"}:
                _forbidden("A prepared Context file has an unsupported value shape.")
            encoded = values.get("content_b64")
            if not isinstance(encoded, str):
                _forbidden("A prepared Context file has no exact content.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise DomainError("forbidden", RETIRED_MESSAGE + " A prepared Context file content is not valid base64.") from exc
            digest = hashlib.sha256(data).hexdigest()
            document_change, _ = documents[reference["document_id"]]
            document = document_states[reference["document_id"]]
            if values.get("after_hash") != digest or document.get("content_hash") != digest:
                _forbidden("A prepared Context file content does not match its document row.")
            if document_change.action == "insert" and values.get("before_hash") is not None:
                _forbidden("A prepared Context file insert carries an unexpected previous hash.")
            try:
                frontmatter, body = split_document(data)
            except (DomainError, UnicodeError, ValueError) as exc:
                raise DomainError("forbidden", RETIRED_MESSAGE + " A prepared Context document is not readable.") from exc
            if plain(frontmatter) != document.get("metadata_json"):
                _forbidden("A prepared Context file frontmatter does not match its document metadata.")
            if reference["document_id"] == upload_id and body != expected_body:
                _forbidden("A prepared Context upload file does not contain the exact accepted learning snapshot.")
            rebuilt = document_bytes(document.get("metadata_json"), body)
            rebuilt_frontmatter, rebuilt_body = split_document(rebuilt)
            if plain(rebuilt_frontmatter) != document.get("metadata_json") or rebuilt_body != body:
                _forbidden("A prepared Context document file is not its exact registered serialization.")
        if set(expected_files) - seen_files:
            _forbidden("A prepared Context document has no file effect.")


def _validate_links(view, business, identity, state, seen=None, depth=0):
    """Validate every supported reconciliation/supersession structure of one candidate or history copy."""
    seen = set() if seen is None else seen
    if depth > MAX_GRAPH_DEPTH:
        _forbidden("A prepared learning relation is nested too deeply to inspect.")
    links = []
    for relation in state.get("reconciliations") or []:
        if not isinstance(relation, dict) or "earlier" not in relation:
            _forbidden("A prepared learning relation is malformed.")
        links.append(relation["earlier"])
        supersession = relation.get("supersession")
        if isinstance(supersession, dict) and supersession.get("replacement_id") is not None:
            links.append(supersession["replacement_id"])
    for key in ("reconciled_into", "superseded_by"):
        if state.get(key) is not None:
            links.append(state[key])
    supersession = state.get("supersession")
    if isinstance(supersession, dict) and supersession.get("replacement_id") is not None:
        links.append(supersession["replacement_id"])
    for link in links:
        embedded, target = None, link
        if isinstance(link, dict):
            embedded, target = link, link.get("id")
        if not isinstance(target, str) or not target:
            _forbidden("A prepared learning relation names an unresolved candidate.")
        if embedded is not None:
            if embedded.get("bucket") not in (None, CONTEXT_BUCKET) or _engine_marker(embedded):
                _forbidden("A prepared learning relation embeds a procedural or engine-linked candidate.")
            _validate_links(view, business, target, embedded, seen, depth + 1)
        _require_factual(view, business, target, seen, depth + 1)


def _require_factual(view, business, identity, seen, depth):
    if depth > MAX_GRAPH_DEPTH:
        _forbidden("A prepared learning relation is nested too deeply to inspect.")
    key = (business, identity)
    if key in seen:
        return
    seen.add(key)
    after = view.after_state(business, identity)
    if after is None:
        _forbidden("A prepared learning relation names an unknown learning record.")
    raw = view.candidate_json_after(business, identity)
    if raw is not None and _engine_marker(raw):
        _forbidden("A prepared learning relation names an engine-linked record.")
    if after.get("bucket") != CONTEXT_BUCKET:
        _forbidden("A prepared learning relation names a historical procedural record.")
    _validate_links(view, business, identity, after, seen, depth + 1)


def validate_saved_plan(ctx, plan, uow, *, source_bytes=None):
    """Refuse any retained learning mutation whose prepared effects would write or link procedural/engine learning."""
    view = _Effects(ctx, plan, uow, source_bytes=source_bytes)
    view.validate()
    return None