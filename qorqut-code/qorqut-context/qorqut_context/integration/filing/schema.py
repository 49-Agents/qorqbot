"""Durable ownership and completion of Context sources, composed once into the operational schema.

Four insert-only (or one-way) tables, all owned here and none of them a document table:

* ``context_source_bindings``: every Context source belongs to exactly one operational scope (one business, or the
  explicit Global namespace). Keyed by the canonical source identity (a GENERAL source's Context path, or an accepted
  Action's key) and created by the backend driver, from the stored queue input, before any Context work for that source
  starts. It is never updated or deleted.
* ``context_source_versions``: an accepted source snapshot. Within one ``(source kind, source ID)`` the revisions are
  consecutive from 1, the operation follows the accepted lineage (the first version is an ``add``; a later content
  version an ``edit``; a retraction a ``remove`` of one exact published version) and the frozen digest names the exact
  bytes submitted. Source identity is separate from any host row revision: nothing here changes when a host updates its
  bookkeeping.
* ``context_filings``: the one association of a source version with its originating request, its registered job (and
  that job's program definition and worker) and its queue input. Replaying an accepted filing reuses it.
* ``context_completions``: the terminal outcome of a filed input (``published`` or ``discarded``), recorded in the queue's
  own transaction, with its pending/applied/conflict application state. This is the completion outbox: a document
  or job adapter applies it later through its authorized operation, exactly once.

Every row is insert-only apart from a completion's application state, so a later version, retry or bookkeeping change
can never rewrite an earlier version's payload, filing or outcome.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

SOURCE_KINDS = ("general", "action")
# The filing adapters' source kinds and the controlled GENERAL path of each (``<prefix><source id>.txt``).
FILING_KINDS = {"document": "foundation/documents/", "instruction": "foundation/instructions/"}
PROGRAMS = ("context.file", "context.instruct")
COMPLETION_STATES = ("pending", "applied", "conflict")
HEX = "NOT GLOB '*[^0-9a-f]*'"

SCHEMA = (
    """CREATE TABLE context_source_bindings (
    source_kind TEXT NOT NULL CHECK (source_kind IN ('general','action')),
    source_key TEXT NOT NULL CHECK (length(source_key) BETWEEN 1 AND 500),
    business_id TEXT REFERENCES businesses(id),
    bound_by_input TEXT NOT NULL REFERENCES context_inputs(id),
    bound_at TEXT NOT NULL,
    PRIMARY KEY (source_kind, source_key)
)""",
    """CREATE INDEX context_source_bindings_by_scope ON context_source_bindings(business_id, source_kind, source_key)""",
    # A binding records the admitting input's own kind and namespace: nothing else can name a different owner.
    """CREATE TRIGGER context_source_bindings_admitting_input
BEFORE INSERT ON context_source_bindings
WHEN NOT EXISTS (SELECT 1 FROM context_inputs i WHERE i.id = NEW.bound_by_input AND i.input_kind = NEW.source_kind
    AND i.business_id IS NEW.business_id)
BEGIN
    SELECT RAISE(ABORT, 'A Context source binding records its admitting input''s kind and namespace');
END""",
    """CREATE TRIGGER context_source_bindings_immutable
BEFORE UPDATE ON context_source_bindings
BEGIN
    SELECT RAISE(ABORT, 'A Context source binding is immutable');
END""",
    """CREATE TRIGGER context_source_bindings_keep
BEFORE DELETE ON context_source_bindings
BEGIN
    SELECT RAISE(ABORT, 'Context source bindings are retained');
END""",
    # -- accepted source versions ---------------------------------------------------------------------------------
    f"""CREATE TABLE context_source_versions (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    business_id TEXT REFERENCES businesses(id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('document','instruction')),
    source_id TEXT NOT NULL CHECK (length(source_id) = 36),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    operation TEXT NOT NULL CHECK (operation IN ('add','edit','remove')),
    source_path TEXT NOT NULL CHECK (length(source_path) BETWEEN 1 AND 500),
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64 AND source_digest {HEX}),
    retracts_revision INTEGER CHECK (retracts_revision IS NULL OR retracts_revision >= 1),
    created_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    UNIQUE (source_kind, source_id, source_revision),
    CHECK ((operation = 'remove') = (retracts_revision IS NOT NULL)),
    CHECK (retracts_revision IS NULL OR retracts_revision < source_revision),
    CHECK (source_path = CASE source_kind WHEN 'document' THEN 'foundation/documents/' ELSE 'foundation/instructions/' END
                         || source_id || '.txt')
)""",
    """CREATE INDEX context_source_versions_by_scope ON context_source_versions(business_id, source_kind, source_id, source_revision)""",
    # Revisions are consecutive in one scope, and the operation follows the accepted lineage. The first version is an
    # add; after a retraction the source is added again; otherwise a content version is an edit. A removal needs a
    # prior version to retract. An earlier discarded add never turns a later edit into an add.
    """CREATE TRIGGER context_source_versions_lineage
