# Context input modes and shared action histories

This document specifies the two trusted-host input modes the engine implements over its source lifecycle. The shared
context is one graph; accepted actions carry optional revision tags and shared history needs no graph subject (see
[Host integration](HOST_INTEGRATION.md)). Operational queue and transport integration belong to the host.

> **Superseded in part by format 3.** This engine no longer consolidates questions and
> low-confidence items in the handoff note: the note is operational only, uncertain details
> are kept as Context notes, and owner-resolvable questions are add-only question output
> held outside the Context. See [CORE_CONTRACTS.md](CORE_CONTRACTS.md), "Notes" and
> "Question output". The remainder of this document is unchanged, except that format-2 histories
> are now read as they are and upgraded by their first format-3 publication ("Format 2 compatibility").

## Scope and exclusions

The engine provides two trusted-host input entry points over the existing source lifecycle:

- GENERAL: extract ordinary states; publish one handoff note for operational
  claims, work requests, all questions and low-confidence items, without acting
  on unaccepted claims.
- OPERATIONAL_ACTION: interpret an accepted action revision; update the graph
  and associate that single action with all relevant object timelines. Publish
  the single note for questions and low-confidence details without routine
  reapproval of the accepted record.

Keep callable APIs, local commands, graph retrieval, immutable history, explicit
retry, and the existing Astra/max runner working together. The local caller can
read committed notes and submit accepted action text without a running agent
platform. This is sufficient to test the boundary before reconciliation.

The engine does not include tasks, sign-offs, an operational SQLite database, permission or
agent registries, inbox/delivery/acknowledgement systems, background outboxes,
MCP hosting, automatic task/action creation, or a main agent. No broader package
integration, UI, deployments, backfilling, semantic
verifier, vector implementation, or live data migration. Tasks and actions are
independent; a task transition is not automatically an action input.

## Preserve existing contracts

Retain five-character context IDs; independent directed edge identities; flat
list attributes and expandable catalogs; nonempty basic_entity_type; full
commit-only evidence; support-only changes; explicit incident-edge handling;
UTC/earliest-component date normalization and unknown 0; random bootstrap
limits; separate source/engine repositories; and sequential atomic publication.
Keep source dates separate from commit recording time.

Do not change the runner's capability restrictions, model, effort, service-tier
handling, global-instruction exception, limits, or retry policy. Bootstrap and
propagation stay gpt-6-astra/max at normal speed. No provider fallback, additional
classification model, keyword classifier, or direct graph-file editing.

Source events use format 2 only, with explicit input_mode and mode-specific
output fields, and repositories carry the format 2 config. Any other repository
configuration, event format, missing or unsupported input mode, or pending job
format is rejected with a clear error without editing, discarding, upgrading,
reading as history, or replaying it. Acceptance uses fresh format 2 data.

## General source input

Keep Pipeline.submit and Context.begin as GENERAL entry points. Preserve raw
Markdown/text bytes exactly, including frontmatter and line endings. A source's
input mode is fixed. Neither frontmatter, metadata.kind, nor caller content
can turn a general source into an accepted action. General entry points cannot
edit/remove a managed action source or write into its reserved source directory.

Apply the source's policy whenever evidence is retrieved, not just when it is
first ingested. Label source/evidence/history results with the recorded input
mode. In any model turn, a raw action claim is still raw, even if quoted by
another source or found by search. Current graph summaries are discovery aids;
read evidence when its eligibility matters.

Ordinary state, such as "Jesse works at Northstar Studio", may support the graph.
"We hired Jesse yesterday", "I registered the LLC", and "build a landing page"
go to the handoff note. Do not infer their effects, record action developments,
create task/deliverable entities, or launder them into summary/attribute state.
An independent state statement in the same file can still be filed. Ambiguous
material goes into the note rather than blocking an otherwise valid raw event.
Structural checks cannot prove this semantic distinction; prompts and real
acceptance must demonstrate it, without claiming a semantic verifier exists.

