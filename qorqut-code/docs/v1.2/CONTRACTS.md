# v1.2 contracts and boundaries

This document specifies the planned target. It does not describe already implemented v1.2 behavior. The owner decisions and current authorization are in [the plan](README.md).

The owner subsequently accepted the standalone engine, new host composition, folder separation and operational Git history. Preserve the behavioral contracts below while moving their code to the owners defined in [architecture](ARCHITECTURE.md). References to “v1 policy/application” mean the preserved semantics inside the new Operations package, not a second running v1 core.

## Identity, ownership and authorization

| Concern | Target contract |
| --- | --- |
| Principals | Keep v1 principal IDs, registrations, global/business agent kinds and role resolution. Do not generate a replacement all-global roster or adopt v2.2's six-character agent-ID scheme |
| Business ownership | A Task or Action belongs to one business or the explicit global namespace. A global list/aggregate view does not change that ownership |
| Operational reads/writes | Resolve current v1 scope and capabilities for every operation, relationship endpoint and response. No rights follow merely from a tag, a native-session ID or a system-origin label |
| Context reads | Grant all registered agents access to the complete published shared Context and its published evidence. This is the deliberate read-access change |
| Context writes | Preserve current v1 write authority and prepare/apply requirements. Global read access does not grant global submissions, protected edits, file access or owner decision powers |
| Mutations | Carry canonical v1 invocation, revision checks, idempotency receipts, policy preparation and application into Operations. New operation names must map explicitly to their corresponding semantic rights; do not install a second permission table |
| DMs | Preserve participant privacy and role contact restrictions. Shared Context does not expose another agent's DMs or raw native conversation |
| Lifecycle | Native sessions are started manually. Nudges, Context jobs, snooze expiry and ordinary API calls never start an interactive agent |

The v2.2 choices to use global instances of the Ordubashy role, grant broad editing rights merely because material is under `operations/`, remove host authentication, or broaden arbitrary script scheduling do not transfer to v1.2. The adopted folder/host structure retains v1 identity and role authority except for the explicitly selected changes.

## Task model

- States are `open`, `working`, `waiting`, `done`, `cancelled`. All current callers, counts, filters and controls use these final values. Do not keep a second live `doing`/`blocked`/`dropped` state vocabulary or a mode switch.
- A Task may have one parent Task, multiple assignees and multiple informational dependencies. Reject self-parenting and ancestry cycles. Generic related-work links are separate from the parent hierarchy.
- Parent and child states remain independent. Completing a parent does not complete its children, and unfinished children/dependencies do not automatically prohibit normal state changes. Dependencies communicate blockers; they do not become an execution gate.
- Parent completion/reopening uses ordinary Task permissions through v1 policy. No Story-only owner-close gate survives on a parent Task.
- A Task has one shared `snoozed_until` timestamp, not one per assignee. Snooze suppresses the Task's due reminder until that time; it is not a new Task state, cancellation, completion, subscription change or deletion.
- Due reconciliation creates one durable due event for the current snooze generation, for current assignees or the creator when unassigned. Repeated ticks cannot create repeated notifications for the same due state. Completed/cancelled Tasks do not produce obsolete work reminders.
- Task snooze is distinct from retained owner Focus-card deferral. Update the Task controls/read model without changing Focus ordering or its independent card workflow.
- Preserve v1's operational scope checks for assignees, parent/dependency links and related resources. No new cross-business access is granted by this port. An agent never gets another business's Task body through a linked parent or dependency.
- Existing parent/child work relationships use normal typed Task references. There is no hidden `is_story` class, parallel Story table or Story API alias in the target runtime.

The target Task schema is prepared in isolated code/fixtures only. No conversion of saved Task states, custom status configuration or Story records is attempted during this phase.

### Retained Task fields and API matrix

Unselected v1 fields are retained. Changing states, tags, snoozing and Story references does not authorize dropping other Task behavior. These are target interfaces, to be expressed as schemas and representative caller fixtures in P1; they are not claims that the port is implemented. Existing operation names remain the final API where listed, with one updated implementation and schema.

