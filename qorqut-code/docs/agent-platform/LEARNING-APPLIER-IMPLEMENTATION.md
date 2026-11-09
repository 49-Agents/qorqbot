# Learning Applier and Context Library

> **Historical implementation record.** The first-party `learning-engine` extra and learning-library installation input
> are removed in v1.3. Role/profile adoption for the offline learning-removal transition is a reviewed preparation step
> with authoritative persisted `roles_publish`/`agents_update` evidence; it is not performed by the setup commands
> described here. A v1.3 target must not contain or call the retired seven engine tables or `learnings.engine_extract`;
> use `qorqut.bootstrap.learning_removal` instead. This file remains for predecessor context only.

This guide describes the implementation and installation of the Learning Applier and
the context library.

The isolated core branch includes main through `1ae18c0` and dashboard through
`5675d92`, preserving the concurrent Notes and filer changes. Role sources are
based on `915cd3f`, template on `eb13150`, and root context on `328b426`.

## Standalone role

`agent-roles/roles/learning-applier/role.yaml` selects a single role and
its learning-application skill/reference. No plugin or Ordubashy dependency.
New profiles use ecosystem scope with explicit allowed business IDs and these
global capabilities: read_global_context, manage_shared_instructions,
publish_roles and publish_plugins. A profile is prepared under
`<businesses-root>/agents/learning-applier`, outside the business dashboard roster.
The owner subsequently selected the Global dashboard's Agents tab as its entry
point. Global → Agents lists registered active root profiles and opens the same
tmux shell mirror used by business agents. The owner types `claude` to begin or
`claude --continue` to resume. Selecting an agent never starts Claude automatically.
The prepared folder and global role remain the same. No application routine,
business-agent migration, or role publication is needed for this entry point.

`GET /api/root/agents` delegates to `agents_list` with explicit `scope: global`;
ordinary calls still require a selected business. Global reads require the
registered `read_global_context` capability and cannot borrow a business context.
Root terminal list, stream, input, resize, start and stop routes require the
current installation owner. Terminal identity is bound to the registered root
profile and workspace, separate from every business session even for equal names.
They use no business environment. Exact-session transcript saving is enabled by
default for global and business agents in each agent's own `sessions/` folder;
global transcript processing remains out of scope. Streams
recheck owner/profile access and navigation disconnects only the mirror.

The role grants learning work, context browse/search/read, own notes/plan,
configuration/catalog inspection and signoff reads. Shared instruction changes
and package publication require the existing signoff flow. Other tools default
to deny. Generated work orientation is based on available operations. The generic
ecosystem scope module has matching source changes in template and root worktrees
to remove task-coordination advice. Install root changes through the registered
instruction update, preserving the database's revision and hash authority.

The owner chooses work from the proposed agenda or supplies an ad hoc change.
Start selected deltas before recording and maintain agent notes during work. The
Applier reviews and records authorized outcomes; it never builds, tests, commits
or pushes software, design, landing, visual, script, test, CI or IaC changes.
That implementation belongs to the responsible human developer (for the engine and
plugin repositories), and the Applier records its outcome from the evidence the developer
supplies after checking the pushed commit itself with read-only Git. A permitted
noncode context or instruction change keeps its bounded path in the procedure: a
worktree and branch per repository, validation, commit and push. `implemented` means
those pushes succeeded, or existing implementation was explicitly verified as
already covering the change. Version publication is an owner-guided wrap-up step.
Existing-agent adoption is outside this change.

## Learning operations

