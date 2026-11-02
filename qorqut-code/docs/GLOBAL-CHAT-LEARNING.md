# Global chat learning

The existing `learnings.extract` routine now processes saved conversations for
registered Global agents (including Ordubashy). Profiles pinned to
the `learning-applier` role are excluded before transcript reads and again at
the program boundary, including explicit agent selection. These conversations
remain a manual owner/Applier workflow. The exclusion is by registered role ID,
not display name or transcript claims.

## Intake and review

- Only registered `agents/` workspaces and supported saved Claude/Codex JSONL
  sessions are read. No personal CLI history or arbitrary filesystem scan.
- Retired Global agents' saved sessions remain eligible. Global session history
  has no newest-20 cutoff. Unchanged hashes do not consume the per-run session
  allowance, so bounded nightly runs can backfill older sessions incrementally.
- The local reader retains its 8 MiB saved-file bound; the program independently
  enforces 16 MiB per adapter session and 64 MiB of changed sessions per run.
  Existing model budgets, exact evidence validation, extraction passes,
  deduplication, contradiction detection and question/answer reconciliation stay
  in place. A growing or invalid session can still delay a batch; inspect failed
  extraction jobs rather than clearing scan state.
- Global transcript candidates additionally pass claim verification. Individual
  rejected model proposals are diagnostic records, not owner queue rejections.
  Incomplete sources publish no partial candidates and pause after three failed
  attempts per hash.
- Global and business sources, scan state and proposals remain isolated. A
  Global conversation mentioning a business is not implicitly routed there.
- Context stays Global; transcript deltas may target the observed agent, role,
  or Global scope. Text must retain business, audience and occasion qualifiers.
- The same Global Learning cards support evidence inspection, editing, owner
  notes, approval/rejection and reopening before application. The Applier uses
  existing review and revision-bound work tools. Delegated decisions still need
  the owner's explicit current instruction; operational signoffs are unchanged.

## Accepted context

Acceptance freezes the text and owner note into one durable upload and queues
the existing filer, just as for business learning. Global materials, topics,
people and source relationships stay under the Global `context/` library.
The filer preserves the learning intact, creates no tasks, and cannot modify
mandatory shared instructions or another business's records.
There is no new general agent upload permission. Global filing is audited by
the decision history, apply receipts, document provenance and job results; it
does not create a business activity log.

Global Context has a Filed context viewer with typed, paginated materials,
topics, people and sources, including owner retry for failed filing jobs.
Existing failed
acceptance receipts use the same intake recovery path. Already accepted legacy
Global context is not silently backfilled; an explicit repeated acceptance can
start filing. Rejected proposals never file. Delta application remains unchanged.

## Rollout

Merge core, dashboard and Learning Applier role changes together. Drain active
context jobs before switching: their frozen program definitions now allow Global
scope, so definition digests change. Back up SQLite and files using the normal
release procedure. Migration 056 rebuilds the relationship constraint to permit
Global context links, preserving rows, indexes, capture triggers and existing
Global task links. Older binaries are not a schema-safe rollback after migration.
Publish and explicitly adopt Learning Applier 1.6.0 after deploying core; old
pinned role guidance still says Global context does not file automatically.

The existing unified routine already schedules Global; no extra routine or
cursor reset is needed. Run a bounded Global extraction after deployment if an
immediate first batch is desired. Do not automatically approve the backfill.
Keep `retry_failed` off scheduled runs: it deliberately overrides the pause after
three unsuccessful attempts. Verification shares the existing per-job model
budget, so inspect cost and paused-source diagnostics before increasing limits.
The live definition inspected on 2026-09-14 is active at midnight America/New_York,
with three passes, a $1 per-scope cap and a 7200-second timeout. No schedule change
is required for this rollout. Already-completed historical snapshots are not
reprocessed merely because the extractor or prompt was upgraded.

Deterministic tests use temporary databases and synthetic transcripts. The opt-in
`tests/test_global_learning_real_chats.py` additionally runs real saved chats
through DeepSeek, with all writes isolated in pytest's database and private
evidence outside the repository.

Codex pruning uses explicit owner-message events and assistant prose, not
user-role response items (which can contain injected instructions and replayed
history). It excludes known inbox notifications, raw tool payloads, and private
reasoning. This is a deliberately narrower representation than the Claude tool
digests. Unknown event-only formats are not inferred to be owner speech. Common
provider/Fastmail token forms are redacted; this is not a comprehensive secret
detector. Captured chats must still be handled as private data.

Evidence quotes must occur in the cited turns. An alleged owner confirmation
must follow the cited assistant statement in a later turn; an initial request
cannot confirm the response it subsequently receives. This validates provenance
and chronology, not whether the owner's reply semantically confirms every claim.
