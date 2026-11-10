# Signoff-backed requests for owner input

Status: implemented in `codex/signoff-input-implementation`; deployment and live-data reconciliation are separate.

## Implemented behavior

Collect explicit owner questions, clarifications, missing information and access requests through an owner-only Signoff.
`signoffs_input_create` accepts an optional `source: {type, id, business_id}` and creates the question and dependency
atomically. The source may be a Task, Table, Project or standalone Focus Reference. Both endpoints keep their own
scope: `business_id` is an explicit project UUID or `null` for Global. Omit `source` for a standalone request.
Source-linked forms default to `owner_only` and reject `management_allowed`; internal delegated questions keep their
existing behavior. An input answer never authorizes a separate action.

Use `signoffs_source_link(source, signoff_id)` to attach an existing pending approval or owner-only question.
Creation and linking are idempotent. Multiple sources can wait on the same Signoff and each source can await multiple
Signoffs (at most 100 pending dependencies per source). Use `signoffs_source_unlink(source, signoff_id, link_id, expected_link_revision)`
to remove an obsolete dependency using its returned `link_id` and `link_revision`. The Signoff and response history remain intact.
Mutations check Signoff visibility and proposer/owner authority, plus the source's read and discussion/link-write
authority. Linking an approval does not require permission to execute its prepared change.

The new `signoff_source_links` table stores dependencies separately from Tasks and immutable Signoff proposals.
Neither linking nor answering changes Task revisions or work status. Task reads return `work_status`, `input_needed`,
`input_requests` and `input_version`; `status` projects `needs_input` while any linked Signoff is pending and not
superseded. Answering, rejecting, cancelling or superseding the last pending request restores the work-status display.
Agents must reassess what the response means for the work. Task list filters, Todo summaries, compact MCP reads and
thread context use this projection. The independent Signoff clock prevents stale client reads from undoing a response.

Pending Signoffs have their own visible section above the chat history, using the existing response cards. This section
does not depend on message pagination, progress-note expansion or Focus membership. Each Signoff's own read policy still
applies; an inaccessible request shows an unavailable dependency without exposing its title or review scope. Contextual
`concerns` links never set the indicator. The worker-assignment badge now says “Owner assigned.”

New manual `status: needs_input` writes are rejected with instructions to create/link a Signoff. Legacy saved values are
retained unchanged: without a pending dependency they display as Open, with `input_request_missing: true` and a thread
notice for explicit reconciliation. No question is guessed or retroactively invented. Source links follow audited Task
moves with their own revision. Existing Signoff events notify the proposer and eligible linked Task workers/coordinator;
private answer text is not copied into shared task notes.

Existing installations require the explicit offline schema upgrade under the normal exclusive instance lock:

```sh
python -m qorqut.migration.signoff_sources inspect --instance-root INSTANCE_ROOT
python -m qorqut.migration.signoff_sources apply --instance-root INSTANCE_ROOT
```

Pass the installation's component selections and `--extensions-config` where applicable. The command accepts only the
exact predecessor or target composition, adds the binding table/index/triggers atomically, and is repeatable. Normal
startup does not migrate or reconcile data. This implementation does not deploy or modify a running installation.

A release that carries later schema transforms as well (the application queue indexes, for one) composes a predecessor
this command no longer matches on a store that lacks both. Use `python -m qorqut.migration.upgrade inspect|apply` with
the same arguments there: it applies this transform and every later one in release order, in one transaction, and its
`revert --to base` walks them back.

Coverage includes atomic creation, retries, standalone/multiple questions, cancellation, exact unlinking, cross-scope
identity, visibility/authorization failures, task-status filters, preserved approval bindings, schema recognition and
independent frontend version handling. The remainder of this document records the investigation that preceded the work.

Recorded November 10, 2026, from the owner's discussion. Investigation baseline:
repository commit `1f1ae90`, on the dedicated `codex/signoff-input-design` branch.
Source links and the changed surfaces were refreshed against remote `main`
commit `a1845b1` before publication. The original probes are identified below.

