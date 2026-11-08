# Cross-session working memory

Working memory is a short, agent-authored handoff, independent of chat history,
model, provider or session ID. Its identity is the registered agent UUID plus the
explicitly selected business (or Global). Tasks, Stories, decisions, Personal
Context and email remain their own authoritative records.

## Tools

- `agent_context_get`: current handoff plus bounded, live attention. Global calls
  return a paginated authorized-business directory, not all business handoffs.
- `working_memory_get`: committed body, revision, whole-file hash and reference
  freshness. `observe:[{type,id}]` returns versioned source tokens. Optional
  `version`, `history:true`/`history_before`, and `legacy_plan:true` retrieve saved
  versions, history metadata and the original imported plan respectively.
- `working_memory_update`: save `body` and `sources:[{type,id,token}]`, with an
  idempotency key. First save needs `create_if_missing:true`; replacements need
  the current `expected_revision` and `expected_content_hash`. Empty text is an
  explicit clear, not deletion. An unchanged body/reference set creates no version.

Use `working_memory_get(metadata_only:true)` for the current revision/hash when
body access is blocked by source revocation or file drift. It does not disclose
references or text. It can be combined with `observe` or history metadata.
Version selection and metadata-only selection should be separate calls.

Save focus, findings, failed approaches, uncertainty, where work stopped and the
next useful check. Target 800–1,600 characters; the enforced body limit is 4,000
characters and at most 20 unique references. Oversized saves fail, never truncate.
These are character limits, not advertised token counts. Observe referenced
records when reading evidence and preserve those tokens in the save. A concurrent
change remains visibly changed; saving never silently marks it reviewed.

Supported references are tasks, Stories, authorized authored documents, signoffs
and email conversations. They must belong to the selected scope. Observation
tokens cover Story timeline sequence, task blockers/configuration, committed
document hashes, signoff status revision and email-message revisions. Child
collections stop at 200; larger sources are explicitly unavailable. References
are evidence, not instructions or authority. Owner-only Global notes and other
agents' migrated private plans are not a way around their source permissions.

## Scope, privacy and persistence

Every interactive role receives core-default access to its own memory. Explicit
denial wins. There is no ambient system-worker access, manager exception or
business-to-Global inheritance. The installation owner can inspect and correct
memory using `agent_id`, including retained memory after an agent is deactivated.
The owner must still select an authorized business.

Memory mutations do not support signoff: they either execute under an allow grant
or fail. A private handoff must not be copied into the shared signoff queue. This
is reflected in the operation catalog, so `signoff` is not a valid override.

Generic document, Material, search and relationship readers do not expose working
memory. Shared inbox events and public operational/Git-history exports exclude
it. The registered file and immutable full-file version snapshots are covered by
private database/authored backups. Hashes cover frontmatter and body, not just the
rendered Markdown. Mutation receipts contain metadata, not echoed private prose.

Every body read checks current scope and evidence access. If any cited evidence
is inaccessible, deleted or unavailable, the whole body is withheld. This is
deliberately conservative: a compact narrative cannot be safely redacted by
guessing which sentences came from a missing source. Metadata-only access permits
an explicit checked replacement without exposing that narrative.

This is application-level isolation, not an operating-system sandbox or automatic
data-loss prevention. The same OS user can inspect files; references cannot prove
that an author cited every source. Never copy credentials into memory. Global
agents' existing own notes are home-scoped: do not use them for business evidence.
For a Global agent, a selected business on `notes_create` is validated and stored
as the immutable UUID `related_business_id` in note metadata while the document
remains privately owned in the agent's Global workspace. Display labels are
resolved dynamically when the note is read. `notes_list`
filters those private notes by the selected attribution; the owner can read them
from Global. Use an authorized same-business record instead for business evidence.
If no such write tool is granted, keep the handoff concise and request an
appropriate owner/coordinator action.

Successful writes use the existing prepared file receipts and CAS checks. A lost
response is retried with the same arguments and idempotency key. A conflicting
session must reread and deliberately reconcile; there is no last-writer-wins
replacement or exclusive session lease. Unsaved conversation state is not durable.

Native file edits are not successful memory saves. Reads fail on hash drift.
The owner can repair existing-file drift by reading metadata, inspecting a known
version and issuing a new update with `repair_observed_hash` for the actual file.
Restoring historical text creates a new version and preserves all earlier versions. Missing files
require owner restoration from the private backup/version evidence before normal
checked writes resume. Never restore an old database over subsequent live work.

## Project notes

