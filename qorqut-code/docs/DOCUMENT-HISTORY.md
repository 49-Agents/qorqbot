# Document history and business activity

Core keeps content history of authored text and a short list of business activity facts
in the `qorqut-business-docs-versioning`
library (`qbdv_document_versions`, `qbdv_business_events`), in the operational SQLite
database. Files and domain tables remain the authority for current content and state; the
library is the history authority. Its version numbers are an independent sequence and
never replace `documents.revision` or a domain CAS revision.

The adapter is `qorqut/storage/history.py`; nothing else
writes the `qbdv_*` tables. The Context Base keeps its own history in its own database;
see `qorqut-context/docs/HISTORY.md`.

The owner's [file viewer](FILE-VIEWER.md) is a second reader of the same chains: it calls
`document_history_get` for a managed document's saved versions, compares two versions, and opens a
saved version by `document_id` and `version`. It writes no history, and a path link for a file that
has no registered document shows only the current file.

## Coverage

| Family | Writers | Capture point | Reader |
|---|---|---|---|
| Registered UTF-8 files: context briefs and founding, legacy pillars, shared instructions, plans, agent/business notes, Global/owner journals, idea drafts, legacy context topics/notes/people/content, editable context uploads and filed dumps, accepted context maps | Prepared operations through `DocumentStore` | `ApplyReceipts.finish` → `history.capture_receipt`, in the finalising transaction after file verification | `document_history_get` (`source_kind=document`) |
| Same, created outside `DocumentStore` | `register_context_sources`, the initial agent plan, agent plan registration, idea-to-business founding documents, platform-reference migration (record-only lifecycle) | `history.seed_document` / `history.document_changed` in the writer's transaction; bytes are hash-checked against the registered row | same |
| Working memory | `working_memory_update`, receipt recovery, offline single-agent conversion | `history.capture_working_memory` (sole writer for this kind) plus the immutable `working_memory_provenance` row | `working_memory_get` only |
| Stories (title, purpose, situation, state_reason, next_step), story drafts (title, body, statuses_json), learning candidates (candidate_json, agent_note), learning decisions (feedback, changes_json) | Prepared operations and background learning output (`learnings_record_batch`), all through `apply_records` | `history.record_applied` in the same transaction as the insert/update/delete | `document_history_get` (`story`, `story_draft`, `learning_candidate`, `learning_decision`) |
| Context Base objects and home briefs | Context Base mutators (host filing, direct edits, seed, wrap, merge, retract) | `qorqut-context` adapter in the context database | `document_history_get` (`context_object`, `context_brief`) |

History IDs: registered files use namespace `business:<uuid>` or `global` and the
`documents.id` (stable across moves); SQL documents use `story:<id>`, `story-draft:<id>`,
`learning-candidate:<id>`, `learning-decision:<candidate_id>`. A SQL document is stored as
deterministic JSON of those fields only, so revision/timestamp/counter changes create no
version. A file version is the exact committed bytes, frontmatter included; a change that
rewrites no file (archive, record-only metadata) creates no version. An unchanged snapshot
is never saved twice.

Authorship is the plan's initiating principal. Approving a Signoff does not make the
reviewer the author; delegated initiator and proposer are kept in event data. Bypass
writers with no human author use `system:<operation>`.

### Before and outside tracked edits

- A new document gets version 1. The first tracked edit of an older document first saves
  its pre-edit text as a `system:history-baseline` version when the frozen plan preview
  hashes to the verified before bytes; otherwise the history says it starts at this edit.
- An owner edit that explicitly accepts an observed out-of-band hash saves that observed
  text as a `system:external-content` checkpoint first. Drift is never attributed to the
  previous author, and there is no watcher or read-time scan.
- Nothing is recorded at proposal/prepare time, for a rejected Signoff, after a failed or
  partial file application, or before finalisation. A replayed or recovered receipt saves
  equal content once and reuses deterministic event IDs.
- Moves keep one chain. Archive, move and deletion never delete history or save an empty
  version; a deleted story draft keeps its history, readable only in the scope that owned it.

### Working memory

Migration 100 imports every existing `working_memory_versions` row through
`import_document_version` (same version numbers, authors, timestamps and bytes) and copies
its provenance to `working_memory_provenance`. It then checks every registered working
memory against its chain (`history.verify_working_memory`). The check requires:

- the document is still `working_memory` in the same business scope as its chain;
- versions run 1..n with content, author and hash matching provenance, and imported rows
  keep their original time and every provenance field exactly;
- the latest version is the registered revision and content hash.

The one lifecycle-only exception is an archived memory. Offline conversion archives a retired
agent's memory by incrementing its revision without new content, so its latest version may
be older than the revision, but it must still hold the registered content. A receipt that
is still applying has its last committed registry state and passes as is. Any other
mismatch aborts the whole migration with `migration_conflict`, listing the problems:

- a memory with no chain, or a missing latest committed version;
- a wrong scope or kind, a gap;
- a hash, author or time mismatch, or inexact provenance.

Nothing is repaired or renumbered. The legacy table and its rows stay as recovery
evidence; new versions are no longer added to it. The public memory version is still the
document revision. A receipt frozen before the upgrade that still inserts a legacy row is
finalised from that row. `working_memory_get`, its source-visibility checks and the
repair flow are unchanged; the generic reader refuses working memory.