## Product direction

Every explicit request to collect owner input should use a Signoff. Reuse the
existing input-request category for questions and existing action Signoffs for
approvals. A request can stand alone or be associated with a task or its thread.

A task displaying **Needs your input** must identify the pending Signoff or
Signoffs responsible for that state. Opening its thread must show those same
requests with the existing response controls. A general reference to a Signoff
is not sufficient: distinguish contextual links from an explicit dependency on
the owner's response.

One request may affect several tasks, and one task may await several requests.
Answering or resolving the last outstanding request removes that reason for
needing input; it does not prove that work has started or finished. The agent
must reassess the task after the response.

Keep question answers distinct from action authorization. Answering an input
Signoff never approves an unrelated action, and an action already awaiting
approval must not gain a duplicate question asking for the same approval.

Existing tasks need a deliberate reconciliation: link an existing request,
create a missing question, or correct an obsolete status. Preserve response and
conversation history. This document does not authorize that reconciliation or
a release.

## Findings that change the initial sketch

The initial conversation suggested adding a generic relationship role and
letting task status follow it. Investigation shows that this is not a complete
implementation plan:

- Generic relationships have one scope for both endpoints. Their database
  CHECK also rejects a new Global task-to-Signoff role. Reusing this table is
  not automatically a migration-free option.
- Storing the binding in task metadata advances the Task revision. This can
  invalidate an already prepared action Signoff that expects that Task's old
  revision. Attaching a request must not itself invalidate its approval.
- A Signoff response advances the Signoff revision, not the Task revision. The
  task UI intentionally ignores a changed `status` at an equal Task revision.
  Deriving a different `status` only in `tasks_get` would leave stale clients.
- An input form currently defaults to `response_policy: management_allowed`.
  A request that specifically requires the owner's input must use `owner_only`;
  delegated internal questions must not automatically become owner blockers.
- Submitted Signoff decisions already generate proposer inbox events. An older
  documentation statement that agents are not alerted is stale. Extend the
  existing delivery path rather than inventing a polling worker.

These findings informed the implementation described above.

## Existing mechanisms to reuse

All paths below refer to this checkout; function names identify the relevant
code without depending on changing line numbers.