### One note, atomically published

Expose an event-bound stage_handoff_note(text) tool in both modes. A nonempty text
stages/replaces the single consolidated note. An empty string explicitly records
that nothing needs handing off: no questions or low-confidence items, and no
operational material for GENERAL input. Repeated calls replace the draft output;
they cannot create several note records or initiate delivery. Enforce the source
byte limit on note text; do not silently truncate. The response can be a small
receipt rather than echoing the entire note.

Persist the disposition and text in the immutable event, not in foundation.
The note's identity is its context event ID, qualified by the selected context;
do not allocate a new global ID. Application-supplied metadata identifies the
source, operation, previous source event when present, and publication commit.
For edit/remove, the note explains changed/withdrawn operational claims; it does
not change accepted operational actions. A source event with a note and no graph
change is valid. Both modes require an explicit note disposition before
publication; controlled low-level callers must supply it too. This includes
uncertain identities, relationships, attributes, dates, and interpretation.
If safe completion is impossible, the propagator stages a nonempty note with
the blocking questions before finishing blocked. The note remains in the
durable unpublished draft for inspection; this does not publish partial state.
Older published action events without notes remain readable unchanged. Older
unpublished action drafts need an explicit note before new publication.

Expose revision-pinned list/read operations for committed notes, with pagination
and event/source/commit metadata. Include empty dispositions when explicitly
requested. Default listing may return only prepared nonempty notes. Keep saved
consumer cursors, delivery status, read receipts, acknowledgements, and actual
sends out of this engine. Report "prepared", never "delivered" or "verified".

## Accepted action input: a small context-side adapter

Provide Context.begin_action and Pipeline.submit_action with add/edit/remove.
These are controlled-caller APIs, not authentication services. The future owning
adapter authorizes the recorder and supplies canonical accepted text; the
propagator never calls these APIs. No public operational HTTP route is selected.

The local boundary accepts an opaque action_ref (nonempty, at most 256 UTF-8
bytes), optional opaque revision_ref with the same bound, exact UTF-8 action
text, optional related-object pointers, submitted_by, and the existing optional
event_id. Text includes the accepted action's actor/date/qualifications when
known; do not invent an operational database schema here. A pointer has kind,
five-character id, and optional explanatory text. Reject malformed/unknown-kind
pointers; known syntax but absent IDs are reported as unresolved suggestions,
not authority to invent an object. No pointer selects a repository or business.

The adapter serializes a deterministic UTF-8 .txt evidence document containing
action_ref, optional revision_ref, text, pointers, and immutable revision tags.
The original action text round-trips exactly through this envelope; raw GENERAL files stay byte-for-byte
unchanged. These serialized bytes become the one source file for this event.
Pointer/revision/tag corrections therefore change source bytes rather than making
an untracked metadata-only edit. Keep the existing size limits over text and
the serialized source. Secrets/provenance capture belong to the future producer;
do not read credentials, sessions, or unrelated files to fill fields.

Allocate the normal context source ID and keep evidence under the reserved
foundation/actions/<source-id>.txt directory. This is context evidence, not
the operational system's live storage. Derive the action_ref-to-source mapping
from published events, including withdrawals; never use an external ref as a
path or replace the context allocator. One action_ref has one source identity
for its lifetime. Edit/removal keeps it. A second add for that reference is an
explicit conflict, including after withdrawal; restoring withdrawn actions is
deferred. An exact replay of a known event ID must still return its publication
before checking a now-removed source or invoking a model.

Use the existing event_id as the context replay identity; expose it before a
model call and preserve it in failures. Same event/different input is a conflict.
Do not impose a sequence format on revision_ref or interpret it as an ordering
number. The future adapter owns upstream revision ordering. Locally, an optional
expected previous action-ingestion commit provides an optimistic revision check
for edit/remove; exact replay is resolved before that stale-base check.

