---
name: manage-agentic-layer
description: Maintain authored skills, instructions, preferences, custom tools and workflows through the shared agentic toolkit.
---

# Maintain the agentic layer

Use this guidance for both Ordubashy and Learning Applier/Overseer. The host
supplies identity, authority, enabled packages and project/work context. Catalog
scope, discovery, a manifest, a tool definition or this skill cannot grant tools,
credentials or plugin authority. Only plugins and user preferences are editable
through these APIs; protected roles, operations and Overseer are not. This API
boundary is not an OS sandbox.

## Decide whether the instruction is explicit

Every standing change needs an owner Signoff, including an explicit instruction.
A lesson inferred from conversation, accepted work or an edit is also a proposal.
Keep proposed changes out of active files until review and approved application. Present
the evidence references, proposed diff, applicability, uncertainty and relevant
counterevidence. Support accept, edit, reject and defer. Do not treat confidence
as approval or insist on a numerical score.

Read the actual request to determine scope. An email-specific correction belongs
in the relevant email instruction; a rule for one workflow does not become a
universal preference. If the request contradicts another rule or its intended
scope is genuinely unclear, resolve that conflict with the owner.

## Submit a direct instruction for review

Both Ordubashy and Overseer use `agentic_change_propose`. Supply a stable
`request_key`, the exact complete owner Chat message as `instruction_text`
(including whitespace), a short `summary`, the concrete `change_direction`,
`target_refs` from the catalog, and `scope` containing `description`,
`project_ids`, `work_types`, `workflow_refs`, `conditions`, and `exceptions`.
An optional `source_id` identifies the original native Chat input. The host
checks the original owner input; do not invent provenance or quote just a fragment.
This shared path does not require the private Overseer package or an extraction run.

Present the returned Signoff using the normal clickable chat reference. Read it
with `agentic_change_get`. After the owner answers, use `agentic_change_reconcile`
with the submission ID and `response`: its `reference` has `provider:
qorqut_signoff`, `signoff_id`, `proposal_revision`, `payload_hash` and the current
`status_revision`; `interpretation` is null for one choice without notes, otherwise
provide `clarity`, `action`, and `rationale` following the owner's actual answer.
Conditional or ambiguous answers require clarification through a Signoff.
Reuse request and operation keys after an uncertain response. Accept queues the
delta in the same accepted queue used by extraction; reject remains rejected,
later leaves it undecided, and revise requests revision. None applies files.

This direct path authenticates messages sent through Qorqut Chat. Other evidence
uses Overseer's extraction/review path; never fabricate a Chat input. If access
fails, report it and retain the request. Do not edit active files through shell,
generated instructions or a raw authoring call as a workaround. Application is
the separately owner-invoked Overseer workflow with its approved plan Signoff.

## Find and update the canonical asset during approved application

1. List/filter the eligible catalog and read relevant bodies and dependencies.
   Search is simple text matching; always list when a search misses. Expose
   overlapping instructions rather than inventing a priority rule.
2. Identify the canonical file already used by the relevant skill. Put universal
   preferences in `user-preferences/SKILL.md`; use a scoped plugin or skill for
   narrower guidance. Do not duplicate universal preferences.
   If no suitable entry exists, create an entry with an explicit description,
   trigger and applicability. Preserve supporting files and existing frontmatter.
3. Inspect current hashes and history. Prepare creates expecting absence and
   edits/deletes using the current SHA-256. Preserve unrelated instructions.
   Include the actor, reason and stable source references in the change request;
   in the host these fields are derived from trusted invocation context.
4. Make changes in the application's isolated branch/worktree, then publish only
   through the approved host application executor. Portable authoring APIs are
   for that managed workspace, not a bypass to active files.
   Preview with `dry_run`, validate and inspect the diff and catalog reachability.
   A conflict needs a fresh read and reconciliation, never a blind overwrite.
   If recovery is required, inspect `lifecycle.recovery_status`; recovery may
   complete only when affected files match the recorded before or after hashes.
5. Test executable behavior explicitly when tool/workflow code changes. Catalog
   reads and imports must not execute code. Use authorized runtime adapters,
   synthetic inputs and explicit scratch paths. No missing verifier or runtime
   may be reported as successful verification.
6. Group related changes in ordinary thematic Git commits when useful. Commits
   are independent of change events. Selected-path commits preserve unrelated
   staged work. A restore is also a proposed change requiring approval;
   preserve existing commits and unrelated staged or working files. Use only the
   toolkit's ordinary commit and restore operations; keep changes local and do not deploy.

## Report the actual result

Report the affected files/entries and returned revisions, checks and unresolved
conflicts. Every role receives complete canonical preferences. The next normal
prompt or resume refreshes the managed projection and relevant catalogue; native
managed skills use the existing projection. Discovering a custom plugin does not
inject or read all its bodies: locate and read relevant procedural entries. Do not
restart sessions or wake paused agents. A delayed refresh remains pending rather
than serving stale instructions. A successful save does not establish refresh or
delivery, publication or inbox delivery. A delivery receipt does not prove model
compliance; report these states separately.
Native capabilities still require the established host publication/grant path.

Every managed change records a durable event before any Git commit. The host
adapter delivers those events to Overseer through the existing inbox/nudge
mechanism. Delivery failures leave events pending for retry; an informal message
is not a replacement for that feed. Advance a consumer cursor only after the
receiver acknowledges and deduplicate with the stable change ID. Keep self-made
changes in history even when the host suppresses an automatic self-nudge.

## Later experience review

Experience extraction is separate work: select authorized episodes/edits,
collect stable evidence references, separate explicit instructions from inferred
candidates, inspect current assets/history, verify direct-instruction application,
group proposals by theme, review evidence/counterevidence with qualitative
confidence and rationale, and apply only owner-approved changes using this same
toolkit. This skill does not start a scheduler, select private evidence, or change
existing learning jobs or review screens.

Outside problems or opportunities may be recorded as observations through the Overseer
outside tools without knowing where implementation belongs. Retrieve them only on owner
request; recording does not create tasks or apply changes. Editable preference and plugin
improvements use ordinary proposals.