| Area | Existing code and behavior | Implication |
| --- | --- | --- |
| Questions and answers | [`signoffs/inputs.py`](../qorqut-operations/qorqut_operations/signoffs/inputs.py): `InputRequests.descriptors`, `prepare`, `apply`, `validate_form`, `validate_response` | Reuse `signoffs_input_create`, `signoffs_input_cancel`, text/choice forms, validation and request keys. |
| Decision lifecycle | [`signoffs/service.py`](../qorqut-operations/qorqut_operations/signoffs/service.py): `respond`, `_decision`, `_update`, `stage` | Reuse exact proposal bindings, immutable decisions, status revisions, retries and replacement history. Input answers never execute actions. |
| Generic associations | [`relationships/operations.py`](../qorqut-operations/qorqut_operations/relationships/operations.py), [`relationships/edges.py`](../qorqut-operations/qorqut_operations/relationships/edges.py), [`storage/core_schema.py`](../qorqut-operations/qorqut_operations/storage/core_schema.py) | `concerns` is contextual. Workflow relationships need a domain-owned mutation path; generic role creation permits duplicates and is not an input lifecycle. |
| Existing scoped metadata | [`tasks/links.py`](../qorqut-operations/qorqut_operations/tasks/links.py), [`tasks/types.py`](../qorqut-operations/qorqut_operations/tasks/types.py) | `$task_links` demonstrates explicit cross-scope references and per-endpoint authorization. It is Task-to-Task metadata, not a ready-made Signoff dependency store. |
| Task read/write | [`tasks/operations.py`](../qorqut-operations/qorqut_operations/tasks/operations.py): `detail`, `read`, `prepare`; [`tasks/schema.py`](../qorqut-operations/qorqut_operations/tasks/schema.py) | `needs_input` is presently a freely writable state. Reads and SQL filters use that saved value. |
| Thread contents | [`inbox/thread_views.py`](../app/qorqut/inbox/thread_views.py): `task_context`, `related`; [`inbox/private_conversations.py`](../app/qorqut/inbox/private_conversations.py) | Reuse typed source identities and the one private source conversation; no new thread or message store is needed. |
| Embedded Signoffs | [`conversation.js`](../biz-dashboard/js/conversation.js): `conversationRelatedRecords`, `conversationRecordEntry`, `conversationTimeline`; [`signoffs.js`](../biz-dashboard/js/signoffs.js): `sgChatMount` | The same actionable Signoff card is already used in native Chat and record threads. |
| Response delivery | [`inbox/events.py`](../qorqut-operations/qorqut_operations/inbox/events.py): `EventFinalizer.targets`; [`host inbox/events.py`](../app/qorqut/inbox/events.py): `HostEventRouting` | `decision_json` changes emit `response_submitted` to the proposer. Host routing supplies additional recipients in some cases. |
| Scope and privacy | [`signoffs/service.py`](../qorqut-operations/qorqut_operations/signoffs/service.py): `_context`, `_visibility`, `_row`; [`records/resources.py`](../qorqut-operations/qorqut_operations/records/resources.py) | A readable Task is not permission to read its linked Signoff. Preserve Signoff policy, private-operation narrowing and exact review scope. |
| Focus | [`host signoffs/ports.py`](../app/qorqut/signoffs/ports.py): `HostFocus`; [`signoffs/inputs.py`](../qorqut-operations/qorqut_operations/signoffs/inputs.py) | Input creation already supports atomic optional Focus presentation. Focus archiving is presentation, not an answer or dependency link. |

### Input request details

An outstanding request is deduplicated by proposer, review scope and
`request_key`. Identical pending forms reuse the request; changing a pending
form under the same key fails. This is not cross-agent deduplication and does
not deduplicate linking a request to several tasks. Those links need their own
idempotence and uniqueness.

`form.kind` now supports `question`, `request`, and `draft_review`; response
modes are `none`, `single`, and `multiple`. Draft reviews add editable text
fields and owner-authored revisions through `signoffs/drafts.py`. Saving a draft
is not submitting an answer: the pending request stays open until returned.
The form supports optional or required notes, but
does not define file-upload or desktop-control fields. A document/login request
can explain the existing handoff destination and ask for confirmation; adding
new collection widgets is separate work.

`validate_form` defaults ordinary questions/requests to
`response_policy: management_allowed`; `draft_review` explicitly forces
`owner_only`.
[`management/service.py`](../app/qorqut/management/service.py),
`auto_route_input`, can route eligible business-agent questions to the
coordinator. This feature must remain distinct from owner-only questions. Set
owner-only policy for the new owner-input path rather than silently changing
all existing management questions.

Cancellation retains the request and history. A generic replacement requires
withdrawing the old request and creating a new one; `supersedes_id` is part of
the wider Signoff lifecycle, but generic input creation does not offer a
replacement argument for ordinary questions. Draft reviews have a separate
`previous_review_id` for an already answered review by the same proposer in the
same scope; that is not pending-question supersession. Do not assume task
dependencies automatically follow the new request.

### Current thread discovery is insufficient

`owner_conversation_related` reads `concerns` relationships, children, action
references, prepared Signoff targets and Focus references. Prepared target
discovery requires matching review/source scopes. Input creation stores
`expected_targets_json: []`, so a question is not discovered as a Task
dependency just because the text mentions that Task.

`expected_targets_json` is execution/version evidence; do not insert Task
references into it just to make a request appear in a thread. The same applies
to immutable proposal content and private message history.

The current thread renderer sorts related cards into the historical timeline
and can exclude cards older than the earliest loaded private message. The
pending requests need a separate visible section, independent of old-message
pagination, collapsed progress, or Focus membership. It can mount `sgChatMount`
directly and deduplicate the same card in the history view.