| Operation | Scope and result |
|---|---|
| learnings_work_list | Selected business, or all permitted businesses for an unselected global agent; defaults to accepted/in-progress deltas |
| learnings_work_get | Explicit source business and learning_id; item, notes, targets, bounded history |
| learnings_work_start | accepted → in-progress |
| learnings_work_defer | in-progress → accepted, retaining notes and targets |
| learnings_work_done | in-progress → implemented; outcome changed or already-covered; nonempty final agent_note required |
| learnings_work_drop | in-progress → rejected-in-work, requiring an agent note |
| learnings_work_note_set | Update agent note/partial targets while accepted or in-progress |
| learnings_issue_action | ignore, snooze, revisit or note for one engine-linked issue; see [Engine issues](#engine-issues) |

Each of the five work mutations also takes the owner's optional `reasoning` (exact words, never asked for or
invented), with `owner_instruction` when an agent supplies it; it is recorded in the same transaction, in the
candidate's append-only history and, for an engine-linked item, as the matching `work_*` issue action. See
[Owner reasoning for work](../LEARNING-ISSUE-BRIDGE.md#owner-reasoning-for-work).

Mutations require expected_revision and idempotency_key. The candidate revision
and same-transaction append-only history prevent competing sessions from taking
the same revision. Owner review and work outcome remain separate in storage;
the UI receives one effective state. Owner-note-only updates preserve active or
terminal work. Owner content edits and review reverts are blocked during active
or terminal work. Deferred accepted work may be reverted; history and agent notes
survive. Omitted feedback preserves the owner note; explicit empty feedback clears it.

Completion requires a new, non-whitespace `agent_note` in the same call, even
when an earlier work note exists. The final summary replaces the current note
atomically with the state and outcome; previous notes remain in history. Include
what applied or was already covered, approval outcome, commits, and anything
still pending. Existing completed records are not rewritten.

Completion targets contain repository, paths, commit SHA, remote, ref and pushed.
All targets must say pushed=true for a changed completion, and at least one is
required. Already-covered needs a reason. Targets are capped at 12,000 serialized
bytes; group long path lists by changed directory. MCP records the agent's
attestation; the skill requires Git verification before reporting success.

The normal Learning queue hides implemented items and context with a committed
handoff. Work history pagination returns compact previews for large historical
values, marked truncated and hash-bound; full before/after snapshots remain in
SQLite and business exports. Extraction-run transcript cores are not loaded by
work_get. Large current items return item_complete=false and next_content_cursor;
pass that token as content_cursor to read bounded JSON chunks of the complete
item. Those cursors reject candidate changes. Historical extraction evidence is
retained unchanged. Agenda summaries are projected in SQLite, avoiding loading
full evidence and variants just to choose work.

## Engine issues

Recurring claims found by the standalone learning engine can enter this same queue as review items linked to a stable
issue. The Learning Applier keeps the owner-led flow: it reads an item with `learnings_review_get` (by `learning_id`
or `issue_id`), records exact per-item accept or reject decisions with `learnings_decide`, and records `ignore`,
`snooze`, `revisit` or `note` with `learnings_issue_action`, each bound to the issue's `expected_revision`, the
item's exact `candidate_id` and `candidate_revision`, and the current `owner_instruction`. The owner's `reasoning` is
stored with its author and source and is never asked for or invented; a recommendation goes in `agent_rationale`.
A revisit does not undo an acceptance, filing or work outcome: for a decided issue it leaves the old item as it is and
makes one linked successor for a new review. Accepted delta work keeps the start, defer, done and drop semantics above,
with the owner's optional work `reasoning` recorded atomically with each action. An issue that reappears after its
agent's source was enrolled again is held when it may be a decided or held issue seen again, and reaches review only
through an explicit `revisit` naming the exact predecessor issue IDs; a hold that lists more predecessors than one revisit
can name is marked incomplete (`hold_reconciliation`) and stays held.
Migration 103 adds the issue tables and the program is unavailable until an installation configures an engine adapter,
so an existing installation changes only when the owner enrolls an agent. The design, recovery limits and focused
checks are in the [Learning issue bridge](../LEARNING-ISSUE-BRIDGE.md).

## Accepted context and recovery

Routing uses bucket, independently of fact/decision/rule/idea/question tags.
Accepted context prepares one registered upload, the existing context.file job,
the owner decision, learning association and audit event in the durable file
protocol. The learning-v1 input keeps tag, effective text, owner correction and
source together as one indivisible block. This intake cannot create tasks.
After filing, the source learning and association remain; further corrections
belong in Context. Context handles provider failures and existing retry behavior.

Repeat acceptance returns the existing association, even with another key. If a
file application partially fails, the learning remains visible and its resource
claim identifies the original receipt. A second acceptance cannot create another
intake. The owner can use the Learning card's Resume handoff control, including
for previously accepted catch-up/reclassified context, backed by:

- GET `/api/biz/{slug}/learnings/{id}/intake`
- POST `/api/biz/{slug}/learnings/{id}/intake/retry` with `{}`

The retry authorizes the owner, then the shared executor reconstructs the original
principal and frozen invocation and revalidates current authority/bytes. It does
not rebind the original plan to the retrying user. Failed partial receipts are
recoverable; conflicting external bytes remain conflicts. Claims release only
after all files are verified unchanged or the original application completes.

Preexisting accepted context does not file during migration or startup. Preview
it with learnings_list filtered by bucket=context/state=accepted; explicitly
review selected items as accepted at their current revisions to enqueue them.
That action preserves the existing owner note unless feedback is supplied.

## General context tools

The trusted MCP invocation source selects compact projections. Dashboard graph,
file and map views and worker reads keep their existing full-document behavior.
An agent cannot select a legacy projection argument to bypass the bounds.

| Tool | Inputs and output |
|---|---|
| context_list | Optional scope/kind/topic_id/person_id/tree, limit/cursor; metadata and short summaries, no bodies |
| context_search | query plus filters; ranked excerpts, next_cursor, refresh_cursor, complete, reasons and coverage |
| context_get | document_id, optional heading or start_line/end_line, max_chars/cursor; bounded body and hash-bound continuation |

Choose scope business, shared or all. The default is business when one is
selected and shared for a global agent without a selected business. Shared reads
require read_global_context. Business reads always select one permitted business.
Private Notes, agent plans/notes, raw email messages, arbitrary paths and
transcripts are excluded. Submitted/filed context snapshots are library sources.

Search uses a disposable SQLite FTS5 cache at
`<authority-database-directory>/context-search.sqlite3`. Managed writes refresh
changed sources opportunistically. Cache failures do not block authority commits.
Search reconciles registered sources in bounded passes: at most 2,000 file stat
checks, 64 changed-source rebuilds and 8 MB of text per pass. It checks nonmatching
sources too, so an external edit can become a new match. Changed files and
returned excerpts are hashed; unchanged misses need only stat checks. Drift from
registered hashes is marked stale and never adopted into authority silently.

Follow refresh_cursor with the same query/filters until coverage completes;
next_cursor pages results for that generation. Empty incomplete results do not
prove absence. Cursor scope/query/registry/generation changes require restarting
the query. A complete refresh is a pass over files, not an atomic filesystem
snapshot. Title matches rank first, with stable ID ordering; there is no vector
service. Narrow queries exceeding 2,000 matches. List/search responses have
serialized-byte budgets; reads return up to 12,000 characters / 24,000 body bytes
and explicit continuation, including within one long line.

The search cache is derived and excluded from authority exports/backups. On a
restore or new installation, missing/stale cache entries rebuild through the
same search reconciliation. An unusable cache reports context_index_unavailable;
repair/recreate this derived file during controlled maintenance. Authoritative
context files and registry records must remain intact.

## Installation boundary

After review and deployment of compatible core/dashboard sources:

1. Back up and migrate the existing database using the installation's maintenance
   procedure. Migration 009 adds candidate work fields and learning_history; it
   neither runs learnings nor creates an agent. Recheck migration numbering
   against concurrent main changes before integration.
2. Publish the explicit learning-applier role from registered package sources.
   Apply the reviewed ecosystem scope instruction through the registered
   shared_instructions_update operation with its current revision/hash.
3. Obtain the installation owner's current invocation context in an authorized
   review business and the published role ref. Call
   `qorqut.bootstrap.learning_applier.prepare_learning_applier(runtime,
   owner_context, role_ref=..., allowed_business_ids=[...], endpoint=...,
   claude_version=...)`. This uses existing profile creation and preparation.
   It creates one root profile, or prepares an exactly matching existing profile;
   differing pins, scope or overrides require explicit configuration review.
4. Inspect ready=true, generated role skill, mandatory context and MCP tools.
   The preparation helper does not probe or launch Claude.

The owner then runs a live Claude session from the prepared folder, asks for an
agenda, selects a delta and checks the guided workflow. Model compliance and a
real external repository push from that session are owner acceptance checks.

## Validation record

Completed in isolated temporary roots, without Claude sessions. Groups overlap;
their counts should not be added together as a unique-test total.

- Broad core regression, excluding the separately tested agent-configuration
  module: 647 passed, with two outdated expectations for MCP exposure and the
  role digest. Both were corrected and passed on fresh runs below. This broad
  run began before the final source edits and concurrent main integration.
- Existing document/agent-configuration/work group: 57 passed, including all
  42 agent-configuration cases.
- Focused work/context/MCP/preparation/package group: 24 passed, including
  competing starts, revision-bound large-item reads and derived-cache failure.
- Final group after integrating core `1ae18c0`: 96 passed across background
  filing/learning analysis, intake, documents/requests, context retrieval,
  foundation metadata, packages, standalone preparation/MCP, and storage upgrade.
  This includes both corrected expectations from the broad run.
- Dashboard Learning checks: 103/103 passed. The pending-intake control also
  covers accepted catch-up items and protects their frozen owner note.
- Real browser/service Learning test passed: effective states, distinct notes,
  owner-note edits during work, valid reverts and durable context acceptance.
- Shared dashboard browser suite: all 15 scenarios passed with the integrated
  sources, including the graph, uploads, signoffs, notes conflicts and mobile UI.
- Notes Python integration: 5 passed. Notes browser passed multiline/status
  preservation, resubmission, Context navigation and migrated Notes behavior.
  Its test now expects the latest queued-job status and explicitly preserves
  the selected note when reopening the pane before navigating home.
- Python compilation and Git whitespace/conflict checks passed.

The standalone MCP test prepares one root agent twice without duplication, reads
an agenda across two explicitly authorized businesses (including identical local
learning IDs), checks pagination and business selection, completes one delta
without affecting the other, excludes private Notes from context search, and
verifies unavailable-tool discovery and invocation denial. Migration from schema
8 preserves existing decisions/evidence, creates no jobs or synthetic history,
and exports new work state and history from a saved database.

Test logs are retained under `/tmp/learning-*.log`. The isolated package-source
map and pinned fixtures live beside the repository worktrees; they are test
inputs, not live package registration. These automated tests performed no live
preparation, migration, deployment, model probe, external repository push or Claude
session.