BEFORE INSERT ON context_source_versions
WHEN NEW.source_revision IS NOT COALESCE((SELECT MAX(v.source_revision) FROM context_source_versions v
        WHERE v.source_kind = NEW.source_kind AND v.source_id = NEW.source_id), 0) + 1
  OR EXISTS (SELECT 1 FROM context_source_versions v WHERE v.source_kind = NEW.source_kind AND v.source_id = NEW.source_id
        AND v.business_id IS NOT NEW.business_id)
  OR NEW.operation IS NOT (CASE
        WHEN NEW.operation = 'remove' THEN 'remove'
        WHEN NOT EXISTS (SELECT 1 FROM context_source_versions v WHERE v.source_kind = NEW.source_kind
                 AND v.source_id = NEW.source_id) THEN 'add'
        WHEN (SELECT v.operation FROM context_source_versions v WHERE v.source_kind = NEW.source_kind
                 AND v.source_id = NEW.source_id ORDER BY v.source_revision DESC LIMIT 1) = 'remove' THEN 'add'
        ELSE 'edit' END)
BEGIN
    SELECT RAISE(ABORT, 'A source version continues its accepted lineage in the same scope');
END""",
    # A retraction names one earlier content version of the same source whose input was really published.
    """CREATE TRIGGER context_source_versions_retraction
BEFORE INSERT ON context_source_versions
WHEN NEW.operation = 'remove' AND NOT EXISTS (
    SELECT 1 FROM context_source_versions v
    JOIN context_filings f ON f.source_version_id = v.id
    JOIN context_inputs i ON i.id = f.input_id
    WHERE v.source_kind = NEW.source_kind AND v.source_id = NEW.source_id AND v.source_revision = NEW.retracts_revision
      AND v.operation != 'remove' AND i.state = 'published')
BEGIN
    SELECT RAISE(ABORT, 'A retraction names one exact published version of its source');
END""",
    """CREATE TRIGGER context_source_versions_immutable
BEFORE UPDATE ON context_source_versions
BEGIN
    SELECT RAISE(ABORT, 'A source version is immutable');
END""",
    """CREATE TRIGGER context_source_versions_keep
BEFORE DELETE ON context_source_versions
BEGIN
    SELECT RAISE(ABORT, 'Source versions are retained');
END""",
    # -- filings ------------------------------------------------------------------------------------------------------
    """CREATE TABLE context_filings (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    business_id TEXT REFERENCES businesses(id),
    source_version_id TEXT NOT NULL UNIQUE REFERENCES context_source_versions(id),
    request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 200),
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    program_id TEXT NOT NULL CHECK (program_id IN ('context.file','context.instruct')),
    program_version INTEGER NOT NULL CHECK (program_version >= 1),
    definition_digest TEXT NOT NULL CHECK (length(definition_digest) = 64),
    worker_id TEXT NOT NULL REFERENCES principals(id),
    initiated_by TEXT REFERENCES principals(id),
    input_id TEXT NOT NULL UNIQUE REFERENCES context_inputs(id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL
)""",
    """CREATE INDEX context_filings_by_scope ON context_filings(business_id, created_at)""",
    # The filing's job is the real registered job prepared for exactly this filing, in the same scope and with the same
    # program and worker; an instruction's source is its job. The input is inserted later in the same transaction.
    """CREATE TRIGGER context_filings_bound_job
BEFORE INSERT ON context_filings
WHEN NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = NEW.job_id AND j.business_id IS NEW.business_id AND j.kind = NEW.program_id
        AND j.principal_id = NEW.worker_id AND j.initiated_by_principal_id IS NEW.initiated_by
        AND json_extract(j.arguments_json, '$.filing_id') = NEW.id AND j.state = 'queued')
  OR NOT EXISTS (SELECT 1 FROM context_source_versions v WHERE v.id = NEW.source_version_id AND v.business_id IS NEW.business_id
        AND ((v.source_kind = 'document' AND NEW.program_id = 'context.file')
          OR (v.source_kind = 'instruction' AND NEW.program_id = 'context.instruct' AND v.source_id = NEW.job_id)))
BEGIN
    SELECT RAISE(ABORT, 'A filing binds its source version to its own prepared registered job');
END""",
    """CREATE TRIGGER context_filings_immutable
BEFORE UPDATE ON context_filings
BEGIN
    SELECT RAISE(ABORT, 'A Context filing is immutable');