The original `1f1ae90` baseline merged progress notes into the ordinary
timeline and showed a task overview. Remote `main` at `a1845b1` groups notes
into collapsed activity through `conversationActivityEntry` instead. The
proposed pending-Signoff section must remain visible independently of either
progress-note presentation; the earlier flattening is not present on current
`main` and must not be assumed as a prerequisite.

### A second indicator has a different meaning

[`tasks/workers.py`](../qorqut-operations/qorqut_operations/tasks/workers.py)
sets `user_needed` when the installation owner is in `worker_ids`. It does not
mean that a question exists. Preserve the assignment meaning internally and
avoid using its current "User needed" badge as proof of a pending Signoff.
Review the wording alongside the new owner-input indicator.

## Recommended implementation shape

This is an engineering recommendation for the next implementation, not a
finalized API contract. Prefer a small explicit binding domain plus one shared
input projection. The existing Signoff remains the only owner-response record.

### 1. Store explicit source bindings separately

Use a small association table with its own revision/lifecycle, connecting a
Signoff ID to the existing typed source identity (`type`, `id`, `business_id`).
Store contextual association separately from a dependency on the response,
either as a constrained relation kind in this table or as existing `concerns`
links where those already work. Only the explicit dependency drives the
input-needed indicator.

For task threads, bind to the Task identity, not the private conversation ID:
the conversation may not exist until its first message. A standalone Signoff
has no source binding and continues to use its own existing conversation.
Use the same source reference for a non-task record thread; such a record need
not acquire a Task status just to show a pending question.

The schema must enforce unique source/Signoff/role bindings, including NULL
Global scope. Signoff IDs are globally unique; Task IDs are only unique within
their owning scope. Index both source lookup and reverse Signoff lookup.
Authorization must check each endpoint; a link must never expand visibility.

Why prefer separate storage after investigation:

| Option | Advantage | Cost or limitation |
| --- | --- | --- |
| New generic relationship role | Reuses the edge machinery and indexes | Needs a Global CHECK migration, retains same-scope endpoints, needs a workflow guard and dependency projection, and currently blocks task relocation across the edge. |
| Reserved field in `tasks.fields_json` | No new table; resembles `$task_links` | Changes Task revisions and can stale prepared approvals; needs reserved-key preservation, reverse scans and independent attention versioning. |
| Small source-to-Signoff binding table | Own revision, explicit scopes, indexed reverse lookup, no Task/proposal mutation just to link | Needs a storage contribution/migration and authorized link/unlink operations. Recommended for the complete requirement. |

Avoid broadening the entire generic graph or putting cross-scope identity in
free-text relationship context to save one table.

### 2. Make request creation and linking coherent

Extend input creation with an optional typed source binding, validated and
written atomically with the request. Omit it for a standalone question. Reuse
the existing input operation and optional Focus integration rather than create
a parallel question API.

Provide a small domain-owned attach/detach operation for existing Signoffs,
especially already prepared action approvals. Its metadata revision is
independent of the Task and Signoff proposal revisions. A retry must not create
duplicate links. Validation must recheck the source, actual review scope,
current Signoff, permitted responder and caller's link authority at apply time.
Knowing a request ID or having source read access alone is insufficient.

Linking an existing action approval must leave `payload_hash`, prepared
targets, proposal content and Task revision unchanged. A pending Signoff that
expects Task revision N must still be actionable after the link is attached.
Do not bypass task/source policy by hiding the link write inside an otherwise
permitted question-creation operation. Specify the operation grant and source
mutation-policy checks as part of the API contract.

### 3. Derive owner input separately from authored work status

Use one backend projection for current pending dependencies, referenced by
task lists, details, thread context and agent summaries. Resolve compact
authorized references first; let the existing Signoff reader load forms.
Evaluate inputs in batches for list pages; avoid one full `signoffs_get` per
Task or per row.