A `business_owned_note` document is an ordinary note of one project, not of an agent: its `business_id` and namespace are
that project, `owning_agent_id` is empty, and it lives at `notes/<YYYY-MM-DD>-<slug>.md` in the project HQ (a collision
gets `-2`, `-3`, ...). Metadata holds `title`, `date`, `author_id`, `pinned` and optional `tags`; the ID and path survive
title edits, and ordinary revision/content-hash checks (and Signoff) protect every update and archive. Notes created before
this model keep their paths and IDs.

- There are no categories, no specialist authoring role and no author-only rule. `author_id` is immutable attribution.
  `pinned` is ordinary metadata with no special meaning, and no note is injected into an agent's context.
- `notes_create` with `target_business_id` creates a project note; `tags` label it. Every write (`notes_update`,
  `notes_delete`, `notes_upload`) of such a note names the same `target_business_id`, which must equal the selected project
  and the note's stored project, so a stale selection fails instead of writing elsewhere. Combining it with an agent-note
  selector (`target_agent_id`, `agent_id`) is rejected.
- Writing needs the ordinary notes grant of the operation plus access to the selected project. Project membership alone is not
  a grant, a tag grants nothing, and the same rule applies to every write path (update, upload, delete and the document store).
- `notes_list(target_business_id=..., tag=...)` lists the selected project's notes for any principal with access to it;
  `tag` keeps notes carrying that tag. Plain agent listings still return only the caller's own notes.
- A Global agent's private note with `related_business_id` stays private; tagging or mentioning a project never makes it
  project-owned. Project notes are not automatic Context Base sources; an author may snapshot one with `notes_upload`.
- Legacy `category` metadata is not interpreted at run time. `python -m qorqut.migration.project_note_tags` (dry-run
  unless `--apply`) converts an explicit workspace copy: the old label becomes an ordinary tag, `category` leaves the metadata
  and frontmatter, and ID, author, path and body text are preserved. A file whose canonical metadata changes gets a freshly
  computed content hash, reported as before/after; unchanged bytes keep their hash. It never touches a live database.

## Legacy plans and bootstrap

There is no automatic live import. Before first saving in an agent's home scope,
read `plans_get` and pass `legacy_plan_content_hash`. The operation verifies both
the registered and physical hash and preserves the exact original bytes and
provenance in version one. It then freezes legacy writes. Long plans require an
explicit short handoff or neutral review pointer; no LLM silently summarizes them.
Global plans import into Global only, not every business.

After cutover, own `plans_get` reads the checked home-scope handoff. Teammate reads
do not gain access. `plans_set` returns `plan_migrated` with instructions to use
the new API: the old write signature cannot preserve observed evidence tokens.
The physical `PLAN.md` remains frozen original evidence, not a second current
plan. The original is available explicitly via `working_memory_get(legacy_plan:true)`.

Fresh bootstrap supplies compact working context instead of the old expanded
work briefing. Mandatory rules remain intact; memory is rendered once. Attention
interleaves changed references, signoffs, active tasks and active Stories, at most
10 items, with explicit omission notices. Global directory pages contain at most
20 businesses. The non-memory context has a 4,000-character output budget.
When the combined hook budget is tight, optional working context is omitted with
a tool-read instruction; mandatory rules and runtime bindings are never truncated.

Memory changes do not alter static configuration hashes. Fresh lifecycle events
read current state; ordinary prompt hooks do not replay the whole handoff.
`agent_config_get(response_detail=full)` retains the detailed on-demand briefing.
When switching businesses, read that business's configuration/Agent Briefs before
acting; `agent_context_get` is working state, not a replacement for mandatory rules.
Neither context reads nor memory saves acknowledge inbox events, accept shared
reviews, change assignments or take over another session's watcher.

## Rollout status and gates

Implementation branches are not a deployment. No live plan imports, owner data,
agent restarts, role-pin upgrades or provider calls are part of development tests.
Before rollout, verify the paired dashboard surface and supported hook adapters,
inventory existing plans, preview the selected imports and preserve a private
backup. Roll back code only to a continuity-aware revision that continues freezing
imported plans; a pre-feature service can reopen legacy writers and is not a safe
cutover rollback target.

The companion dashboard adds an owner Working memory tab to the existing agent
pane, including scope selection for Global agents, correction, version inspection
and restore-as-draft. It preserves drafts in page memory, checks revisions and
retries uncertain saves with the same request. File-drift repair remains an
explicit owner recovery operation, not a silent UI overwrite.

Behavioral fresh-session acceptance after approved deployment remains a rollout
gate. Semantic/hybrid search remains an evidence-gated recall
improvement, not a dependency of deterministic startup or durable saves.