END""",
    """CREATE TRIGGER context_filings_keep
BEFORE DELETE ON context_filings
BEGIN
    SELECT RAISE(ABORT, 'Context filings are retained');
END""",
    # A controlled adapter path holds only the input of a real filing: the admission creates the filing first, then the
    # input, and a public GENERAL submission under these prefixes is also refused by its validation.
    """CREATE TRIGGER context_filing_controlled_inputs
BEFORE INSERT ON context_inputs
WHEN NEW.input_kind = 'general' AND (json_extract(NEW.payload_json, '$.path') LIKE 'foundation/documents/%'
        OR json_extract(NEW.payload_json, '$.path') LIKE 'foundation/instructions/%')
  AND NOT EXISTS (SELECT 1 FROM context_filings f JOIN context_source_versions v ON v.id = f.source_version_id
        WHERE f.input_id = NEW.id AND f.business_id IS NEW.business_id AND v.source_path = json_extract(NEW.payload_json, '$.path')
          AND v.operation = NEW.operation)
BEGIN
    SELECT RAISE(ABORT, 'Controlled Context paths hold only the input of their own filing');
END""",
    # -- completions (the terminal outbox) ------------------------------------------------------------------------
    f"""CREATE TABLE context_completions (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    business_id TEXT REFERENCES businesses(id),
    filing_id TEXT NOT NULL UNIQUE REFERENCES context_filings(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    input_id TEXT NOT NULL UNIQUE REFERENCES context_inputs(id),
    source_version_id TEXT NOT NULL REFERENCES context_source_versions(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('published','discarded')),
    context_event_id TEXT,
    published_commit TEXT,
    reason_json TEXT CHECK (reason_json IS NULL OR json_valid(reason_json)),
    outcome_digest TEXT NOT NULL CHECK (length(outcome_digest) = 64 AND outcome_digest {HEX}),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','applied','conflict')),
    application_json TEXT CHECK (application_json IS NULL OR json_valid(application_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK ((state = 'applied') = (resolved_at IS NOT NULL)),
    CHECK (state = 'pending' OR application_json IS NOT NULL),
    CHECK (outcome != 'published' OR (context_event_id IS NOT NULL AND published_commit IS NOT NULL)),
    CHECK (outcome != 'discarded' OR (reason_json IS NOT NULL AND published_commit IS NULL))
)""",
    """CREATE INDEX context_completions_by_state ON context_completions(state, created_at)""",
    """CREATE TRIGGER context_completions_bound
BEFORE INSERT ON context_completions
WHEN NOT EXISTS (SELECT 1 FROM context_filings f JOIN context_inputs i ON i.id = f.input_id
        WHERE f.id = NEW.filing_id AND f.job_id = NEW.job_id AND f.input_id = NEW.input_id
          AND f.source_version_id = NEW.source_version_id AND f.business_id IS NEW.business_id
          AND i.state = NEW.outcome AND i.context_event_id IS NEW.context_event_id)
BEGIN
    SELECT RAISE(ABORT, 'A completion binds its filing, job, input and source version to the input''s terminal outcome');
END""",
    """CREATE TRIGGER context_completions_immutable
BEFORE UPDATE OF id, business_id, filing_id, job_id, input_id, source_version_id, outcome, context_event_id, published_commit,
    reason_json, outcome_digest, created_at ON context_completions
BEGIN
    SELECT RAISE(ABORT, 'A completion''s outcome is immutable');
END""",
    """CREATE TRIGGER context_completions_applied_final
BEFORE UPDATE ON context_completions
WHEN OLD.state = 'applied'
BEGIN
    SELECT RAISE(ABORT, 'An applied completion is final');
END""",
    """CREATE TRIGGER context_completions_keep
BEFORE DELETE ON context_completions
BEGIN
    SELECT RAISE(ABORT, 'Context completions are retained');
END""",
)

TABLES = {
    "context_source_bindings": TableSpec(("source_kind", "source_key"), "business_id", False, None),
    "context_source_versions": TableSpec(("id",), "business_id", False, None),
    "context_filings": TableSpec(("id",), "business_id", False, None),
    "context_completions": TableSpec(("id",), "business_id", True, "revision"),
}
IMMUTABLE_FIELDS = {
    "context_completions": frozenset({"business_id", "filing_id", "job_id", "input_id", "source_version_id", "outcome",
                                      "context_event_id", "published_commit", "reason_json", "outcome_digest", "created_at"}),
}
# The binding, the filing and the completion reference the input that carries them; a filing binds one registered job.
REQUIRED_TABLES = frozenset({"context_inputs"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("context-filing", TABLES, IMMUTABLE_FIELDS, SCHEMA, required_tables=REQUIRED_TABLES)