Recommended distinction:

- Authored work status describes the work (`open`, `working`, `blocked`,
  `waiting`, `done`, `cancelled`).
- The derived owner-input indicator says this source has an outstanding
  owner response, and names the exact Signoffs.

The UI can continue to display **Needs your input** prominently, but should
not persist a second unsynchronized answer state. Clearing it reveals the
existing work status; it does not start work, finish a task or revive a closed
task. The agent receives the response and reassesses the work.

Treat existing stored `needs_input` rows as legacy records to reconcile. After
that reconciliation, remove the freely selectable `needs_input` option or
make attempts to set it require the explicit dependency flow. Retaining the
enum value temporarily for migration is different from continuing to let it
create a phantom request.

Do not implement this by returning a different `task.status` at the same Task
revision. Add independently versioned input observations and notify/render
them separately from local status-edit intent. Account for out-of-order reads,
Signoff responses in another pane, current drafts, and permission changes.
Choose the exact input version/change-feed mechanism before coding.

The existing change feed in
[`service/changes.py`](../app/qorqut/service/changes.py) maps Signoff changes
to the `signoffs` kind and Task changes to `tasks`. Its versions also back HTTP
ETag reads. Register binding changes and the cross-scope Signoff dependencies
for the affected task views: changing a response must invalidate the input
projection even when neither the Task row nor its owning project changed.
Review the compatibility readers as well as canonical operation responses.

### 4. Render the same requests everywhere

Task rows show the derived indicator/count; opening the record shows its
pending Signoff cards together above the conversation using `sgChatMount`.
The source and Signoff identity must be the same in both places. The marker
must also work for special Task types whose ordinary status ball is hidden.

Historical cards and responses remain in history. Pending dependencies must
not depend on someone posting a duplicate chat message or updating an overview.
Preserve unsent Signoff answers across refreshes, and use the existing exact
proposal binding on submission. Provide a truthful unavailable/error state;
loading or losing read access is not equivalent to resolving a request.

### 5. Reuse response delivery for reassessment

`EventFinalizer.targets` already detects decision changes and emits
`response_submitted` for the proposer. Extend that path as necessary for the
authorized coordinators/responsible agents of linked sources. Dedupe delivery
when a single request is linked to several sources for the same agent.

Do not send private answer text to a task audience merely because the Task is
linked. Events can refer to the Signoff and let its authorized reader enforce
access. Confirm cancellation and supersession also prompt the required
reassessment; they do not necessarily change `decision_json`.

The current inbox/nudge mechanism can handle delivery. Do not start native
sessions or add a background worker for this feature.

## State rules to encode

| Condition | Owner-input behavior |
| --- | --- |
| Linked pending owner-only input request | Show the question and Needs your input. |
| Linked pending action approval | Show that approval, without creating a duplicate input request. |
| Draft review saved but not returned | Keep the owner-input marker; a saved draft is not an answer. |
| Input answered or draft review returned | Stop counting that request; retain the answer and notify the appropriate agent. |
| Approval approved/applied | Owner response is no longer pending; execution outcome remains a separate concern. |
| Rejected or cancelled | Remove that response dependency from the pending set; retain history and reassess work. |
| Failed/conflicted approval | Do not count it as an unanswered question. Show the execution/conflict issue through the existing Signoff behavior; an explicit retry/new review can be requested where appropriate. |
| Request superseded/replaced | Never leave a pending marker pointing only at the old request. Transfer dependencies explicitly and atomically when the replacement is intended to answer the same need. |
| Several linked requests | Keep the marker until all relevant pending owner responses are resolved. |
| Question routed to management | Do not label it owner input unless the owner is the actual required responder. |
| Notification snoozed, notify disabled, Focus archived | These change presentation/delivery, not whether an answer is pending. |
| One Task completed or its dependency detached | Do not cancel the shared Signoff or other tasks' dependencies. Its standalone lifecycle remains independent. |

