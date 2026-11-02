# Learning from human contributions

> **Historical context (v1.3).** The standalone learning engine and its issue bridge are retired by
> `v1.3-legacy-learning-removal-1`. A v1.3 target no longer contains the seven engine tables or accepts preparation that
> writes/reads them; the engine program, issue package/action and enrollment/state operations are predecessor-only.
> The five shared learning tables (candidates, decisions, history, runs and scan state) remain, and this document's
> candidate/contribution history is retained as historical data. Current installation no longer selects the
> `learning-engine` extra or the learning-library source. See `LEARNING-ISSUE-BRIDGE.md` for the retirement boundary.

The nightly business extractor now reads saved business Claude transcripts and
submitted human contributions from the authoritative database:

- Owner notes on Signoff approvals/rejections, and input-request notes and choices.
- Owner-authored task notes and committed Story notes (never drafts).

In v1.2 (G022/G023) extraction is one Global stream: a root `learnings.extract`
routine runs one Global job. It reads registered Global agent transcripts,
excluding Learning Applier chats, and the owner contributions of Global and every
active (unarchived) business. Each contribution keeps its original business,
author, kind, ids, time and hash, and its new candidate, run and scan rows are
Global ones. A business contribution's Global scan key is
`business:<business id>:<kind>:<id>`. Businesses' earlier candidates, decisions,
runs and scan state stay readable history; they are not copied or reset. Global
has its own Learning page, owner decisions, work history and scan state. Accepted Global context enters the same filer in the
Global workspace. The Learning Applier's authorized agenda includes Global deltas.
See [Global chat learning](GLOBAL-CHAT-LEARNING.md) for intake and rollout details.
No business or role membership is inferred from where source code is checked out.

## Evidence and repeated work

Each contribution has a namespaced source key, actor, timestamp, exact evidence
blocks and a frozen context snapshot. The question and choices, exact Signoff
proposal, task details/preceding notes, Story/reply context, or approved recipient
explain a contribution but cannot pass the evidence check as human speech.
Selected choices have a separate `owner_selected` speaker type; observations by
other human executors use `human` and can supply context observations, not owner behavior rules. No choice or bare approval endorses unrelated
claims in the proposal. Legacy imported Signoffs are excluded: their provenance
must not be inferred from an unknown historical decision.

The existing three sequential passes apply to each contribution. New/changed
transcripts and new contributions alternate under the same per-job soft budget
and timeout. Up to 50 database contributions and 100 total completed sources are
processed in a run. Agent-filtered manual extraction remains transcript-only.
Contributions of every scope are ordered together by time, business and ID under
that one bound. A contribution is skipped when its current hash completed in
Global scan state or in its own business's earlier scan state (retry and paused
states keep their meaning); its context is built only when it is selected. A note
migrated from v1 is also matched to its v1 progress through its exact migration
provenance. A Story note whose v1 progress cannot be proven to match is paused for
the later migration's reconciliation instead of being extracted again. Reminder/snooze activity, projection rebuilds and later task edits do not
replay an immutable contribution.

Pruned transcript limits remain unchanged. Structured contribution plus context
is capped at 160,000 characters and deduplication input at 200,000 characters;
oversized input is reported rather than silently truncated. Incomplete/invalid
extraction leaves that source eligible for retry. Empty dialogue needs no model
call. Comparison covers open and accepted/implemented lessons in the same
workspace; duplicates add evidence, contradictions remain review candidates,
and existing review/work decisions are never overwritten.

Candidate reads include the source cores referenced by their evidence. The
Learning page exposes View source for the exact consumed snapshot. Compatibility
fields `session_id` and `transcript_hash` in `learning_scan_state` continue holding
old transcript cursors; new non-transcript entries have a typed prefix, source
kind and contribution hash. No personal transcript store or general document
crawler is involved.

## Release

Apply migrations 032 through 034 using the reviewed maintenance workflow, then
register background workers and deploy matching core/dashboard releases.
Drain existing learning jobs before updating: the main program definition now
supports both business and Global scopes. The `learnings.extract_global` alias
remains registered for historical jobs and explicitly configured old routines. Migration preserves business candidates, decisions,
work history and scan hashes, and enforces Global uniqueness/foreign references.
Migration 032 originally created a separate Global routine. Migration 033 retires
that generated routine when the main `global/evolution-extraction` exists,
copying its Global scheduling cursor into the main routine. Migration 034 then
removes the obsolete definition and its routine run history, as requested by the
owner. It also removes the paused legacy repository-status example, the Global
learn-apply placeholder, and paused per-business email schedules where an active
shared email routine has successful runs for that business. Unreplaced email
schedules and in-flight jobs are protected. Independent job receipts remain. The main
routine's schedule, timezone, pause state and business cursors stay unchanged.
In v1.2 this each-business-plus-Global target is removed: a learning routine
targets Global only, and a saved each-business one is refused until it is changed
to root. A business extraction is refused.