### Business activity

Business-scoped facts only; a Global action records none, and an unknown time or actor is
never invented. Event IDs derive from the source row or document revision.

| Kind | Source |
|---|---|
| `task.completed`, `task.reopened` | A committed task crossing into or out of the business's configured closed states (tasks operations and setup reconciliation) |
| `action.recorded` | New `action_logs` rows |
| `document.created/updated/moved/archived/unarchived/deleted` | Registered business documents |
| `story_draft.deleted` etc. | Deleted SQL documents |
| `story.event_recorded`, `learning.changed` | New `story_events`, `learning_history` rows |

Events carry IDs, kinds, revisions and history version numbers, not document text.
`task_completions` semantics and existing readers are unchanged. Context Base activity is
in the context database (`context.*` kinds) with its own sequence; the two are never merged
or replicated. It includes businesses an object has just left, and each (re)creation of a
context ref (see `qorqut-context/docs/HISTORY.md`).

## Reads

Both operations are installation-owner only (a business owner who is not the installation
owner is refused), dashboard-exposed and read-only.

- `document_history_get` — `source_kind`, `source_id` and exactly one of: `version`;
  `from_version` + `to_version` (unified diff); or a metadata page (`before_version`,
  `limit` 1..200, default 50) without contents. Business or Global scope. The source is
  first resolved through its owning domain (`DocumentStore.get`, the scoped repository, or
  the Context Base's canonical scope). `current_revision` is the domain's; `history_version`
  is the library's. In a business scope a merged or removed context ref is not resolved
  into another object; its retained history is readable from Global.
- `business_history_list` — the selected business, `source` `core` (default) or `context`,
  `before_sequence`, `limit`.

HTTP: `/api/root/operations/document_history_get`,
`/api/biz/<slug>/operations/{document_history_get,business_history_list}`.

## Exclusions

| Not versioned here | Reason |
|---|---|
| Source email messages (`email_message`), Context Base raw upload source text | Immutable originals; comments and timeline contributions are append-only |
| Binary assets and data uploads (`asset`, `data_upload`), non-UTF-8 files | Not text documents |
| Generated agent configuration, caches, public exports | Projections of other authority |
| Source-code and docs repositories | Git |
| Business Tables (`table_versions`), finance, email, approval and execution receipts | Their own specialised history, Git writers and audit |
| `qorqut-learning` | Historical experimental backend, retired with the v1.3 learning-engine schema/extra; not installed or versioned as a live writer |

Exclusion is by exact kind (`UNTRACKED_KINDS` in the adapter), never by a pattern in the
kind name. Context uploads and dumps are editable through `context_document_update`, so
they are tracked like other authored text, as are accepted model-generated context maps
(attributed to the filing job's worker principal) and legacy pillars.

Existing applied receipts and previews, Git history, backups, `learning_history` and
Context Base attribute/change history are kept as they are. No complete earlier
chronology is reconstructed from them; history starts at an explicit baseline, except
working memory, which is imported in full.

### Private data

`qbdv_*` and `working_memory_provenance` are not in the public export lists and are never
written to Git. Full SQLite backups include them; snapshot validation counts them.

## Existing databases: baseline

```bash
python -m qorqut.maintenance.history_baseline --root <businesses-root> \
    [--database <state.sqlite3>] [--context-dir <root>/.contextbase]          # preview
python -m qorqut.maintenance.history_baseline --root <businesses-root> ... --apply
```

Preview reads only and reports counts and IDs (no bodies). Apply saves one
`system:history-baseline` version ("earlier edits were not recorded", with the document
revision) of each ready, hash-matching text document with no history, one document per
transaction, rechecking revision, hash and availability; baselines untracked SQL documents;
imports existing `action_logs`, `story_events` and `learning_history`
facts that have a real actor and timezone-aware time under the live event IDs; and reports
working-memory consistency with the same check as migration 100 (a problem list, never a
silent clean result). Busy, missing, drifted, unreadable and non-text documents are
listed, never written. Task completions are not imported (`task_completions` has no actor
or revision identity). With `--context-dir` it runs the Context Base's own
`migrate_history`. Repeating it adds nothing.

## Rollout

Nothing here runs automatically beyond the schema migration. The order for a release:

1. Build the release as usual. With the `--no-deps` recipe, install the pinned
   dependencies explicitly first:
   `pip install "qorqut-business-docs-versioning @ <pinned source from pyproject.toml>"`
   and, when the Context Base is enabled, the pinned `qorqut-contextbase[evolution]`
   (see `pyproject.toml`). Then install the release and run `pip check`. All three need
   Python >= 3.12.
2. Stop writers and back up. Run the schema migration (`Database.initialize`, migration
   100 on top of schema 099). Startup (`open_existing`) refuses a database without the history tables and never
   creates them.
3. Let existing receipt recovery finish pending applications.
4. Under maintenance, run the baseline preview, reconcile what it reports, then `--apply`.
   The Context Base can also be upgraded on its own:
   `python -m contextbase --data-dir <root>/.contextbase migrate-history [--apply]`. Until
   then its history reads and captured writes fail with `migration_required`.
5. Verify: a second preview shows nothing ready, `working_memory` reports no problems, and
   an owner `document_history_get` returns versions for a sample document.
6. Resume writers.