For closed/archived tasks, specify whether the task-list marker is suppressed;
the still-pending Signoff must remain accessible in its own queue. No automatic
parent/child propagation: link each affected task deliberately.

## Concrete implementation touchpoints

| Change | Files/entry points to inspect or update |
| --- | --- |
| Binding schema and lifecycle | Operations storage contribution/registry and a small binding module; input creation in `signoffs/inputs.py`; explicit link authority in host composition where necessary. |
| Decisions and replacements | `signoffs/service.py` (`_update`, `stage`, `respond`); `signoffs/inputs.py`; existing inbox event finalizer and host routing. |
| Task projections and queries | `tasks/operations.py`, `tasks/overview.py`, `tasks/associated.py`; host `agents/attention.py`, especially `task_summary`, ranking, reassessment and removal policy. |
| Thread projection | `inbox/thread_views.py` (`task_context`, `related`) and `inbox/private_conversations.py` source resolution. |
| UI state and rendering | `task-state.js`, `tasks.js`, `business-tasks.js`, `global-tasks.js`, `attention-board.js`, `agent-focus.js`, `conversation.js`, `signoffs.js`. Include list paint signatures and task-type status hiding. |
| Refresh and cache dependencies | Host `service/changes.py`, `transport/http.py`, `transport/compatibility.py`, and the dashboard subscribers for task/Signoff refresh. Binding writes and cross-scope responses must invalidate the same derived input projection. |
| Move to project | Operations `tasks/relocation.py` and host `domains/tasks/relocation.py`: extend preview tokens/dependency inventory and either carry the binding with checked scopes or report a precise blocker. |
| Migration | Follow the explicit inspected-predecessor/target and instance-lock pattern in [`migration/task_needs_input.py`](../app/qorqut/migration/task_needs_input.py). It is an example, not the migration to reuse for this new schema. |
| Agent instructions | [`role-workflow.md`](../../qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/qorqut/agentic/assets/role-workflow.md), [`thread-replies.md`](../../qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/qorqut/agentic/assets/thread-replies.md), [`agents/skills/role/SKILL.md`](../../qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/qorqut/agents/skills/role/SKILL.md), and the owner-input/owner-focus default packages named below. |
| Operator/developer docs | Update this plan and [`SIGNOFF-INPUT-NOTIFICATIONS.md`](SIGNOFF-INPUT-NOTIFICATIONS.md), including its stale response-delivery description. |

Host instruction sources have moved into
`qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/qorqut/`;
the links above use that current location. Default package sources are under
`qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/imported-role-skills/native/skills/owner-input/`
and `owner-focus/`, each with `SKILL.md`. Their current guidance already uses
Signoffs for questions but directs substantive owner work into tasks and small
browser interventions into References. The new "all input collection" rule
must reconcile those routes explicitly; no blanket rewriting of unrelated
permission or execution rules.

The runtime hint in `agents/attention.py::task_update_hint` currently says to
record the question in the Task thread. Change that hint as well as Markdown
instructions, otherwise the two routes will continue to conflict. Updating
default files alone does not adopt new instructions into running agents.

## Reconciliation and rollout

1. Audit stored `needs_input` tasks and their visible evidence. Report proposed
   existing-request links, genuinely missing questions, and stale statuses.
   Do not guess a current question from the latest comment.
2. Implement and exercise the new bindings/projection using synthetic data.
   Include legacy-row behavior before enabling the stricter write contract.
3. Run the schema upgrade through the installation's explicit release/migration
   path. Preserve histories, revisions, prepared approvals and extension data.
4. Apply reviewed per-task reconciliation through authorized domain operations.
   Do not bulk create near-duplicate Signoffs or answer/cancel them on the owner's
   behalf. Do not copy a private conversation into shared Task notes.
5. Adopt the updated agent guidance and validate the list/thread views together.
   Publishing/deploying and live reconciliation are later work, not actions
   performed by this documentation branch.

## Checks before implementation is considered complete

