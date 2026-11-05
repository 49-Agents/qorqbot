# Core backend contracts

This documents the delivered format-3 context-only backend, implementing
[INPUT_MODES.md](INPUT_MODES.md). It does not implement or prescribe
the separate operational platform. Format 3 adds the note collection, supervised
edits and question output below, and is the only format written. A format-2 history
is read as it is and upgraded only by its first new publication (see "Format 2
compatibility"). A repository configuration other than the exact format-2 or format-3
one, a published event that does not match the format of its publishing commit or has
no supported input mode, and a pending job of another format are rejected with a clear
error, without conversion or any change to them.

The local pipeline in [PROPAGATION.md](PROPAGATION.md) adds real model execution. Broader Qorqut
integration is host-owned (see [HOST_INTEGRATION.md](HOST_INTEGRATION.md)). The core Context
API still accepts controlled graph operations without running a model.

## Records

- A node is `{id, summary, attributes}`. An edge additionally has `from_id` and
  `to_id`. An edge's independent ID permits parallel edges and endpoint edits
  without replacing its history. Every record ID matches `[a-z0-9]{5}`. A note
  `{id, text, objects}` is a third, separate kind of record (see "Notes").
- Attribute lists contain strings, finite JSON numbers, or booleans. Preserve
  member order while removing exact, type-aware duplicates; `true`, `1`, and
  `"1"` stay distinct. Empty optional attributes disappear. `basic_entity_type`
  is a nonempty list of nonempty category strings. There are no mandatory edge
  attributes. Other structural fields are not extensible; use attributes.
- `graph/nodes/<id>.json` and `graph/edges/<id>.json` hold current records.
  `graph/catalogs/nodes.json` and `edges.json` retain all published attribute
  names and categorical string/boolean values, including setup seeds. Numbers
  are not enumerated or summarized by ranges.
- `graph/config.json` identifies format version 3 and the publication branch.
  Initialization creates config/catalogs in a distinct commit without a source
  change. An initially empty vocabulary still requires categories on nodes.

`graph/events/<id>.json` is immutable and contains:

- `id`, `format_version`, `base_revision`, and a `submission_digest` for
  detecting a reused event ID with different input.
- `operation` (`add`, `edit`, `remove`), `submitted_by`, and `source` metadata
  (`id`, `path`, `kind`, `title`, `origin`, optional `origin_ref`/`created_at`).
- `input_mode`: `GENERAL` or `OPERATIONAL_ACTION`, fixed for the source lifetime;
  `input_digest` binds the serialized source bytes (null for removal), and
  `previous_source_event` identifies its previous event/commit when present.
- Optional `record_context: {scope, provenance}`: immutable trusted-caller evidence
  present only when the caller supplied it (see "Controlled record context"). Events
  published without it are unchanged and remain valid; no key is written for `None`.
- Optional `execution`: present only on a controlled deterministic event and absent from
  every other. A graph edit has `{kind: "deterministic_graph_edit", plan_digest}` (see
  "Controlled deterministic graph edits"); a supervised edit has `{kind:
  "deterministic_supervised_edit", plan_digest, document_digest, plan}` (see "Supervised edits").
- Both modes have `handoff_note: {disposition, text}`. Disposition is `prepared` for
  nonempty text and `empty` for an explicit empty string. Missing disposition
  blocks publication. The event ID within this context is the note identity.
  An event without this field is never given an invented disposition; new
  publication requires it.
- OPERATIONAL_ACTION has `action` (opaque action/revision references, submitted
  pointers, immutable revision tags, optional optimistic previous commit),
  `action_associations` (one date/summary and the complete associations), and a
  host-derived `association_transition` with before/after/added/removed kind/IDs.
  Each association contains kind/id/explanation and automatic `self` citation.
  GENERAL rejects these action-only outputs. Full action text lives in the source envelope,
  not on each graph object or in a second action record.
- `changes`: one final change per affected record. Each has `kind` (`node`, `edge` or
  `note`), `id`, `operation`, `explanation`, `citations`, `support_after`, and `developments`.
- A development has `date`, `description`, and `citations`. Dates normalize to
  UTC with earliest missing components; wholly unknown development dates use
  numeric `0`. Unknown source creation dates are omitted.

`citations` explain an individual change (at most 15, with an explanation of at
most 150 words). `support_after` describes the object's complete remaining
support; it is not limited to the 15 citations attached to one explanation.
Deleting an object leaves no current support. A support-only update records a
change even when the current node/edge file stays identical. Developments can
also record meaningful history without changing current attributes for accepted
actions. GENERAL writes reject nonempty developments. Association history needs
no dummy graph write or additional current support citation.

References inside the event are full source-event commit IDs or `self`.
Public reads resolve `self` to that immutable event's introducing commit.
Initialization commits and unpublished/foreign commits are not source evidence.
Active support cannot refer to a source that has been deleted; historical
explanations can still cite that source's earlier commits. Semantic assessment
of support after a source edit belongs to the propagator, not a new
statement/verifier system in the backend.

## Notes

A note is one atomic, source-supported proposition or closely connected account that
is not a graph entity: a reported happening, request, intention, observation or
qualified or uncertain information. It is a separate Context record, never a node,
edge, event or claim type, and it adds no confidence field or taxonomy.

- **Record.** Exactly `{id, text, objects}`. `id` matches `[a-z0-9]{5}`; `text` is
  nonblank valid UTF-8 within the source byte limit, kept as written (dates, attribution,
  intentions and uncertainty belong in it); `objects` is a list, possibly empty, of
  `{kind: "node" | "edge", id}` links. A repeated link collapses and link order is
  preserved. Any other field, a note or event link, or a malformed ID is refused.
- **Storage.** `notes/<id>.json` holds the current note. Notes are not under `graph/`,
  have no catalog entries, are not traversed by `neighbors`, and are not counted as graph
  objects. `Snapshot.notes` maps each note ID to its record. An event's `changes` list
  records `kind: "note"` entries with the same `operation`, `explanation`, `citations`,
  `support_after` and (always empty) `developments` as other kinds.
- **IDs.** One namespace covers nodes, edges, notes and events. `put_note` without an
  `id` allocates an unused one; an ID is never reused, even after removal. A supplied
  existing note ID replaces that note and keeps its identity and history.
- **Links.** A link states relevance only; links to several nodes do not imply edges
  among them, and a link to an edge describes that relationship. Validation at
  publication requires every link to name a node or edge of that kind in the final
  draft or one **published at some earlier time** (a historical, since-removed target
  stays linkable and is reported with status `removed`); an invented ID, the wrong
  kind, or an object created and withdrawn within the same draft is refused. Removing a
  node or edge never changes a note: the note stays and its link reads as removed.
  `Snapshot` refuses a published note or event whose links name something never published.
- **Support and reassessment.** Evidence is exactly as for other records: `self` or
  full source-event commits, never a note ID. A current note needs supporting source
  events; one source may support many notes and a note may be shared by several
  sources (list each in `support_after`). `supported_by(source_id)` and
  `required_reassessment` include notes, so editing or removing a source requires each
  note it supported to be kept with remaining support, revised, or withdrawn. A note
  that loses its last support must be removed.
- **Reads.** `read("note", id)` returns `{record, support, revision, links}` with
  each link's `status` (`current` or `removed`); `history(id, kind="note")` returns the
  ordinary before/after records, support, explanation and citations, including a
  support-only change and removal; `linked_notes(kind, id, offset, limit)` pages the
  current notes linking a current or historical node or edge, in note ID order, with
  `current` telling whether the target still exists. Pinned snapshots show the notes as
  they were. Search projects each note's text as `kind: "note"`; it has no attributes.
- **Modes.** GENERAL and OPERATIONAL_ACTION events may both write notes. A note never
  records developments, creates or changes an Action or its associations, or widens
  any related-object pointer.
- **Tools.** The event tools add `put_note`, `remove_note` and `list_linked_notes`,
  accept `note` in `read_object`, `list_objects`, `search_context` and `object_history`,
  and let a node or edge read show the IDs of the notes that link it. A tool link is
  checked immediately against known published records and the staged draft; the final
  check at publication is authoritative.
- **Bootstrap.** The bootstrap sample adds `notes`: an unranked, bounded (40 notes,
  600-character excerpts) selection of existing notes that link a selected node or
  sampled edge or share a word with either source version, with counts and a notice.
  It is a starting sample, not the full set; the model searches `kind: "note"` for more.

## Source and draft operations

`Context.begin(...)` stages one GENERAL foundation addition/edit/removal and
returns a receipt with stable event/source IDs. Input bytes are retained unchanged;
source paths must stay inside `foundation/` and use `.md` or `.txt`. Metadata
is in the event. Text/frontmatter/metadata cannot select action mode. UTF-8
source bytes and handoff notes obey the saved source limit (default 256 KiB).
Identical-byte edits are rejected as metadata-only operations. It also accepts
optional `record_context` and `expected_revision` (below); neither changes the
stored source bytes.

`stage_handoff_note(event_id, text)` replaces one durable draft note in either
mode, including explicit empty disposition when nothing needs handing off.
The note is the operational hand-off for the eventual main agent, not a place for
questions or a copy of Context notes: requested or proposed work, claims of completed
operational changes it may need to verify or act on (pointing at relevant note IDs), and
the reason for a blocked finish. Uncertain identities, relationships, attributes, dates
and interpretations are kept as Context notes, and what the owner could resolve is filed
as question output. Accepted-action notes clarify unresolved details without routine
reapproval of accepted facts. GENERAL edit/removal notes describe changes/withdrawals of
raw operational claims, without changing accepted actions. Note-only events are
valid (an event may change only notes). Meaning is interpreted by the model or controlled caller: ordinary state
may support the graph; raw action claims, requests, and ambiguous operational
material belong only in the note. Retrieved raw evidence retains that policy.

`Context.begin_action(...)` stages controlled-caller accepted text with an opaque
`action_ref`, optional `revision_ref`, optional related-object pointers,
optional `tags` snapshots, recorder and optional event ID. It is not authentication.
References are nonempty and at most 256 UTF-8 bytes; pointer kinds are node/edge,
IDs use the normal five-character syntax, and optional text is `explanation`.
Absent IDs remain unresolved suggestions. Tags are a list of `{id, name,
business_id}` snapshots (default `[]`); business_id may be null. Tag/business IDs
are opaque nonempty strings of at most 256 UTF-8 bytes, names are nonempty, and
tag IDs are unique within a snapshot. They are grouping metadata, never inferred
graph relationships or a requirement to create a node.

The deterministic UTF-8 JSON `.txt` envelope preserves exact action text,
references, pointers and tags. Tag-only revisions therefore change canonical
source bytes. Text and serialized envelope each obey the source byte
limit. Evidence uses reserved `foundation/actions/<source-id>.txt`; GENERAL
cannot write there. Published events derive the action-ref/source map, including
withdrawals. Edit/remove retains identity; add conflicts even after withdrawal.
The optional `expected_previous_commit` checks edit/remove concurrency; exact
event replay is resolved first. Revision references have no sequence semantics.
Removal supplies no text or pointers; its revision tags are preserved in the
immutable event and submission digest. Restoration is deferred.
`begin_action` also accepts optional `record_context`; for add/edit it is a
separate `record_context` member of the canonical envelope, beside (never inside)
the text, pointers and tags, and a removal retains it in its event and digest.

## Controlled record context and expected base

`record_context` (`Context.begin`/`Pipeline.submit` and `Context.begin_action`/
`Pipeline.submit_action`) is exactly `{scope, provenance}`. `scope` is exactly
`{"kind": "global"}` or `{"kind": "business", "business_id": <opaque ref>}`, where
the reference is nonempty and at most 256 UTF-8 bytes like other opaque references.
`provenance` is a JSON object of strictly JSON-compatible values (`null`, booleans,
numbers that are finite, valid-UTF-8 text, lists, objects with text keys). Nothing
is converted: a tuple, set, bytes, non-text key, non-finite number, unpaired
surrogate or other object is refused, and the canonical serialization must fit the
source byte limit. `None` (the default) means no supplied context; no scope is ever
inferred from tags, text or metadata. It is trusted caller evidence only: it creates
no permission, business graph, tag ownership or model-write capability, and tags
remain ordinary labels.

A supplied value is copied, validated, and bound into the submission digest
(canonical JSON, so key order is not identity). It is kept in the pending job and
the draft digest, written to the published event as `record_context`, and recovery
publishes the saved value. GENERAL source content stays byte-for-byte the
submitted bytes; the context is event metadata only. An Action add/edit also places
it in the canonical envelope, so a change of context alone is a new source
revision; a removal has no new source bytes but its event keeps it. Reusing an
event ID with a different, added or removed `record_context` is the usual
different-input conflict, for published and pending events alike. Absent evidence
leaves a submission's digest identical to the one earlier code computed.
`Snapshot.event`/`source_change`, source `history`, the Action projections
(`action`, `actions_history`, `action_timeline`), the draft inspection and the
`read_draft`/`read_evidence`/`object_history` event tools expose it when present,
and `source_change.record_context` is part of the pipeline's model payload.
`EventTools` accepts no argument that could set or change it. The propagator
prompts are not changed by this extension.

`expected_revision` (`Context.begin`/`Pipeline.submit` only) is an optional full
published commit: 40 or 64 lowercase hex characters, never `self`, a branch or
another revision expression. For a NEW event, under the same lock that stages it,
it must equal the current published head; otherwise `begin` raises `Conflict`
before any pending state, input file or model call exists. The expectation is bound
into the submission digest and kept in the pending job, whose `base_revision`
(the head it matched) is preserved by a matching retry. It is not recorded in the
event, which already carries that base. An already published event is resolved
before the comparison, so replaying the same submission with the same original
expectation after the head advances returns the original publication with no
write and no model call; a changed, added or removed expectation for that event ID
is a different-input conflict. This is not a new input mode, graph writer,
automatic rebase or migration switch.

`stage_action_associations` replaces the full shared date/summary/association
draft. Summary and each explanation are nonempty, at most 150 words. Add/edit
may explicitly have no associations when no suitable subject exists; supplied
targets must exist in history or this draft.
Removed historical objects need not be recreated. Removal has no current
associations, retains the previous date and records withdrawal in its summary.
The canonical action timeline remains separate from graph developments.

`put_node`, `put_edge`, `remove_node`, and `remove_edge` stage changes against
that pending event. Node/edge writes supply an explanation, explanation
citations, the complete desired support set, and optional developments. Basic
operations can be composed into a merge; there is no merge API. The final graph
must have existing endpoints, including after node deletion. Reverse, parallel,
and self-referencing directed edges are supported.

There is one pending event and one local writer for the shared context. Durable runtime
state, source bytes, and the draft sit outside the published repository and
outside disposable indexes. Reusing a published ID for a new record is rejected.
Retries of the same submission keep the event ID and must retain the same input.

Each local Git repository binds to one canonical absolute runtime directory in
`<Git common directory>/qorqut-context/runtime.json`. Its nonblocking writer lock
is the adjacent `writer.lock`; runtime/index operations acquire that lock before
the runtime's own lock. Both repository-to-runtime and runtime-to-root ownership
are validated before new markers or pending/index state are written. Different
resolved runtime paths, incompatible owners, and corrupt markers are conflicts;
equivalent symlink paths are accepted. There is no automatic rebinding, migration,
or repair. Pure pinned Git reads do not need a runtime lock.

The common directory is resolved through Git, including when `.git` is a file.
Coordination metadata is local and never enters published context commits;
jobs, source input, drafts, and indexes remain in the separate runtime directory.
Initialization prepares the empty Git repository, then takes the same locks and
rechecks for existing context or unrelated files before claiming runtime state
or publishing setup. A conflicting initialization preserves existing files.

## Controlled deterministic graph edits

`Pipeline.submit_edit(plan, *, submitted_by, event_id, record_context=None)` (over
`Context.begin_edit(plan, *, submitted_by, event_id, source_bytes=..., record_context=None)`)
executes one fixed, typed graph plan as a controlled GENERAL event. **Trusted callers
only**: nothing authenticates or authorizes the caller, there is no CLI command, no model
tool and no queue, and a model can neither write the plan nor select this path. The host
that calls it owns authorization, and the library grants no permission, ownership, scope
or write capability. `event_id` is required, caller-supplied and five-character; the
caller's `submitted_by` and optional `record_context` are bound and recorded exactly as for
`begin`. No model, bootstrap sample, propagation attempt, runner call or usage receipt is
involved in admission, retry or recovery.

**Plan** (a JSON object; an unknown field, a missing field or an unsupported verb is
refused before anything is staged):

```json
{"format": "qorqut-graph-edit-v1",
 "expected_revision": "<full published head commit>",
 "rationale": "<nonempty text>",
 "operations": [<one or more operations>]}
```

- `put_node`, `put_edge`: exactly `op`, `record`, `explanation`, `citations`,
  `support_after`. `record` is the complete node/edge record of the ordinary schema
  with its caller-supplied five-character `id`; it creates, replaces, or (with an
  identical record and a different complete `support_after`) changes only support.
- `remove_node`, `remove_edge`: exactly `op`, `id`, `explanation`, `citations`. The
  object must be current at the base; the resulting support is empty.
- An object ID may appear once in a plan (node and edge IDs share one namespace). IDs
  are never allocated, translated or inferred; a created ID must be unused and
  different from the event ID. Removals never cascade: incident edges must be
  removed or replaced explicitly, and a node merge is composed from explicit puts and
  removals. `home_brief_patches`, `home_brief` deltas, implicit `home_ref`,
  developments and impacts do not exist in this vocabulary, and no old request is
  translated.
- The existing validators and limits apply unchanged: the record schema and
  `basic_entity_type`, explanations of at most 150 words, 1-15 citations, endpoint and
  support checks, and the support/source reassessment rule. Citations and support are
  `self` (this event's source) or full published source-event commits at the base. An
  operation that would change nothing is refused, so the recorded `changes` are exactly
  the plan's operations. Values are normalized by those validators (for example
  duplicate citations collapse) before the plan is encoded.
- The canonical encoding (`encode`: sorted keys, indented UTF-8 JSON) of the whole
  plan must fit the configured source byte limit; there is no second limit or truncation.

**Base.** `expected_revision` must equal the published head when a NEW event is
admitted, under the same lock that stages it; otherwise `Conflict` is raised before any
pending state exists, and the plan is never rebased. The whole plan is validated
against that base at admission, so an unexecutable plan is refused instead of leaving
pending work. A pending event whose head later moves is retained and refused, like any
other draft.

**Source and marker.** The canonical plan bytes are an add-only GENERAL source at the
reserved path `foundation/edits/{event_id}.txt` with metadata `{kind: "graph_edit",
title: "Explicit graph edit", origin: "controlled_caller"}`. `Context.begin`/`Pipeline.submit`
refuse that prefix for add, edit and remove, so ordinary text, including text that is
exactly a plan, can never create this source or select deterministic execution. The
pending job records `execution: {kind: "deterministic_graph_edit", plan_digest}` in the
same atomic write that binds the source; `plan_digest` is the digest of the canonical
plan. The marker joins the submission digest, the draft seal (`inspect_draft`), the
published event and source `history`. The readers (the pending-job reader, publication
and `Snapshot`) require the reserved path, the marker, the fixed metadata, the
add-only GENERAL event and canonical bytes whose digest matches the marker and whose
`expected_revision` is the event's base to appear together; a published edit's recorded
`changes` and empty handoff note must also match its plan. Any other combination is
refused.

**Execution.** `Pipeline.retry` (and `submit_edit` itself) dispatches from the validated
marker before any runner, bootstrap or attempt exists. It stages exactly the plan's puts
and removals plus an explicit empty handoff note through the ordinary draft primitives,
validates the draft with `inspect_draft(validate=True)`, and publishes through the
ordinary sealed publication. An interruption during staging leaves a partial draft that
an explicit retry of the same plan completes; a staged entry that is not exactly an
entry of the plan (an extra key, an altered record, support, citation or development) is
refused and retained, and the handoff note is always reset to empty. `Context.publish`
enforces the same equality, so a lower-level caller cannot publish a draft that differs
from the normalized plan, including its evidence and empty handoff note.

**Recovery and replay.** `recover` stays mechanical: it finishes a published event and
otherwise retains the pending edit for an explicit retry or discard; it never executes
a plan or starts a model. A crash after Git publication is completed with the original
commit and creates no second event. An exact replay (same plan, `submitted_by`,
`record_context` and event ID) returns the original publication after the head advances;
a changed plan, base, attribution or context under that event ID is a different-input
`Conflict`, published or pending.

**Receipt.** The ordinary published receipt (`status`, `event_id`, `source_id`,
`base_revision`, `published_commit`, `input_mode`, `handoff_disposition`,
`result_counts`, `recovered`) with `execution` set to the marker, `model: null`,
`diagnostics: null` and no `model_execution`. No attempt directory, `receipt.json` or
usage receipt is created, so `usage_receipts` is an empty page for the event; the marker,
not that empty page, establishes that no model ran. Failures after admission are
`PropagationError` with phase `recovery` (marker and binding checks), `edit_staging`,
`edit_validation` or `publication`, and `diagnostics: null`. Ordinary model receipts carry no `execution` key.

Scope: this is the library half only. Wiring a host `context_edit` action to
`submit_edit`, its authority and object-ownership rules, are outside this library.

## Supervised edits

`Pipeline.submit_supervised(plan, *, content, submitted_by, event_id, record_context=None)` (over `Context.begin_supervised(...)`) executes a manually authored
batch as one controlled GENERAL event with **no model**: a supporting Markdown document plus
explicit node, edge and note operations, published as ordinary evidence-backed filing.
**Trusted callers only**, exactly as for graph edits: nothing authenticates the caller, no
model tool selects this path, and the host that calls it owns authorization and the
approval conversation. The legacy `qorqut-graph-edit-v1` route is unchanged and does not
accept note operations.

**Plan.** `{format: "qorqut-supervised-edit-v1", expected_revision, rationale, operations}`
with the same strictness as a graph edit. Operations are `put_node`, `put_edge`, `put_note`
(exactly `op`, `record`, `explanation`, `citations`, `support_after`; a note record is the
complete `{id, text, objects}` with a caller-supplied ID) and `remove_node`, `remove_edge`,
`remove_note` (exactly `op`, `id`, `explanation`, `citations`). IDs are never allocated; an ID
may appear once; removals do not cascade; no developments exist. The existing validators
and limits apply unchanged, an operation that would change nothing is refused, and
`self` cites the supporting document. The canonical plan and the document each obey the
source byte limit. Whether a plan should cite `self` is the reviewer's decision; preview
reports `cites_supporting_document`.

**Document and source.** `content` must be nonempty UTF-8 bytes. It is preserved exactly,
including line endings, as the add-only source `foundation/supervised-edits/{event_id}.md`
with metadata `{kind: "supervised_edit", title: "Supervised edit {event_id}", origin:
"controlled_caller"}`, so ordinary source reads, evidence reads and search expose the prose.
`Context.begin`/`Pipeline.submit` refuse that prefix, and text that looks like a plan selects
nothing.

**Marker and binding.** The pending job and published event carry `execution: {kind:
"deterministic_supervised_edit", plan_digest, document_digest, plan}`: the normalized plan, its
digest and the SHA-256 of the exact document bytes. The marker joins the submission digest, the
draft seal and source history, and the readers (pending-job reader, publication and `Snapshot`)
require the reserved path, the marker, the fixed metadata, an add-only GENERAL event, document
bytes matching `document_digest` (which is the event's `input_digest`) and a plan whose
`expected_revision` is the event's base to appear together. A published event's `changes` and
empty handoff note must equal the plan's, so the intended edits can be verified independently
of the prose. This is execution provenance, not a second knowledge format.

**Record verification.** Change metadata is not enough: the event's own commit tree must hold
exactly the plan's records. Each `put` must be present as the plan's normalized record bytes at
its record path, and each removal must be absent. The check reads only those paths, runs before
the commit is promoted to the published ref, and runs again whenever the event is read from
history (`Snapshot`, capture, restore and recovery), so an inconsistent commit is refused with a
`ContextError` and never published or accepted. It looks only at the event's own commit, never
at the latest snapshot, so later legitimate edits and removals of those records stay readable.
The legacy graph-edit route is unchanged.

**Preview.** `Context.preview_supervised(plan, *, content, submitted_by, event_id,
record_context=None, source_bytes=...)` (and `Pipeline.preview_supervised`) validates the same
input and the proposed batch against the published head without a lock, pending state,
reserved ID, index update, initialization, recovery or model, and returns
`{revision, plan, document_digest, ...}`: `revision` is the inspected published head and `plan`
the normalized plan. It also returns `status` (`ready`, `pending` or `published`, with
`published_commit`), `source_path`, `plan_digest`, `document_bytes`, `submission_digest`,
`rationale`, per-operation `operations` (`op`, `kind`, `id`, `operation` add/edit/remove,
`before`/`after` records, `support_before`/`support_after`, `explanation`, `citations`),
`result_counts` and `cites_supporting_document`. Invalid input is a `ContextError`; a stale
`expected_revision`, a pending event for something else or a reused event ID is `Conflict`.
An exact retry of an already admitted or published event previews as `pending`/`published`
without a fresh-head check.

**Approval boundary.** Preview output is not an authorization. The engine cannot establish
conversational approval and cannot prevent files from changing between an earlier preview and a
later explicit apply. The trusted caller reads each file once and submits those bytes and the
parsed plan unchanged. `submission_digest` and `document_digest` are informative: they identify a
proposal but are never an approval grant and nothing enforces them against an earlier preview.
Binding stays with the existing mechanisms: `expected_revision`, the submission, input and draft
digests, and exact-event retry.

**Execution, replay and recovery.** Admission validates the whole plan against the published
head under the writer lock, so an unexecutable or stale plan leaves no pending work and is
never rebased. Execution stages exactly the plan's operations plus an explicit empty handoff
note through the ordinary draft primitives, validates, and publishes with the sealed
digest; a partial draft is completed by `retry`, any draft content outside the plan is
refused and retained, and `Context.publish` enforces the same equality for lower-level
callers. Questions cannot be attached to a supervised event. `recover` stays mechanical and
never executes a plan. Exact replay (same plan, document, `submitted_by`, `record_context` and
event ID) returns the original publication after the head advances, and any difference under
that event ID is a `Conflict`. No bootstrap, runner call, attempt directory or usage receipt
exists on any path.

**Receipt.** The ordinary published receipt (including `result_counts` with a `note` entry) with
`execution` set to the marker (so it carries the plan and document digest), `model: null`,
`diagnostics: null` and no `model_execution`. Failures after admission are `PropagationError`
with phase `recovery`, `edit_staging`, `edit_validation` or `publication`.

**Command line.** `preview-supervised` and `submit-supervised` accept `--plan-file`,
`--document-file`, `--submitted-by`, `--event-id`, optional `--record-context` (JSON) and
`--source-bytes`. Repository and runtime are the global `--repo`/`--runtime`; both commands, and
`question-output` and `ack-question-output`, require an explicit `--runtime` and refuse its
omission before any Context construction or filesystem access (legacy commands keep their
default runtime). Neither supervised command can reach a model.

## Question output

Questions ask the owner for what is missing or contradictory. They are **not Context data**:
never a node or edge attribute, a note, foundation evidence, nor part of an event or handoff
note. This engine produces and hands them off; the external collection that stores, edits,
answers and deletes them is another component.

- **Emit.** The event tool `emit_question(text, related=[])` (and
  `Context.emit_question(event_id, text, related=None)`) adds one entry
  `{text, related: [{kind: "event" | "node" | "edge" | "note", id}]}` to the pending event's
  output; nothing can edit or remove one. `text` is nonblank UTF-8; a repeated reference within
  one entry and an exact duplicate entry collapse. An `event` reference names this event or a
  published event; a node, edge or note reference must be in the current draft or known
  published history (a since-removed record is valid). Invented or wrong-kind IDs, and a record
  created and withdrawn within the draft, are refused, at emit time and again for the final draft
  when it is validated for publication. The serialized list must fit the source byte limit. A
  question changes no record, creates no Action, and broadens no related-object authority.
- **Staging.** While the event is pending the output is held in the runtime's pending job and is
  part of the draft seal (`inspect_draft` reports `questions` when any were emitted, and a later
  emission invalidates a sealed digest). A new model attempt starts with none, so a failed
  attempt cannot leak stale questions; a ready draft published on retry keeps the questions of
  the attempt that completed. Discarding the event discards them. Controlled deterministic
  edits (graph or supervised) cannot carry questions.
- **Outbox.** Only after the event is in Git is its output promoted to
  `<runtime>/questions/<event_id>.json`, `{format_version, event_id, published_commit,
  questions}`, outside the repository, written atomically and idempotently by recovery. It
  survives publication cleanup until acknowledged. The outbox is runtime authority: capture,
  verify and restore carry it, and every entry must match a published event.
- **Read.** `Pipeline.question_output(event_id)` (and `Context.question_output`) returns
  `{event_id, published_commit, questions}`. An event that published with no emitted
  questions, or whose payload was acknowledged, yields an empty list. An unpublished, failed,
  discarded or unknown event, or a published event whose completion is still pending (recover
  it first), raises `Conflict` and exposes nothing. Reading never calls a model, so retrying a
  completed event does not run one merely to recover questions.
- **Acknowledge.** `acknowledge_question_output(event_id, published_commit)` removes the payload
  after the caller has delivered it elsewhere; it is idempotent (`removed` reports whether a
  payload existed) and refuses a commit other than the event's own. Delivery to the external
  store and its own retry ledger are the host's.
- **Source text.** A source that itself contains question text is ordinary evidence preserved
  byte for byte; it is unrelated to generated question output.
- **Commands.** `question-output <event-id>` and `ack-question-output <event-id> <commit>`.

## Publication and recovery

Readers pin a published Git commit. Current checked-out files are a convenience;
reads use Git objects so an interrupted checkout/index refresh cannot expose a
mixed or unpublished state.

Publication validates the final graph and references, constructs a complete Git
tree using a separate temporary Git index, creates one commit with the saved
base as parent, then advances the published ref only if its base still matches.
It never commits a source separately or changes an earlier event. A no-impact
source event still has a source change and an empty `changes` list.

Recovery checks published events first. If the event is present, finish checkout,
indexing, and bookkeeping without republishing or rerunning propagation. Use a
safe two-tree checkout that refuses to overwrite unrelated edits; initialization
uses a Git-generated empty tree as its base. For a partially materialized published
checkout, inspect only paths changed between the saved base and published target.
Under Git's normal index lock, validate their staged entries and reconcile entries
still at the base only when working bytes/mode already match the target, including
an absent deleted file. Atomically install the updated index copy while preserving
unrelated entries, then release the lock before the safe checkout. Unknown working
or staged changes and existing Git locks remain explicit errors; recovery neither
overwrites them nor removes their locks. If unpublished,
retain the input/draft and error for explicit retry or discard; do not loop model
calls. A changed base is an error. Discarding an unpublished event does not
change the published repository. Successful temporary drafts are removed only
after all completion work succeeds.

## Format 2 compatibility

Format 2 (no notes, supervised edits or question output) is read, never migrated.

- **Reads.** A `Snapshot` accepts exactly `{"format_version": 2 | 3, "published_ref": "refs/heads/main"}` (the
  version must be an integer) and exposes it as `format_version`. Each published event must carry the format of
  `graph/config.json` in the commit that published it, and the format never decreases along the history, so
  changing an event's version field is no bypass (events are also immutable). A format-2 snapshot holds no notes
  and its events record no note changes and are not supervised edits; a format-3 snapshot may retain format-2
  events. Sources, graph records, support citations, history, search and old revisions read unchanged.
- **First write.** Every new event is format 3. When the base snapshot is format 2, the publication commit also
  sets `graph/config.json` to format 3, in the same atomic commit as the source event and record changes: no
  separate migration commit, no new IDs, no rewritten event. Plain reads, supervised previews and abandoned or
  invalid drafts change nothing, and old revisions keep their format-2 configuration. Ordinary recovery handles an
  interruption around that publication exactly as for any other.
- **Operator guidance.** Nothing needs refiling or backfilling; old records survive and notes and questions arise
  from future filings. After the first format-3 write the older engine cannot read the head, so finish any unfinished
  format-2 job with the older engine before the first write by this one. This engine never performs that cutover.
- **Pending work.** Pending jobs and question outbox payloads are format 3 only. An unfinished format-2 job fails
  with `Unsupported pending event format version 2` and the instruction above; it is not converted, discarded or
  replayed, and capture of a Context holding one is refused.

## Recovery capture and relocated restore

`qorqut_context.recovery` captures, verifies and restores a Context's authority as one component: the Git
repository and its separate runtime together. It never starts a driver, model call, queue replay, publication,
index rebuild or approval, and it repairs nothing; every pending input, unresolved attempt and usage receipt is
preserved byte for byte.

`capture(repository, runtime, destination, *, barrier, runtime_locks=())` requires a caller-supplied writer
barrier: any object whose `verify_active()` raises unless every writer of this Context is stopped or fenced right
now (its optional mapping result is recorded). The library calls it before and after copying and never turns a
boolean into proof. It additionally takes the Context's own non-blocking propagation, repository-writer and
runtime-writer locks, so a running writer is a retryable `writer_active` refusal. `runtime_locks` names a host's own
advisory `*.lock` files directly in the runtime (the host queue driver's): each must be empty, is held while it
exists, is recorded in the manifest and is never captured.

Every file is classified; only named classes are accepted and anything else fails the capture as
`unclassified_authority` instead of being dropped.

| Class | Examples | Treatment |
| --- | --- | --- |
| authority | `pending.json`, `events/<id>/input.bin`, `questions/<event>.json` (undelivered question output), `runs/<event>/<attempt>/*` (attempts, receipts, usage `calls/`, source changes) | copied exactly, hashed; each question entry must match a published event |
| Git | all refs, the full object closure from every ref tip and every object only retained runtime state names, reflogs, normalized `config` | one self-contained pack, refs, verbatim logs |
| derived | the search index, Git's index, template and accelerator files | recorded in the manifest, not captured, rebuilt by the owner |
| lock / transient | engine locks, declared host locks, `.write-*` and Git temporaries | held or ignored, never captured; an in-progress file is a retryable `source_changing` |
| refused | alternates, links, hardlinks outside the Git directory, `remote` configuration, unknown files, a dirty checkout the engine cannot account for | the capture fails |

The capture directory appears only complete: it is built beside its destination, verifies itself in a private
scratch repository and is then renamed. `manifest.json` records the format and component versions, source
repository/runtime identities and both ownership markers, the published ref and commit, event/pending/usage
cutoffs, the ref list, closure digest, pack, logs, every runtime file with size and SHA-256, derived and transient
files, both barrier results and the before/after source fingerprint. A fingerprint change, a missing or unreadable
required file, or a missing object fails with a typed `RecoveryError`. The component's `context_format` is the
captured head's own format, 2 or 3 (a history with format-2 events and a format-3 head is 3); `verify` and
`check_pair` require it to equal the format of the captured head snapshot, so a completed format-2 Context captures,
verifies and restores as it is, while its pending work is format 3 only.

`verify(directory, *, expected_manifest_sha256)` needs neither the source nor the barrier and changes nothing: the
manifest digest, the exact file set and hashes, a strict index of the pack, every ref, the closure, `fsck`, the
engine's own format and event-integrity checks and the pending job's checks.

`restore(directory, *, target_repository, target_runtime, expected_manifest_sha256)` verifies first, then restores
into explicit new or empty targets that are separate from the capture and are never read from it. The restore is
journaled beside the repository (`started`, `staged`, `bound`, `runtime_installed`, `repository_installed`,
`complete`) with phase and checksum evidence, stages and validates both sides, rebinds only the two location
ownership markers, installs the runtime first and the repository last, and finishes only after `check_pair`
confirms ownership, formats, event integrity and exact hashes. An interrupted restore is incomplete and unusable
and resumes deterministically for the same capture and targets; a different capture or target, or changed staged or
installed evidence, is refused instead of overwriting. Events and provenance keep their captured bytes and are never
relabelled for the new location.

A restore is also fenced and ownership-checked before it writes anything: the capture, both targets and every derived
journal, lock and staging location must be disjoint (`invalid_target`); a nonblocking lock per target, keyed by its
path and taken in a stable order, is held until the restored pair has been verified, so two restores that share the
repository or the runtime cannot both proceed (`restore_conflict`, retryable) and the loser leaves every byte alone;
and a staging location (`.<name>.restoring`) is removed only when a journal of the same capture and targets records it.
An existing staging entry with no such journal, including a symbolic link or file, is refused untouched. There is no
force or overwrite option.

Scope: the host owns the barrier (see its `RECOVERY.md`) and the decision to restore.

## Retrieval and future vector search

`Snapshot` supplies current nodes/edges, catalogs, object/source history,
neighbors, and source before/after bytes for a cited commit. Source/evidence/
history/search reads include the input mode. Object history also includes
before/after records, so removed objects can be inspected without recreation.
History preserves recording time separately from development dates. No backfill/replay API is
introduced.

Revision-pinned `handoff_notes(include_empty=False, offset=0, limit=20)` returns
note metadata without text; `handoff_note(event_id, offset=0, limit=16000)` pages
text. Both include source/operation/previous event, publication commit and
recording time. Collection limits are 1–100; note text limits are 1–64,000
characters. There are no consumer cursors, sends, read receipts or acknowledgements.

`action(action_ref)` returns the latest accepted revision, exact text (null after
withdrawal), current associations, transition and publication metadata.
`action(..., audit=True)` pages all immutable revisions. `action_timeline(kind,
id, audit=False)` pages current associations; audit includes every revision
from an object's first association onward, even after a correction excludes it.
Old snapshots return their then-current action revision. These projections are
rebuilt from immutable Git events, not a new authoritative index/database.

`Context.actions_history(*, tag_id=None, global_only=False, audit=False, offset=0,
limit=20, revision=None)` and the equivalent snapshot read page published Actions
in publication order independently of object associations. Current reads select
the latest revision of non-withdrawn Actions before applying filters; audit reads
include all revisions and withdrawals. Filters combine: tag ID equality and/or
no tag with non-null business_id. Each revision uses its own immutable tag
snapshot, never current mutable operational tags. Results retain canonical
action/source identity, exact revision_ref, text, transitions, previous-event
binding, commit and recording time in the existing collection-page shape.
CLI `actions-history` and the event tool `actions_history` expose this read.

Search projects sources and current nodes, edges and notes into `SearchRecord` values with
kind, stable ID, text, attributes (empty for a note), and a content digest. Requests/results pin a
published revision. The initial SQLite backend provides literal word/prefix
search and exact attribute-member filters; multiple filters/members must all
match. Numeric/boolean/string matches stay distinct. Search limits are explicit.

The search backend boundary supports `rebuild`, `apply` (changed/removed records
and an expected base revision), and `search`. A future vector backend can use
the same record projection and result identities. Its model/version, chunking,
and vector-store choices remain future work. Embeddings remain derived data;
they do not become graph fields or evidence citations. Missing/stale indexes
are rebuilt for the requested revision before use. Rebuild failure is reported
rather than returning data from a mismatched revision.

## Real propagation and local commands

`Pipeline.submit` is GENERAL; `submit_action` uses `Context.begin_action`;
`submit_edit` and `submit_supervised` execute a controlled deterministic graph or
supervised edit without any model (see "Controlled deterministic graph edits" and
"Supervised edits"), and `retry` dispatches to them from the pending
job's validated execution marker before any attempt is created. Each
passes `record_context` through (and `submit` also `expected_revision`) to its
`begin`. Both expose the stable event ID before invoking the model. `submit` validates
UTF-8/size before a new event, and retains its stable receipt across failures.
Edits/removals carry exact base before text and submitted after text. Published
receipts and durable completed ready drafts are recovered before either model
can run. `retry` is explicit; blocked/error/incomplete attempts retain their
draft without publishing. `recover` alone never starts a model.

Bootstrap loosely matches both source versions with current node summaries and
string attributes, asks the configured model to select candidate IDs, and
uniformly samples up to 40 additional neighbors and 60 edges inside X+Y.
No scoring/ranking is used. All candidate IDs must fit the request limit; all
returned IDs must belong to the candidates. Samples include support, counts,
omission notices and the invitation to explore, and persist for a matching retry.

`EventTools` binds one Context/event/base; model arguments never select paths,
another event/context repository or arbitrary revisions. Search and history use the
published base. Object/draft/list/neighbor/catalog reads overlay staged changes.
Deleting a node does not hide still-present incident edges. Catalogs contain
all published names/values plus draft vocabulary, with no numeric enumeration.
Source reads use managed source IDs; evidence reads accept only full source-event
commits in the base history. Ordinary collection pages default to 20 (max 100);
text pages default to 16,000 characters (max 64,000). Results state their view,
revision and pagination. Search uses one extra hit for has_more, without claiming
a total or adding pagination to core search.

Mutation tools allocate creation IDs, replace complete records, and require
the existing explanation/citation/support/development contracts. Unknown tools
or arguments fail. Foreign source-event references fail before staging. Final
cross-object validation remains in Context. `finish_propagation` with complete
requires exact coverage of source-supported objects needing reassessment,
validates the draft, returns its digest, and seals mutation tools for that
attempt. Action edit/remove coverage unions current source support with every
past action association, independently of bootstrap sampling. Historical targets
are readable through object/history/action-timeline tools. Both modes expose
`stage_handoff_note`; only ACTION exposes `stage_action_associations`. GENERAL
graph-write schemas omit developments. The common propagator
prompt plus exactly one mode prompt is combined and its digest is recorded.
A blocked finish requires a nonempty handoff note and never permits publication.
The note and partial draft remain durable and inspectable, not delivered.
Coverage is not a semantic verifier; the model is responsible for interpreting remaining evidence.

`Context.inspect_draft(event_id, validate=True)` runs publication validation
without advancing Git or touching indexes. Its digest binds event/input/base
and every staged record/support/history change, mode output, immutable tag snapshots,
input identity and previous source event, excluding phase/error bookkeeping.
`publish(..., expected_draft_digest=...)` checks that digest under the writer
lock. The pipeline writes a ready record only after both a complete finish and
a completed model turn; explicit retry can publish that unchanged draft without
another turn. Any ready-record/draft mismatch or changed base is a conflict.
Low-level controlled callers can still publish without the optional digest.

Each pipeline attempt holds a separate propagation lock in Git's common
directory, then uses Context's existing per-operation locks. No Context lock is
held across a model call. Private run records, bootstrap samples, completion
gates, errors, receipts and bounded diagnostics live in the separate runtime's
`runs/<event>/<attempt>/`, outside transient event input directories.

The app-server runner uses a fresh ephemeral thread, normal service tier,
explicit model/effort (defaults Astra/max), bounded input/output/time/call limits,
disabled unrelated capabilities and inherited MCP entries, and application-owned
dynamic tools. It validates tool/thread/turn identity, replays identical call IDs,
denies unsupported interactions and reaps its own child process group on every
exit. It never resumes an arbitrary Codex thread or changes global configuration.
The command interface exposes init, submit, retry, pending, recover, discard,
search, show-object, history, source inspection, submit-action, handoff-notes,
handoff-note, action, action-timeline, notes, preview-supervised, submit-supervised,
question-output and ack-question-output with JSON results/errors (note kinds are accepted by
search, show-object and history). There are no operational delivery commands.

## Model-call usage receipts

Each actual runner call of a pipeline attempt has one private, bounded receipt at
`<runtime>/runs/<event>/<attempt>/calls/<stage>.json` (`stage` is `bootstrap` or
`propagation`; a stage runs at most once per attempt). The call ID is stable:
`call_id = <attempt_id>:<stage>`. The pipeline writes the `started` receipt durably
**before** it launches the runner and rewrites it whenever an attributable usage snapshot
is observed, so a timeout, failed turn or killed process cannot erase observed usage. It
settles the receipt to `completed` (the turn completed) or `failed` (the runner raised, or the
turn did not complete). An in-process interruption (`KeyboardInterrupt`, `SystemExit`) or a
killed process leaves the receipt `started`: unknown and unfinished, never free or completed.
Nothing reconciles it; an explicit retry is a new attempt and therefore a new call.

A bootstrap with no matching candidates makes no call and has no receipt. A retry that reuses
an earlier bootstrap sample creates no bootstrap call or receipt; its propagation receipt's
`bootstrap_call_id` names the original charged call (also stored as `bootstrap_call_id` in
`attempt.json`, `null` when no call produced the sample). Publication, recovery and receipt
replay never call a runner and create no receipt. Receipts hold identities, times, counters and a
bounded error summary only: no prompts, source text, tool arguments or results. They live in the
runtime, not Git, and are private like the other run records.

### Runner protocol

`Runner.run(*, stage, instructions, payload, directory, tools=None, output_schema=None,
on_identity=None, on_usage=None) -> RunResult` (`qorqut_context.runner.Runner`). The pipeline
always passes `on_usage`; a runner with no usage evidence never calls it. `on_usage(Usage)`
receives each **cumulative** snapshot as soon as it is observed; a later snapshot replaces, never
adds to, an earlier one. `RunResult.usage` (default `None`) is the runner's last snapshot and is
observed too unless it equals the last one delivered. `Usage` fields: `input_tokens`,
`cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens` (each a
nonnegative integer or `None`), `cost_usd` (finite nonnegative number or `None`), `thread_id`,
`turn_id` (the native identity the counters claim) and `source` (a provenance label, default
`runner`). `None` is unknown, never zero. The pipeline accepts a snapshot only if its fields are
well formed and it is attributable. A snapshot that claims native IDs is accepted only when it names
**both** and they equal the thread/turn pair the runner has already announced through `on_identity`
(`usage_scope` `native`); an early, half-specified or foreign claim is ignored and counted in
`ignored_updates`, and is never kept to be relabelled by a later identity. A snapshot that carries
neither ID is a trusted runner's call-scoped evidence (`usage_scope` `call`). A snapshot carrying no
value at all is ignored silently. The pipeline copies only `thread_id`, `turn_id`, `model_provider`,
`observed_model` and `usage_updates_ignored` from the identity dict. A call's non-null native
thread and turn IDs are immutable once announced (a different later announcement is ignored);
provider and model evidence may change, e.g. a reroute changes the observed model, and a later
identity never changes the scope of, or relabels, an earlier snapshot.

`AppServerRunner` reports `thread/tokenUsage/updated` notifications (Codex app-server 0.162.1
protocol; params `{threadId, turnId, tokenUsage: {total, last, modelContextWindow}}`). It uses the
thread's cumulative `total` (`inputTokens`, `cachedInputTokens`, `outputTokens`,
`reasoningOutputTokens`, `totalTokens`); each call runs one turn on a fresh ephemeral thread, so
that total is this call's usage. Updates for another thread or turn, and updates whose counters are
missing, non-integer, boolean or negative, are ignored, counted in `identity.json`
(`usage_updates_ignored`) and logged privately by reason; an update that arrives before the
`turn/start` response is held until the turn is known. `model/rerouted` for this turn replaces
`observed_model` in the identity (the configured model is unchanged). The observed provider is the
`modelProvider` of the `thread/start` response and the observed model is that response's `model`;
neither is inferred from the other or from the configured name. The turn events carry no dollar
amount, so `cost_usd` is `null` for this runner and no price is guessed.

### `Pipeline.usage_receipts(event_id, *, limit=100, cursor=None)`

Read-only: no lock, no initialization, no file creation, no process, no retry. Returns primitive
JSON `{"status", "event_id", "items", "next_cursor", "error"}`. `limit` is an integer from 1 to 500.
Items cover exactly that event, ordered by the immutable attempt `sequence` and then stage
(`bootstrap` before `propagation`), including failed and unfinished calls. `next_cursor` is an
opaque string when a further page exists, else `null`; it binds the event, the limit and the last
returned ordering key, so it is refused for another event or page size. An invalid event ID, limit
or cursor raises `ContextError`. `status` is `ready` (`error` null) or `unavailable` (`items` empty,
`next_cursor` null, `error` is `{"code", "message"}` with a bounded message): `corrupt_evidence`
(an attempt record or receipt is not a regular file, is oversized, is not strict JSON, names a
different event/attempt/stage/sequence, or has an invalid field, state or completeness flag) or
`unreadable_evidence` (an inspection, listing or read failure; only a genuinely missing path counts
as absent). Every evidence directory is checked, not only the leaf file: a symbolic link or a
non-directory where the run, event, attempt or `calls` directory belongs is `corrupt_evidence` and
is never followed. An absent run or event directory is `ready` with no items. **An empty ready page
means only that no call receipt is recorded** — never proof of zero calls or zero cost (for example
for an attempt made before receipts existed, or an event the pipeline never ran); the host must
cross-check its known input and attempts. Each item:

| Field | Meaning |
| --- | --- |
| `call_id`, `event_id`, `attempt_id`, `attempt_sequence`, `stage` | Stable identities; sequence is the 1-based attempt number |
| `state` | `started`, `completed` or `failed` |
| `started_at`, `finished_at` | UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`; `finished_at` is `null` while `started` |
| `configured` | `{model, effort, service_tier}` the pipeline configured |
| `observed` | `{provider, model, thread_id, turn_id}`; each `null` unless the runner announced it |
| `usage` | `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens` (integer or `null`) and `cost_usd` (number or `null`): the latest accepted snapshot |
| `evidence.usage_source` | Provenance of the snapshot (`thread/tokenUsage/updated` for `AppServerRunner`), else `null` |
| `evidence.usage_scope` | `null` without an accepted snapshot; `native` when it claimed and matched the call's verified thread/turn pair; `call` when a trusted injected runner supplied it carrying neither ID. Fixed when the snapshot is accepted |
| `evidence.usage_updates`, `evidence.ignored_updates` | Accepted snapshots (each replaces the last) and rejected or runner-discarded updates |
| `evidence.usage_observed` | At least one counter was observed |
| `evidence.counters_complete` | All five counters are present (valid observed values); says nothing about finality or call state |
| `evidence.usage_complete` | Verified **final** usage. No source can prove final totals (a finished call, a `completed` status or a full snapshot is not proof), so this is always `false` |
| `evidence.cost_observed` | A cost was supplied |
| `evidence.limitations` | Any of `unfinished_call`, `usage_unreported`, `partial_counters`, `final_totals_unproven`, `cost_unreported`, `ignored_updates` |
| `bootstrap_call_id` | Propagation only: the call that produced this attempt's bootstrap sample (its own, or the reused original), else `null` |
| `error` | `null`, or `{type, message}` of a failed call (message at most 300 characters) |

Limitations to rely on: the protocol has no event that proves a turn's totals are final, so the
last accepted cumulative snapshot is the best native evidence and `final_totals_unproven` accompanies
any observed counters (`AppServerRunner` stops reading at `turn/completed`, so a later update would not be seen). `cacheWriteInputTokens` is not collected. Counters for a call that ended
without a snapshot stay `null`. The pipeline persists its own call intent and settlement even for an
injected runner that supplies no usage; the host owns any price model, ledger and aggregation.

## Remaining boundary

Standalone MCP serving, web UI, package distribution, broader Qorqut
integration and vector search remain deferred. The external question collection
(storage, editing, deletion and delivery bookkeeping) and any approval or review interface for
supervised edits belong to the host, not this engine. Mechanical checks use controlled
outputs; real-source acceptance separately observes model behavior. Neither
demonstrates guaranteed factual correctness. Real inputs and model traces
stay outside this source repository.