| Existing behavior | Disposition | Target field/API | Acceptance check |
| --- | --- | --- | --- |
| Calendar due date | Retain | `due: YYYY-MM-DD or null` on `tasks_create`, `tasks_update.changes`, `tasks_get/list`; `due_before` remains an inclusive calendar-date filter | Round-trip a real date, clear it with null, reject invalid dates; snoozing changes neither the date nor its filter result |
| Archiving | Retain independently of completion | `archived: boolean`, default false; `tasks_update` archives/unarchives; `tasks_list.include_archived` defaults false | Archive/reopen visibility without changing Task state, assignees, due date, children or discussion; explicit detail access still uses normal authorization |
| Typed custom fields | Retain | `fields` values on Task reads/writes; owning-scope field definitions through `tasks_config_get` and `tasks_config_update` | Preserve `string`, `int`, `bool`, `date`, `enum` validation, reserved-name rejection and revision checks; reject a definition change that invalidates existing synthetic Tasks |
| Custom state and closed tag-list configuration | Replace | `tasks_config_get` exposes the fixed state definitions, the shared tag-catalog reference and owning-scope field definitions; `tasks_config_update.config` changes field definitions only | Old configurable-state/tag writes fail clearly; fixed states and catalog labels are not silently rewritten by a configuration update |
| Task kind and requests for the owner | Retain | `kind: ordinary or request_to_owner`, default ordinary; preserve the create-time requirement that an `request_to_owner` explicitly assigns the installation owner | Round-trip both kinds; reject creation without the owner assignment; preserve owner-task/coordinator routing and existing assignment restrictions; never treat the kind as a Signoff |
| List and detail consumers | Retain, adapting changed model fields | Keep IDs, title, revision, author/timestamps, assignees, `fields`, `due`, `archived`, `kind`, search, pagination, tag/assignee filters and `due_before`; `closed` derives from `done/cancelled`, `paused` from `waiting` | Source-defined DTO fixtures exercise Focus, setup, reports, owner-task routing and registered Task callers without a second Task representation |
| Closed/paused/snoozed list defaults | Retain the independent filter controls; replace snooze payload | `include_closed` and `include_paused` remain true by default; `snooze_filter=active` remains the default, with `snoozed/all` available; target writes use `snoozed_until: timestamp or null` | A waiting Task is not automatically snoozed; archived, closed, paused and snoozed filters remain independent; remove the old indefinite-snooze write shape and update callers |
| Configurable states, completion and blockers | Replace selected semantics | Fixed `status` enum; `tasks_complete` defaults to `done` and accepts only terminal states; `blocked_by` remains informational with valid, visible, distinct Task targets | Normal Task permissions govern completion; dependencies do not prevent it; invalid/missing/self links and dependency cycles remain rejected |
| Comments, note references and Story grouping | Replace storage/Story targets; retain useful caller functions | `tasks_comment`, `tasks_thread_get`, `tasks_notes_list`, create/update `note`, summaries and typed note references all use the one canonical item thread; `parent_id` and related Task links replace `story_ids/story_context` | A comment written through either retained entry point appears once, with actual/represented attribution; linked-resource privacy holds; no Story alias or duplicate note writer |

The field-definition scope remains the owning business or the explicit global namespace; sharing tag labels does not share custom-field configuration. Preserve v1 configuration-change authorization and validation against current records. P1 must enumerate actual callers of removed fields, including setup guards, rather than silently accepting and ignoring their arguments.

## Shared tags

The new shared tag catalog initially covers Tasks and Actions. Existing Signoff/routine labels remain owned by those v1 systems; do not enlarge their schemas to match the v2.2 tag catalog as part of this port.

Tags support creation, listing, application and removal from an item. Previously used tags remain discoverable in the shared catalog. No renaming or merging feature is needed. Validation prevents duplicate catalog entries for the same normalized label; presentation retains a stable display form.

A business-named label is still only a label. It does not create a business/folder, transfer ownership, create a second owner or grant access. Item listing resolves the caller's operational access first and then filters visible items by tags. The shared catalog may reveal label names, as requested, but never leaks unauthorized item contents or counts through tag queries.

An item thread inherits its item's tags for display/filtering; it has no separately editable tag set.

## Actions and Context submission

An Action records a business/global activity or outcome. It is neither a planned Task nor the generic technical audit log.

- Each Action has one owning scope, stable identity, current revision, immutable revision history, attributed actor, recorder and provenance. Use the actual v1 principal IDs.
- Creation, correction and withdrawal append accepted revisions. A withdrawal retains history. A real-world reversal is a new Action describing what happened, not destruction of earlier evidence.
- Corrections recheck expected revision and current authority. Human representation records attribution; it never grants an agent the owner's decision powers.
- Record the Action revision and its exact Context-queue input atomically. The input references that immutable revision and its tags, scope, related objects and source provenance; later edits cannot silently change queued evidence.
- Filing is asynchronous. Context failure leaves the accepted operational Action intact and exposes queue state separately.
- Task completion does not require an Action. Recording an Action does not complete a Task. Discussion, Signoff decisions, inbox delivery, routine receipts and other bookkeeping do not automatically become Actions.
- Audit and job receipts stay with their current owners. Inventory existing `action_logs` writers to distinguish real activity from technical receipts, then route intended Action producers into the one canonical revision path. Do not maintain old and new authoritative activity writers.
- Mutations subject to approval continue through **v1** prepared Signoffs and application. Do not import v2.2's separate one-use grant/approval ID model.

Existing activity UI/readers consume the canonical Action read model. They must not create a second Action on a retry or when projecting a technical receipt.

## Threads, messages and follows

Three concepts remain separate:

| Concept | Purpose and lifetime |
| --- | --- |
| Native conversation | The provider's exact terminal/CLI session, whose raw transcript is archived |
| DM | A durable participant-bound conversation, including existing private owner/peer channels |
| Item thread | Shared discussion attached to an authorized Task or Action, surviving native-session replacement |