- Standalone input, linked input, existing approval, multiple requests per task,
  and one request shared by several tasks; duplicate/retried operations converge.
- Atomic question-plus-link creation: either both commit or neither does. Failure
  to authorize a source must not leave a newly created orphan question.
- Attaching a prepared approval that expects Task revision N does not change
  that revision or make the approval stale. Ordinary concurrent content edits
  still invalidate prepared changes as before.
- Lists, task details, summaries and threads show the same pending request set,
  including requests older than the loaded conversation page. Special Task
  types, empty conversations, Global sources and cross-scope approvals work.
- Answer, cancellation, rejection, replacement and application transitions;
  multiple linked tasks; appropriate reassessment events without duplicate or
  unauthorized answer delivery.
- Respond-versus-link, cancel-versus-link and replace-versus-response races;
  stale response bindings, retries and post-commit uncertain receipts.
- Task revision unchanged on answer: refresh clears the input marker anyway.
  A slow stale response cannot resurrect it or erase a newer local status edit.
  Unsaved Signoff answers survive unrelated list refreshes.
- Permission denial/revocation and inaccessible linked requests do not leak
  titles or answers, fall back to a broader scope, or become fake successful
  resolutions. Delegated management questions are not owner requests.
- Closing, archiving, unlinking or moving one source does not cancel a shared
  request. Move preview/apply keeps exact scoped identities and existing
  approval constraints.
- Notification snooze and Focus archive do not clear pending input. Legacy
  migration is idempotent, preserves history and has a deliberate missing-link
  state instead of silently declaring all old tasks answered.
- Bounded list queries and independent input freshness: no per-task full-form
  fetches and no recurring status-reconciliation job. Conditional GETs must
  not return a stale 304 after a linked Signoff changes in another scope.

## Investigation validation performed

No application behavior or production data was changed. Two isolated probes
were run directly against this baseline's source:

1. Executed the actual `relationships` CREATE TABLE statement in an in-memory
   SQLite database. A Global `task -> signoff` `concerns` row was accepted; the
   same Global row with role `awaits_input` failed the CHECK. A project-scoped
   `awaits_input` row passed that schema CHECK. This verifies storage behavior,
   not endpoint authorization or a complete proposed implementation.
2. Loaded the actual `task-state.js` into a Node VM. Observing revision 1 with
   `needs_input`, then revision 1 with `open`, retained `needs_input` and emitted
   no second status notification. Revision 2 with `open` was accepted and emitted
   the update. This confirms the derived-status cache issue.

Read-only tracing also verified prepared-target revision validation, Signoff
response events, delegated-input defaults, source discovery and move blockers.
At the original baseline, the published checkout included a small host test
subset but no Operations or dashboard test directories. Current remote `main`
also includes dashboard tests such as `signoff-chat-reference.test.js` and
`focus-task-move.test.js`, plus additional host tests. Use those existing
harnesses where applicable. The original checkout's untracked files were not
copied. The checks above are investigation probes, not a claim that the future
feature has passed integration tests.

## Decisions still to settle during implementation

- Final binding schema/operation names and the exact authority required to
  attach an existing request across scopes. Separate binding storage is the
  current recommendation.
- The public read shape and independent freshness/version contract for derived
  input, plus how legacy `needs_input` status filters/pickers transition.
- General non-task conversation coverage in the first delivery and the display
  policy for closed/archived sources with outstanding requests.
- Whether pending input can be explicitly answered through ordinary chat. The
  minimal version requires the Signoff response controls; a comment is not a
  submitted answer. Chat-to-answer inference would need separate attribution
  and confirmation rules and is not assumed here.
- Generic input replacement and dependency transfer as one transaction; avoid
  introducing a brief false "no input needed" state between cancel and recreate.
- How arbitrary generated conversational questions are kept compliant. Domain
  validation can enforce the task-marker/link invariant, while agent guidance
  governs question phrasing. This plan does not claim that code can detect
  every request for input in free text.