Source changes do not migrate, publish roles, repin profiles or restart the live
service. Global model calls use the explicitly configured Global provider. SQL
backups and root exports preserve Global learning candidates, decisions, history
and extraction provenance. Review does not wake agents or change notification
semantics; responses are consumed on the next learning run.

## Engine-enrolled agents

An owner can enroll a registered agent's saved sessions in the standalone learning engine
(`learnings_engine_enrollment_set`). That agent's transcripts then leave this legacy transcript path: the extraction
program skips them (asking for one by `agent_id` is refused), its own scan cursor gains no row for them, and engine
review items are never offered to its duplicate, supersession or reconciliation comparison. The engine keeps a separate
cursor and keeps each session's attested lifecycle, parser coverage and extraction result apart: a session the installation
has not attested complete is deferred, never parsed or sent to a provider. The attestation is read again on later runs, processed
sessions included, so evidence whose attestation is withdrawn or corrected stops counting. Signoff answers, task and Story notes and every other
non-transcript source keep this processor. Un-enrolling returns the agent to this path. Nothing is enrolled by default,
and without a configured engine adapter the enrollment does nothing. See the
[Learning issue bridge](LEARNING-ISSUE-BRIDGE.md).

## Opt-in provider acceptance

`tests/test_learning_provider.py` uses the real configured DeepSeek client with
synthetic contributions and temporary databases. Enable it explicitly:

```sh
QORQUT_TEST_LEARNING_LIVE=1 QORQUT_LEARNING_ENV_FILE=/path/to/provider.env \
  .venv/bin/python -m pytest -q -s tests/test_learning_provider.py
```

The environment file supplies provider configuration only; no business records
or personal transcripts are loaded. Tests cover three-pass extraction and dedup
for transcripts, task/Story notes, business and Global Signoff answers, Global
selected choices, unchanged-source replay, and accepted-rule duplicates/conflicts.
The executor-observation provider case starts at the normalized contribution
boundary and checks that it cannot become an owner rule, even with a misleading
parent title. Live model assertions are semantic and
may fail on provider/model changes; they are excluded from default test runs.

On 2026-09-10, all eight provider acceptance cases passed against
`api.deepseek.com` using `deepseek-v4-flash` (39 model calls across the final
passing cases). The run exposed and corrected ambiguous Global context scope
instructions; the prompt now states the current workspace's allowed scope
explicitly. Sixteen source/analysis regression tests also passed. These checks
validate synthetic cases, not extraction quality across a production corpus.

## Review decisions are retained, not re-extracted

Learning approvals, rejections, reopens, edits and review notes are saved in
`learning_decisions` (current decision) and `learning_history` (attributed,
timestamped before/after snapshots) for both business and Global candidates.
Backups and scoped exports preserve those records. Work outcomes also retain
history. Reviews are not Signoffs and are never scanned as new extraction
sources; accepted lessons remain comparison inputs for duplicates/conflicts.

## Reconciliation before review

The existing comparison call now receives source timestamps and resource IDs,
compares new candidates with one another and existing lessons, and distinguishes
answers, explicit updates, duplicates and unresolved disagreements. Extraction
preserves suggestions as ideas and temporary instructions as temporary, and
omits a question when its answer is explicit within the same source.

An explicit answer or update within 24 hours can combine unreviewed statements
into one candidate, including across extraction runs. The rolling window avoids
splitting a short question/answer exchange at UTC midnight. Missing/reversed
timestamps, longer gaps, conflicting model classifications and ambiguous chains
cannot silently remove a candidate. Equivalent ideas may combine across
context/delta classification; a recommendation cannot merge into a decision.
These are model-assisted semantic judgments, not guarantees of factual truth.

The answer keeps its own evidence. Earlier statements, their exact quotes and
source references remain as reconciliation context. Existing collapsed questions
stay stored and are hidden only from the review queue. Keep both separately
restores the earlier candidate and records the owner's action. Restored or
previously reviewed candidates cannot be automatically collapsed again. All
existing decisions and notes remain unchanged; a concurrent review aborts stale
reconciliation application. Unresolved new-versus-new conflicts are flagged,
and the dashboard groups related open cards for review. Accepted lessons receive
proposed updates with the earlier statement displayed, never silent replacements.

No historical scan cursors are reset by this release; already-processed sources
are not replayed automatically. Drain learning jobs before deployment because
the program's declared output resources now include learning history. No schema
migration or credential-handling changes are required.