Task/Action threads are readable by those allowed to read the item. The thread and its writes use that item's current authorization. Reading does not automatically follow the item or acknowledge all inbox events.

Creators, assignees and commenters follow by default. Explicit unfollow remains sticky until explicit follow. Required responsibility updates still reach responsible assignees; optional discussion subscription is independent. Preserve existing v1 coordinator routing for owner work and other mandatory recipients without treating it as permission to read unrelated work.

When supported relayed comments identify the owner as the represented speaker, retain the actual recorder as well; the owner and recorder receive the applicable follower treatment. This is attribution, not formal approval, owner impersonation or a new permission to relay protected decisions.

V1 has private owner/agent conversations bound to individual operational items. Preserve those as participant-private DM bindings. Do not merge them into the shared item thread or expose their contents because an item is readable. The retained Focus and agent pane can continue to show these private channels through their existing presentation contract.

Retained Signoff, email and other domain discussions keep their current write owners and gates. Shared presentation/event code may refer to them, but a generic thread-post API cannot bypass a Signoff rejection-note requirement, existing commenter restrictions or formal review. Do not maintain duplicate Task comments and thread messages as separate sources of truth.

## Native sessions and Chat

The v1 registry remains the source of agent identity. The new native-session integration binds each session to the exact registered agent, provider, native conversation ID, session generation, workspace and verified process/terminal identity. A reused PID, pane or display name is not enough.

One active native conversation per agent is supported. Switching/selecting an agent in Chat does not launch it. Both Claude and Codex use their respective adapters through one backend delivery contract.

| Situation | Direct Chat behavior |
| --- | --- |
| Offline | Reject and ask the owner to start the agent manually; do not silently queue an offline Chat message |
| Ready | Submit into the exact existing native conversation |
| Busy with supported steering | Steer that ongoing conversation |
| Busy without supported steering | Queue for the next turn of that same exact conversation |
| Definitely unsent when that session ends | Mark undelivered; the owner can resend |
| Submission outcome uncertain | Preserve uncertainty and receipt evidence; do not automatically replay into that or another session |

Use one arbitration/locking path for all input producers: Chat, inbox nudges and retained authorized terminal delivery. Bind every accepted input to a durable ID and exact session. Respect human composer ownership and verified provider readiness; never paste into an unknown prompt, take over an unverified terminal or use a blanket interrupt. The provider adapter must actually submit accepted text, not merely leave it typed in the composer.

Do not replace v1's existing terminal lifecycle or role control permissions with v2.2 global-agent defaults. Removing an old independent Chat runner must not remove legitimate terminal-control APIs or start a replacement agent automatically.

## Backend inbox nudges

The backend observes durable recipient inbox events and submits a short notice through the same native input path as Chat. Its dispatcher runs with the backend, independently of any browser or agent-side polling process.

Required semantics:

1. Persist application origin `system`, type `inbox_nudge`, an input/delivery ID and Chat marker metadata. Keep provider transport roles separate: Claude/Codex may receive ordinary native input even though Qorqut marks the notice as system-generated. It is never displayed as a message personally written by the owner.
2. Direct the recipient to the existing MCP inbox tools. Preserve the priority instruction: continue an active direct user request, checking the inbox after it finishes or at a suitable checkpoint.
3. Submit immediately when the adapter has a safe mechanism, including while busy. Report processing as unconfirmed unless the provider supplies evidence. A safety deferral is a visible pending delivery, not fabricated success or a deliberate wait-for-turn policy.
4. Coalesce eligible events into one outstanding nudge per agent, using durable per-conversation event coverage (the attempts' covered event ids). Events eligible together share one bounded notice; events arriving during the minimum spacing wait and go out together afterwards. Reconcile already handled/stale events before submission.
5. Submission alone does not mark inbox events read, acknowledge them, complete any item or authorize any operation.
6. If checking is deferred, retain the durable need. Eligibility is event-based (inbox reliability repair, 2026-10-14): an event warrants a notice when it is delivered, unacknowledged, currently authorized and not already announced or possibly announced to the same native conversation. New events are announced independently of older pending ones; an unchanged backlog is never announced again after Chat inputs, replies or elapsed time. Chat and work progress neither rearm nor gate a notice. Do not resend on every timer tick or require an agent-maintained timer. Bound the retry policy and test it explicitly.
7. Offline events stay pending. After the next manual start, re-evaluate outstanding events and target the newly verified session. Never rebind an uncertain delivery receipt to a different session.
8. Preserve existing event producers, recipient rules and deduplication identities for retained domains. A bare Signoff transition that v1 deliberately does not broadcast must not acquire an invented new wake merely because a generic nudge dispatcher exists.

Remove agent-side inbox polling, Claude Monitor setup, agent watcher launch/recovery instructions and maintenance dependencies that exist solely for polling. Keep MCP inbox listing/acknowledgment tools, native identity validation, backend queue reconciliation and unrelated monitors. An implementation inventory must distinguish these before deleting broadly named watcher helpers.

## Raw transcript retention

Archive complete raw provider transcripts for verified registered sessions, append while active and capture the final tail after exit. Retain them until the owner explicitly deletes them. Preserve their binding to agent, native session, source file and byte/hash progress.

Readable Chat is a derived view; do not make a second normalized Chat transcript the durable authority. Backend input/delivery records supply provenance and delivery markers alongside provider-native events.

Only exact correlation can label an observed transcript entry as a specific owner message or system nudge. Matching text alone cannot assign identity. If provider evidence cannot correlate an entry, present the backend notice/receipt truthfully rather than inventing a provider role or processing confirmation.

Do not scan personal provider-history folders to discover unregistered conversations. Existing authorized learning/transcript consumers may read the archive through their retained scope/enrollment contract; this port does not introduce new learning, enrollment or automatic evolution behavior.

### Lossless archive representation and growth

Use one append-only raw byte archive per verified native conversation/source lineage, plus durable capture receipts. This deliberately replaces the donor's whole-file-per-capture storage in `app/qorqut_app/native/transcripts/__init__.py`: that implementation writes every complete prefix again and retains the earlier files.

- The archive owns exact provider bytes, including malformed records and partial final lines. Capture appends only the newly verified suffix. Derived Chat parsing cannot rewrite, normalize or discard archived bytes.
- A capture receipt binds the agent, provider, conversation, verified source identity and observed session generation to an archive ID, committed byte length, prefix digest, predecessor receipt and completeness evidence. Older captures are bounded prefix views of the same file, not duplicate files. Resuming the same verified conversation does not recapture its entire prefix under a new generation.
- One capture writer serializes append/publication. Verify the previously committed prefix and exact session binding, append and durably flush bytes, then publish the receipt and latest pointer. Readers use a committed receipt boundary, never an uncommitted tail. A source that loses bytes or changes previously captured content, unexpected replacement or archive corruption is a visible error; do not replace a trusted archive with the new source.
- After a crash, verify any extra archive suffix before adopting it into a new receipt. Preserve uncertain bytes for resolution and do not append the same suffix again. Recovery cannot claim completeness, overwrite a committed prefix or silently discard uncertain bytes. Capture retries without new bytes or new completeness evidence create no new payload or receipt.
- Raw range reads are bounded by the selected receipt's byte length. Indexing and hashing use bounded-memory streaming; no capture creates an in-memory or on-disk collection of all earlier transcript copies. Prefix-integrity verification may still read earlier bytes; the storage guarantee is not a claim of constant-time verification.
- Record malformed-offset/index evidence incrementally, not as an ever-growing list copied into every receipt. Mark final-tail capture separately from a complete observation while the process is active; inability to verify the final tail stays explicit.

**Acceptance:** use synthetic Claude/Codex recordings to check byte-for-byte reconstruction, old-prefix reads, partial/malformed tails, exact-session rejection, unchanged polls, crash recovery and final-tail capture. Add one storage-growth fixture: 256 captures appending 64 KiB each must retain exactly 16 MiB of raw payload, not the approximately 2 GiB sum of all prefixes. Count every archive payload, receipt, index and leftover capture file; for this valid-record fixture bound metadata by a fixed per-capture/per-entry budget declared in the test. Repeating unchanged captures does not increase storage, and doubling captures/bytes grows storage linearly. This is a focused regression check, not a benchmark suite or a change to retention policy.

### Durable Chat identity, reads and owner notifications

Raw transcripts remain authoritative for native conversation content. Keep durable delivery receipts, message identity/completion metadata and Chat read positions in the one operational store. Preserve existing owner attention acknowledgments in their own durable preferences contract, with paths retargeted by S1. A disposable rendering/search cache is permitted; an independently writable normalized conversation is not.

| Contract | Target interface and behavior |
| --- | --- |
| Stable message identity | Key an entry by registered agent, provider, native conversation/source lineage and a verified native message/item ID; when none exists, use the immutable raw record start plus a provider-defined entry discriminator. Do not use capture UUID, page offset, timestamp/text hash or active process generation as message identity |
| Streams and completion | Provider fixtures define which records form one logical reply and when it is complete. Stream updates retain that reply identity. Persist its one completion identity/cursor only when eligible; parsing it again, observing a later capture or resuming the conversation cannot create another reply notification |
| Durable ordering | Allocate a monotonic per-agent completion sequence across native conversations. Store the stable identity-to-sequence binding, completion evidence and raw locator durably; rendered bodies remain derived. Arrival/completion order used for unread tracking is separate from transcript display order and pagination |
| Read boundary | Keep the owner-only `POST /api/root/agents/{agent_id}/chat/read` with `{sequence}`; it advances the owner's per-agent cursor only to an existing completion boundary that the client rendered. Return the observed boundary with the Chat page/event. A stale read request cannot clear a concurrently completed newer reply; fetching/prefetching/selecting alone does not mark unseen replies read |
| HTTP/read models | The existing Chat state/history/events endpoints expose stable entry IDs, completion sequences, exact conversation binding and application-origin/type metadata. The reply-notification reader joins identity/read metadata to a bounded derived preview instead of querying an authoritative normalized message body |
| Eligible replies | Preserve completed, nonempty, owner-visible assistant replies. Exclude partial output, tool-only/internal records, owner-input echoes and system-generated nudge records. An assistant's actual visible reply after a nudge is still an assistant reply; the nudge itself is never one |
| Owner attention and navigation | Preserve the latest eligible unread reply per active enabled agent. Global-agent replies retain existing attention/toast eligibility; business-agent replies retain `nav_only` behavior. Keep active-business/agent and owner authorization filters |
| Independent acknowledgments | The owner's attention-queue notification acknowledgments remain separate from Chat read cursors, inbox acknowledgment and Signoff state. Use stable notification IDs derived from reply identities so restart/reprojection does not resurrect an acknowledged entry. Reading Chat removes its unread indication; acknowledging a card does not silently mark conversation content read |
| Background ownership | Capture, completion indexing and notification availability run in the host with Chat closed. The browser only renders/records actual reading; it is not the notification producer. Keep private item-DM read/notification semantics with their existing owner |
| Rebuild and retention | Rebuilding a derived cache must reuse durable identity/completion bindings and saved cursors. Never reset read state to regenerate a view. Ended native conversations remain readable and retain unread state; an integrity failure reports unavailable evidence, not a fabricated new message |

The completion sequence is not the SSE transport cursor, provider message role or an inbox event ID. Register the durable metadata as a host-owned schema contribution through Operations; there is no second database or permission authority. Update the existing client directly to the final response schema.

**Acceptance:** with Chat closed, append a complete synthetic reply and reconcile the backend; its notification/nav indicator appears. Render/read through its completion boundary; the indicator clears. Restart and rebuild the rendering cache; it stays cleared and the reply/notification IDs remain unchanged. Also check business `nav_only`, separately acknowledged attention cards, partial-to-complete output after an earlier read, a concurrently arriving later reply, same-conversation resume and a system nudge that creates no owner-message or reply notification.

## Shared Context

Replace the old `qorqut-contextbase` host integration with the pinned new `qorqut-context` library and its published graph/search/history model. There is one shared graph. Business ownership remains operational metadata, not a separate Context engine per business.

The owner selected general graph-based Context only. Home/bootstrap content is a bounded read of published node summaries, attributes and relationships, with graph references and a pinned revision. Remove the dedicated living-brief record, editor, write operations and maintenance requirements from the target; do not recreate them as a second summary store or a generated-brief job. An empty graph produces an explicit empty view, not invented brief content. This presentation decision leaves the agreed ordinary and Action-revision input paths into the shared engine intact.

All registered agents may read its published Context, including evidence deliberately published into that graph. Links back to raw Tasks, Signoffs, DMs, uploads or transcripts still pass the source system's authorization. A global Context query must not proxy arbitrary protected source reads.

Use one ordered durable queue for ordinary Context inputs and immutable Action revision inputs:

- A canonical input records submission authority, source revision/provenance and stable idempotency identity.
- `GENERAL` submissions cannot masquerade as backend-created `OPERATIONAL_ACTION` revisions.
- A blocked head input pauses later Context processing, not operational work. Notify the configured v1 main coordinator through its real registration, rather than hard-coding the v2.2 Ordubashy ID.
- Explicit retry or discard reconciles the engine's single pending-input state and the host queue together. A backend restart cannot silently skip a blocked input or generate a duplicate publication.
- Subjectless/global Actions remain available through tags/global activity history; do not invent a Context entity for every tag.
- Context pipeline/provider jobs are bounded backend computation, not a manually managed agent session or authority to auto-start interactive agents.

Keep v1 write-policy preparation and approval where it applies. An approved protected Context operation can enqueue its exact frozen input through the existing v1 application path. Queue publication does not turn agents into approvers or accept unreviewed replacement arguments.

Replace old `legacy`/`both`/`base` filing branches and retire the old engine's active writers. Update current Context reads in briefings, working memory, reports, existing Context UI and background handlers to the target model. Do not retain both filing pipelines as a rollout strategy.

Existing Context-specific registered jobs necessarily change their handler/contract to this engine. That is permitted integration work; replacing the generic scheduler, broadening script execution or creating routines automatically is not. Existing non-Context records/features must not disappear simply because their read model was formerly assembled by the old Context service.

### Retained Context interfaces

Keep the selected shared graph and the current write/owner-diagnostic authority. The matrix names final host operations; preserve a useful operation name without retaining its old engine implementation. P1 supplies exact request/response schemas and caller fixtures. Missing donor features are bounded integration work, not permission to remove a view silently.

| Existing behavior | Disposition | Target interface | Acceptance check |
| --- | --- | --- | --- |
| Graph lookup/search, neighborhood and history | Replace implementation; retain retrieval functions | `context_object`, `context_find`, `context_expand` read a pinned published snapshot using new node/edge IDs, summaries, attributes, relationships, source history and explicit pagination; `context_changes` retains its owner-only diagnostic boundary | Existing UI/bootstrap/report readers consume the final DTO; two business agents read the same published graph through agent-permitted queries; draft data and protected raw sources remain inaccessible |
| Living brief/home/bootstrap pack | Remove dedicated living briefs; replace home/bootstrap content with the general published graph | `context_home` and bootstrap DTOs return bounded published node summaries/attributes, relationships, graph references and revision. Update consumers directly; remove brief-only fields, editing routes and maintenance requirements | A synthetic graph supplies the home/bootstrap view at its published revision; an empty graph is explicitly empty. No dedicated brief schema, writer, editor or background generation remains |
| Explicit typed graph edits | Retain deterministic edits; replace old graph schema | `context_edit` accepts typed operations on new node/edge records, source/citation/support evidence, rationale and expected published revision. A queue entry freezes that exact plan; the controlled Context writer stages/validates/publishes it without model reinterpretation | Enumerate each retained graph-edit verb in P1 and map it explicitly; remove old brief-only verbs. Valid edits produce one attributed event; unauthorized targets, unsupported verbs, stale graph bases or invalid support fail without publication; retry cannot reinterpret the request |
| Focused instructions | Retain permission/attribution; replace filer | `context_instruct` freezes text, people/source references and attribution into a GENERAL input through its registered program | Only currently permitted roles/scopes submit; request/job/input/result identities survive retry and restart; no operational authority follows from a handoff note |
| Upload filing/retry/retraction and status | Replace engine integration; retain workflows | Existing document upload operations and Context-specific programs use the completion contract below; `context_retry`/discard controls address an exact input, while source edits/removals create distinct revisions | Status does not claim processed at enqueue; publication reaches the authorized upload/job receipt; old and new source revisions cannot be confused |
| Filing decisions and upload evidence/map | Retain diagnostic purpose; replace payload | Owner-only `context_filings`, `context_upload_map` and `context_changes` expose input/event/commit, attempts, publication effects/source links and available diagnostics from the new engine | Inspection reaches the exact source revision and published changes; unpublished attempts are marked as such; no fabricated old-filer decisions or invented generated outline |
| Filing-cost view | Retain accounting and access; replace old model-specific columns | Owner-only global `context_filing_costs(start_date, end_date, limit, cursor)` returns per-input/attempt/provider/model usage, known USD totals, unknown-call count, completeness and stable result references. Replace hard-coded Luna/Jev columns in callers | Date-filter/pagination totals include failed and retried attempts once; replayed receipts do not duplicate costs; missing usage/pricing is unknown/partial, never zero or a fabricated price |
| Old filing modes and legacy graph objects | Remove | No `legacy/both/base` selector or alternate graph writer; existing readers/writers call the final interfaces above | Active registration, dependencies and callers contain one pipeline and no silent fallback |

Direct edits share the ordered writer and pending-input rules with GENERAL and Action inputs; they cannot jump past a blocked input or change its published base. A deterministic edit is a controlled source event, not an `OPERATIONAL_ACTION` invented from a graph patch. P1 must bind its typed payload and expected graph revision durably, map current target-write restrictions, and preserve citation/support validation. Use the existing lower-level Context staging/publication APIs where sufficient; any missing verb needs an explicit design mapping before its caller is changed.

The pinned donor has graph staging/publication APIs. Its lack of v1's living-brief service is consistent with the selected graph-only design; P4 derives the home view from published graph reads. The retained filing-cost view still requires a durable usage-receipt collector at the runner boundary. Collect observed usage for every attempt and preserve unavailable evidence as unknown; source-wide retries must not erase earlier attempt costs. The owner cost view remains a diagnostic read and cannot initialize or mutate Context. Raw model diagnostics do not become shared Context evidence merely because all agents can read the published graph.

### Context completion for uploads and registered jobs

Context Git publication and operational SQLite completion cannot be one transaction. Use immutable bindings plus a durable completion receipt/outbox and idempotent authorized application. Queue acceptance, publication and source/job completion are three distinct milestones.

| Durable binding | Required fields/meaning |
| --- | --- |
| Source revision | Source kind/ID, owning scope, exact content revision and digest of submitted bytes plus filing-relevant metadata (such as title/participants), frozen payload, operation (`add/edit/remove`) and provenance. Exclude processing/receipt metadata from that digest; keep source identity separate from document row revisions changed by bookkeeping |
| Filing identity | Stable `filing_id` binding the source revision to the originating request and registered `job_id`, program-definition/authority reference and approved scope. Replaying the same job/submission returns this binding; a new source revision is not a retry |
| Queue identity | One `input_id` for that filing, frozen payload/digest, queue order, attempts and retained `context_event_id`. Persist the event reservation before engine submission; authority cannot be reconstructed from caller-supplied IDs |
| Publication evidence | Verified Context event, source/input digest and `published_commit`, with disposition and optional published handoff. A submitted/ready draft is not publication evidence |
| Completion receipt | Stable receipt ID binding filing/job/input/source revision to verified outcome, event/commit or discard/failure reason, and `pending/applied/conflict` application state. Uniqueness prevents duplicate document updates, job completion, notifications and cost accounting |

1. **Accept/freeze.** The authorized document/program path freezes the source revision and persists its filing association and queue entry in one operational transaction. If the job is registered before enqueue, enqueue and bind exactly once at its first bounded execution step. Never reread the latest upload and substitute it for approved/frozen input. Expose the filing/input IDs and state immediately; the upload is not processed yet.
2. **Wait without occupying execution.** A Context program uses the retained `ProgramPending`/deferral contract while publication is unfinished, releasing the serial execution claim. The host's Context driver performs external work outside SQLite transactions. A blocked Context input does not monopolize the job runner or prevent unrelated retained routines, Tasks or Chat. Deferral checks state; it is not automatic model retry.
3. **Publish and record outcome.** On success, verify the engine's immutable event/source/digest before recording `published` and a pending completion receipt in one operational transaction. Reconcile an already published exact event before attempting any model work. Only committed handoff notes generate their normal deduplicated notification.
4. **Apply through the registered job authority.** The Context program returns the frozen outcome through its declared result/output path. Replace `context_base_receipt` with a single final `context_filing_receipt` operation: trusted worker invocation, bound job/program, approved output/resources and current authority are mandatory. A generic system label or a public receipt payload is insufficient. Apply source metadata and the receipt's `applied` marker atomically; preserve the generic job executor's existing output receipts and completion/recovery semantics.
5. **Compare before source completion.** Finalization uses a fresh document row revision for compare-and-set, but verifies the source content revision/digest against the frozen binding. Receipt-only metadata changes may be rebased after that check. A content edit, removal or replacement cannot be overwritten or marked processed by an older result. Keep the older revision's filing outcome and settle its job truthfully with `source_current=false`; the current revision shows its own state, or `unfiled` when no input was accepted for it.
6. **Recover mechanical gaps.** A crash after Git publication but before SQLite outcome recording is recovered by the exact event/digest, without a second publication/model run. A crash after outcome recording but before document completion replays the pending authorized receipt. A crash after a document output committed but before job success reuses the executor's output receipt. Missing authority, unavailable evidence or a revision race remains a visible pending/conflict completion, not success or a new filing.

Expose `filing = {filing_id, source_revision, input_id, job_id, state, published_commit, completion_state, source_current, error}` through document/job reads. When no input exists, `state=unfiled` and IDs are absent. Preserve the document's existing `pending/processed/failed` status vocabulary as a derived summary for current consumers, supplemented by the exact filing state below; it is not another writable source of truth.

| Input/result condition | Upload and job consequence |
| --- | --- |
| Queued or processing | Current revision stays `pending`; job defers; expose queue position/state |
| Blocked attempt | Current revision stays `pending` with `filing.state=blocked` and error, not an unexplained spinner; no implicit model retry. Notify the coordinator once per failed attempt; later Context inputs wait |
| Explicit retry | Same filing/input/frozen source/event, with a new attempt record; clear only that attempt's block after authorization. A changed source requires another input |
| Published, completion pending | Expose the verified commit and `completion_state=pending`; document remains pending until its authorized completion output applies |
| Published, receipt applied | Mark only the matching current source revision `processed`; the registered job settles through existing success rules. A no-graph-change event is still a real publication, not a discarded input |
| Explicitly discarded before publication | Record terminal `discarded`, never `processed`; the matching current upload becomes `failed` with an explicit discard reason through the declared failure output, and its job settles as non-success. Queue progress resumes only after mechanical discard reconciliation |
| Source changed while waiting | Finish and retain the immutable old input's result without altering newer content/status. Any accepted filing of the new revision is a separate ordered input; never cancel, replace or silently refile it as part of old-result recovery |
| Completion conflict/authority failure | Preserve the published outcome and exact unapplied receipt; report the failure and use the retained job recovery path. Do not retry propagation to repair document bookkeeping |

Job cancellation is not retraction of already published Context. Before publication, the changed Context handler must reconcile its bound input through the queue's authorized discard boundary; uncertain/in-flight outcomes must first be checked for publication. After publication, cancellation cannot erase the event or report that filing never happened. P1 freezes this Context-handler mapping using existing generic job states, without changing cancellation/scheduling rules for unrelated programs.

**Acceptance:** one synthetic upload/job/input runs through publication and authorized document/job completion. Inject a blocked attempt then explicit retry, an explicit discard, a source-content edit while waiting, an unrelated metadata revision, revoked worker authority and each crash gap above. Assert exact revision/commit linkage, no duplicate completion or model invocation, truthful current-source status and an unrelated program's ability to run while Context waits. A test that proves only enqueueing is insufficient.

## Removing Stories without losing shared infrastructure

Current code lets Stories own services used by unrelated items. First make the shared resource resolver and typed relationships independent of `StoryOperations`. Then replace each current Story consumer with the appropriate ordinary Task, relationship or neutral item lookup.

| Current Story use | Planned replacement |
| --- | --- |
| Work grouping, purpose, ongoing discussion | An ordinary parent Task with description, children and its canonical thread |
| Story-to-Task membership | Parent relation for hierarchy; ordinary related links where multiple associations are needed |
| Linked materials/assets/tables/products | Typed relationships to Tasks or other existing item types; no forced hierarchy for every attachment |
| Focus/attention item lookup | Neutral permission-aware item resolver and Task presentation |
| Owner-only Story close/reopen | Ordinary Task completion/reopen, with independent child state |
| Briefings, reports, learning evidence | Final Task/thread/relationship read models, preserving their current audience and workflow |
| Story navigation/editor/tool guidance | Existing Task destination with parent/linked-work access; no separate Story destination or Story-only tools |

Do not redesign platform execution as part of this task. If a current operation cannot be mapped without changing its product behavior, isolate that exact decision for the owner rather than silently deleting functionality.

Remove Story registration, operation descriptors, active schemas/readers, permission catalog entries, routes, current instructions and UI affordances only once their needed replacement paths exist. Historical source commits and archival documentation may still contain the word “Story”; a text search alone is not the runtime acceptance criterion.

No data conversion, old-ID redirect layer, pending-approval replay or real Story-record deletion is part of this code phase.

## Retained subsystem boundaries

| Subsystem | Behavior to retain | Permitted dependent edits | Explicitly excluded |
| --- | --- | --- | --- |
| Signoffs | V1 owner review authority, exact prepared operation/revision binding, automatic application/recovery of approved plans, rejection/input rules, retry and existing notification rules | Extract generic lifecycle/executor into Operations; register domain contributions; retarget Story lookups and changed operation preparation | V2.2 response-only approval, separate one-use grants, agent recording formal owner decisions, generic-thread approval bypass |
| Routines/jobs | Registered program model, cron/timezone, current pause/manual-run controls, serial execution, existing slot catch-up/recovery/cost/authority rules | Extract definitions/state/authority into Operations, compose concrete host runners, update Context/Task/Story/transcript consumers | V2.2 fresh-Task occurrences, arbitrary script routines, new overlap/missed-slot/retry policy, automatic creation of routines |
| Focus | Card layout, curation/ordering rights, private channels, ordering/deferral and source-specific workflow | Neutral item lookup; final Task statuses/snooze metadata; replacement Task links where Stories were shown | Focus redesign, changed card authority or a second Task model to avoid wiring changes |
| Agent pane | Existing layout, tabs and global/business context | Chat mount/navigation presentation and exact session/status wiring | New overall workspace layout, removed tabs or global-only agent roster |
| Other v1 features | Existing tools/permissions and domain behavior; existing data remains untouched | Register retained services/domains with new host/engine; update imports, paths and affected typed references/readers | Browser-engine replacement, auth removal, automatic learning/evolution |
| Owner reminders | Existing v1 owner-notification tool and durable delivery behavior | Only shared delivery integration if an actual selected dependency requires it | Deleting owner reminders because v2.2 treats agent reminders as snoozed Tasks |

For routine behavior, the deployed source specifically describes globally serial runs, manual runs not advancing scheduled slots, first/changed schedules establishing a baseline, DST handling and missed slots coalescing to the latest due slot. Those are v1 behaviors to preserve, even where the v2.2 design chose differently.

## UI presentation boundary

Port the new Chat and navigation presentation into the existing dashboard mounts. The donor UI uses a different application/framework and all-global assumptions; do not transplant its root store, router, roster or full layout.

Chat may change message typography, spacing, composer, delivery status, history presentation and compact system-nudge markers. Navigation may change the agent selector/list appearance and interaction while retaining business grouping, role identity, selection and permission behavior. Preserve per-agent draft/history/scroll state where supported by the existing host.

Keep CSS scoped to these surfaces. Wire new Task statuses, snooze controls and parent links into affected existing views without redesigning their layout. Context views may need new backend DTOs and rendering fields but no unrelated visual rebuild. Story navigation is removed as part of the requested domain removal, with useful access routed to Tasks.

## No data work or transitional deployment

Source moves into `qorqut-code/` and `qorqut-roles/` and code targeting new `operations/` paths do not authorize moving real data. Operational Git history is now included as a code feature; its first test snapshots must use only isolated synthetic records/files, with coverage defined in [history](HISTORY.md).

Code may define the final schema, queries, provider adapters and synthetic initialization needed to exercise it. It must not load existing data to discover failures, run automatic upgrades against live roots, import business/agent directories, reset unsupported rows or copy a real database as a test fixture.

Later migration work will reconcile actual records, identities, files, history and prepared state against these settled contracts. That is intentionally deferred. No backup/restore procedure, gradual deployment toggle or legacy writer is introduced now in anticipation of that work.