The v2.2 shared-context contract supplies optional immutable operational-revision
tags, a list of {id, name, business_id} snapshots; business_id may be null and
omitted tags mean []. There is no mandatory graph subject, business fallback or
tag-to-node creation. Tags belong in canonical source evidence and submission
seals, so a tag-only correction is a distinct, exactly retriable revision.

## Shared action associations and correction

Store the action revision once in its source event. Add an event-bound
stage_action_associations(date, summary, associations) tool only in action mode.
It replaces the full association draft for this action. The host supplies the
fixed action/source/event identity and input operation; the model cannot name
or modify another action. Summary is at most 150 words. Each association contains
kind, id, and a concise explanation (at most 150 words). Links cite self
automatically; do not turn external references into graph citations. Normalize
date with the existing rules. Store summary/date once, not one action per object.

ADD/EDIT permits an explicit empty association list when no suitable subject
exists. Use every relevant identifiable entity/edge; a history-only action does
not require a dummy put or an extra support citation on current state. Pointers
are suggestions, and the final set is stored separately. Association targets must be known objects of the stated kind
in published history or created in this draft; unknown IDs are not valid.
Historical objects may receive associations without being recreated.

REMOVE stages an empty current association set, preserving the previous date
and an explanation of withdrawal in audit history. Do not infer an opposite
real-world action or manufacture current support. Earlier immutable events stay
unchanged. A later real-world firing is a new action, not an edit of the hiring.

On action EDIT/REMOVE, required_reassessment is the union of current objects
supported by that source and every object previously associated with the action.
This is independent of bootstrap sampling and includes removed/history-only
objects. Tools can inspect their old graph/action history without requiring a
current record. finish_propagation still requires exact coverage of that set.
The backend records association transitions from the old to the new set; past
associations are not lost when support changes or the current object disappears.

Expose revision-pinned action lookup, per-object action timelines, and published
actions_history independently of graph subjects. Tag/global filters use immutable
revision snapshots; global means no linked business tag. Current history lists
latest non-withdrawn Actions; audit includes every revision and withdrawal.
A current action lookup shows the latest accepted action revision and its current associations;
an audit view includes additions, corrections and withdrawals with publication
commits and recording times. An old snapshot shows that snapshot's then-current
revision. Keep ordinary graph-maintenance history separately available through
existing history reads. No growing history array on current node/edge records
and no action nodes. Indexes remain rebuildable, never authoritative.

## Publication, tools and prompts

- Mode, input/action identity, serialized source digest, immutable tag snapshots,
  handoff disposition/text, and action associations all participate in the
  submission/draft seals as appropriate. They cannot change after successful
  completion without invalidating the ready marker.
- A GENERAL event rejects an action-association draft. Both modes require a
  handoff-note disposition. All necessary output is validated before publication.
- GENERAL model graph-write schemas omit dated developments, and core validation
  rejects nonempty GENERAL developments; ordinary state history still exists.
  Action runs can record meaningful dated developments.
  The shared action reference/timeline remains canonical, so developments are
  not a second independent action record.
- Mode-specific tools are absent from the wrong registry and rejected at core
  publication too. All reads retain existing bounded/pinned behavior. No new
  execution, filesystem, network, messaging, delegation, or source-write tools.
- Prompt composition uses the common propagator instructions
  plus exactly one of general_input.md and operational_action.md. Record the
  combined digest. Bootstrap remains entity discovery, not a trust classifier.
- A successful finish and completed model turn are both still required.
  An already published event recovers without another model invocation. A
  ready draft includes the exact mode outputs. Failed drafts retain them for
  explicit retry; no automatic retries or outbox implementation.
- A low-level caller can stage controlled changes, but cannot publish the new
  event shape with missing/inconsistent mode outputs. Meaning is assessed by
  the propagator/caller, not by string matching in the validator.

Extend the local CLI with separate action submission, committed-note inspection,
action lookup, and action timeline inspection. Reuse submit/retry/pending/recover
and existing local error envelopes. Do not add delivery commands or a service.
