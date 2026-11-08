# Operational history host framework (first checkpoint)

Design: [History](HISTORY.md). Package:
`app/qorqut/operations_history/`; tests: `app/tests/test_operations_history*.py`. The matching trusted row-export library is
`qorqut_operations.history_exports` in `qorqut-operations` (same branch name, based on accepted `f1fa10a`, its own
`docs/HISTORY_EXPORT_CHECKPOINT.md`).

This is code only. Nothing is wired into application startup, the change publisher, `Database` construction, the existing
`qorqut-tables`/`qorqut-history`/nightly/document history code, the schema or any dependency manifest or submodule pin, and no
history repository exists anywhere except in synthetic test directories. It is not a complete H1 port.

## Source and what changed from the donor

Donor `qorqut-v2.2/qorqut-code@df7daa7fddf387a7e91212f5ed831b32c0735f96`, `app/qorqut_app/history/` (read as Git objects):

| Donor | Here | Change |
|---|---|---|
| `_state.py` | `_state.py` | unchanged (atomic private JSON state files) |
| `_git.py` | `_git.py` | same isolated plumbing; adds a `guard` called before every command |
| `_capture.py` | `_capture.py` | same descriptor-relative capture and incomplete handling; adds trusted excluded subtrees and re-applies every exclusion when carrying a saved version forward |
| `__init__.py` (`History`, `export_files`) | `_service.py`, `_tree.py`, `_areas.py` | rows from an `ExportCatalog` instead of the fixed foundation tables; areas from a typed provider instead of "every agent under `agents/{id}`"; sharding from each export spec instead of table-name rules; `realpath()` replaced by `O_NOFOLLOW` descriptor chains; richer status |
| (not wired in the donor) | `_observer.py` | new inert coalescing observer |

## Public surface

`History(database, *, paths, catalog, areas, clock, git_runner=None, git_timeout=300)` is inert: it validates argument types and
touches neither the filesystem, Git nor the database. `paths` is the explicit `InstancePaths`, `catalog` the trusted
`ExportCatalog`, `areas` the required trusted provider and `clock` returns the attempt's ISO-8601 UTC time. Methods:

| Method | Behavior |
|---|---|
| `initialize()` | Explicit, under the lock. Creates `.runtime` (0700) only if absent, builds a bare SHA-1 repository beside its final name (`ref refs/heads/history`, no remote/template/hook), validates it and renames it to `paths.history`. An existing location is validated and never repaired; anything that is not exactly that repository (plain directory or repository, file, symlink, added remote/config include, alternates) is refused untouched as `history_incompatible`/`history_unavailable`. A missing operations root is `history_unavailable`. No snapshot. Interrupted-creation leftovers (`.history-init-*`) are never swept. |
| `validate()` | Read-only, no lock, creates/repairs nothing; the same repository check plus `status()`. |
| `snapshot()` | Blocking. One read unit yields the catalog export and the area selection; it closes; then files and Git. Returns the summary below or raises a `DomainError` after recording it. An unchanged tree makes no commit. |
| `status()` | Never raises on Git or status trouble. |

Locations: the operations root, `.runtime` and the repository are opened with `O_NOFOLLOW` descriptors from the explicit
`InstancePaths` and `paths.history` (which must be `.runtime/history.git`); the lock (`history.lock`), advisory status
(`history-status.json`) and file stat cache (`history-files.json`) live beside the repository in `.runtime`, outside the tracked
tree. Before **every** Git command the guard re-checks that the opened root, runtime and repository directories are still what
their paths name, so a runtime location replaced during an operation is refused even when the replacement links to the real
repository. This is path validation, not protection against a same-user race.

### Areas (the trusted provider)

`areas(uow)` is called inside the export read unit and returns values of exactly four kinds: `GlobalFiles`,
`GlobalAgentFiles(agent_id)`, `BusinessAgentFiles(business, agent_id)` and `BusinessHeadquarters(business)`. They carry exact
registered IDs/slugs; the host resolves them through `InstancePaths` (`files`, `agent_files(agent)`,
`agent_files(agent, business_folder=slug)`, `business_hq(slug)`) after the unit closed. Anything else, an ID/slug that is not one
`SEGMENT` path segment, a duplicate (also case-folded), or overlapping resolved folders fails with `history_areas_invalid`
before any file is captured. Nothing scans folders, reads tags or infers registrations. A resolved folder whose existing path
crosses a symlink is reported as an excluded `symlink` area and none of it is read. Each value may carry
`excluded=("relative/subtree", ...)`: a trusted list of the domain's generated projections beneath that area, each a safe
normalized relative POSIX path. Those subtrees are skipped (`generated_projection`) and are never carried forward.

### Tree

`database/manifest.json` (the export manifest: format, schema identity, coverage, per-table key/columns/structured
schemas/omitted column names, classified exclusions), `database/<table>.jsonl` or `database/<table>/<hex>.jsonl` for sharded
tables (canonical JSON lines in key order; the shard is the declared fixed hash-prefix of the stable key), and the authored
files at their operations-relative paths under `files/`, `agents/{id}/files/`, `businesses/{slug}/hq/` and
`businesses/{slug}/agents/{id}/files/` with exact bytes and the owner-execute bit. No clock value enters any file, so identical
state produces identical bytes. Not captured: Context, generated instructions/provider configuration, native archives and
transcripts, `.runtime`, business `code/`, role/source repositories, unregistered folders and the SQLite file. The donor's name
and header exclusions remain (symlinks, special files, nested Git or bare repositories, virtualenvs, caches, `node_modules`,
`.runtime`, provider configuration, `.env*`, credential and key names, PEM/PuTTY headers); they are heuristics, not a secret
scanner.

### Incomplete capture and carry-forward

An unreadable or concurrently changed expected path keeps its previously committed version and is reported
`unreadable`/`changed_during_capture`; a confirmed deletion (ENOENT) disappears from the new tree and stays in older commits.
When a saved version is carried forward it is re-checked: it must lie inside a currently scanned area and must not be under an
excluded name or excluded subtree, so an exclusion can never bring earlier private content into the new tree. Leaving an area
(for example a deregistered agent) removes its paths from the next tree but not from older commits.

### Summary and status

`snapshot()` returns `changed`, `commit`, `previous_commit`, `tree`, `complete`, `files_complete`, `coverage_complete`,
`retryable` (file capture only), `captured_at`, export `format`/`schema` identity, row counts, `coverage`
(`complete`, bounded `missing` list, `missing_total`), resolved area labels, file counts, bounded `exclusions` and `incomplete`
lists with totals, and `status_recorded`. **`complete` requires both complete file capture and complete table coverage**; an
unclassified table or an incomplete path makes it false while the exported data and files are still committed.

`status()` returns `state` (`uninitialized`, `unavailable`, `ready`, `ok`, `incomplete`, `failed`), the live `commit`,
`last_attempt` (time, outcome, error) separately from `last_complete` (the latest known complete commit and time),
`consecutive_failures`, `last_snapshot`, `error` and an `observation` of the live ref against the commit the status last
recorded (`consistent`, `ref_commit`, `recorded_commit`, `ref_error`, `status_error` and an explanatory `note` when they
differ). Status is advisory: if it cannot be saved after a successful commit, the commit stands, `snapshot()` reports
`status_recorded: false`, and the next `status()` reports the unrecorded ref advance honestly until a later snapshot records
it. A Git, lock, ref-conflict, export-composition or area failure records a failed attempt, preserves `last_complete` and the
last recorded snapshot, leaves the history ref where it was (the compare-and-swap update preserves an unexpected change and
reports `history_conflict`) and never touches Operations rows or repeats domain work.

Error codes: `history_uninitialized`, `history_unavailable`, `history_incompatible`, `history_busy` (retryable),
`history_git_failed` (retryable), `history_conflict` (retryable), `history_areas_invalid`, `history_composition_invalid`,
`history_export_*` (from the export library) and `history_failed` (unexpected, retryable).

Git runs only `init`, `config`, `rev-parse`, `symbolic-ref`, `ls-tree`, `hash-object`, `update-index`, `write-tree`,
`commit-tree` and `update-ref`, with an allowlisted environment (no inherited `GIT_*`, no user/system configuration, no
prompts), `core.hooksPath=/dev/null`, `protocol.allow=never`, a throwaway private index and an exclusive file lock shared by
every process (a second caller gets `history_busy`). It never fetches, pushes, resets, prunes, restores or reads a source
checkout's index.

### Observer (not started anywhere)

`HistoryObserver(snapshot, *, interval=60.0, retry_delay=None, min_gap=None, wake=None, pause=None, clock=None)` with `start()`, `notify_commit()`,
`stop(timeout)` and `state()`. One worker thread waits on a coalesced wake or the bounded periodic interval and then runs the
injected `snapshot` callable (normally `history.snapshot`); the first pass runs promptly at `start()`. `notify_commit()` is an
advisory post-commit hint that never blocks or raises and coalesces; a burst during a snapshot leaves exactly one further pass; a
missed hint is recovered by the periodic pass. Automatic passes keep a monotonic minimum gap (`min_gap`, default: the interval,
so 60 seconds) after each pass finishes: hints during a pass or its cooldown stay coalesced and are served by one pass at the
deadline, and they never bring it forward (`v1.2/perf-20261014-history`). A failed pass is recorded, never raised, and the next pass waits at least the larger of
`min_gap` and `retry_delay` (default: the interval), so there is no tight retry loop. `stop(timeout)` returns an `ObserverStop`
(`stopped`, `snapshot_running`, `pending`, `last_error`): it starts no new pass, lets a running snapshot finish within the
timeout, abandons a merely pending pass, and reports `stopped=False` while the thread is alive. It never closes the database.
The observer is not registered with the existing change publisher or any routine.

**Restart (`v1.2/history-resume`).** The same object can be started again, but only after a `stop` that returned
`stopped=True` (a never-started observer is trivially stopped). `start` raises `RuntimeError` while a worker thread may still
exist: the thread or its snapshot is running, a `stop` timed out (`stopped=False`) and has not since been repeated until it
reported `stopped=True` (even if the thread ended by itself in between), or a `start`/`stop` is in progress. Each start runs
one worker generation with its own stop signal that is created, never cleared, so an earlier thread can never see a newer
generation's state; the default failure delay waits on that generation's signal and is therefore interrupted by its stop. Start
and stop are serialized by a transition lock that only they take (start never waits for it and is refused; a stop waits for it at
most its own `timeout`, then changes nothing and reports `stopped=False`); the state lock the worker needs is never held across
a join, and a pass cannot begin after a stop was requested. A successful restart schedules exactly one initial reconciliation
pass (which also covers anything pending or changed while stopped; hints sent while stopped are ignored), coalesces later hints
as before and can never overlap snapshots, because the previous thread is confirmed ended first. `passes`, `failures`,
`last_error` and the fixed bounded `retry_delay` after a failed pass carry over unchanged; the minimum-gap deadline belongs to one
worker generation, so a restart's initial pass is never delayed by it. `start`, `stop(timeout)`, `state()`,
`notify_commit()` and `ObserverStop` keep their signatures; failure logging stays in the backend only.

## Design decisions

- The trusted excluded-subtree list is carried on each area value (`excluded=`), so "beneath a selected area" is structural;
  `History.__init__` keeps exactly the specified signature plus the donor's `git_timeout`.
- The observer's first pass runs at `start()`; `stop()` abandons a pending (not running) pass because the next start's first pass
  recovers it; the same observer restarts only after a stop that reported `stopped=True` (see Restart above).
- A symlinked area folder is reported as an excluded `symlink` area labelled `kind:identifiers` (for example
  `business_hq:acme`) rather than failing the whole snapshot; a symlinked `.runtime`, repository or operations root is refused.

## Focused checks run

Synthetic temporary instances only, with a recording Git runner; no real data, provider, source repository or system history
ref. Host tests import the sibling Operations worktree of this same branch through `PYTHONPATH` (the host repository does not pin it yet).

- `app/tests/test_operations_history.py` (22): inert construction; explicit private local initialization and refusal of
  directory/plain-repository/file/symlink/remote locations untouched; replaced runtime root refused before and during an
  operation; one read unit shared by rows and areas and closed before Git; global, global-agent, business-agent and business-HQ
  files included and Context/transcripts/generated instructions/provider configuration/runtime/code/roles/unregistered
  folders/other-business paths absent; safe selected rows with the private direct-message row, secrets and nested event data
  absent from every Git object; unchanged snapshot makes no commit; change, execute bit, row change in one shard file and
  confirmed deletion with old commits intact; incomplete coverage for unclassified tables and `complete` only when classified;
  boundary exclusions (nested Git, virtualenvs, caches, env/credential/key names and headers, symlinks, FIFOs, linked business
  folder) and linked agent-folder created later; exact bytes of binary/large/non-UTF-8 files and hostile Git environment/hook
  ineffective; changed-during-capture and unreadable-directory carry-forward and later resolution; stat-cache restored-times
  case; excluded subtrees skipped and not resurrected through an unreadable parent; Git failure with preserved
  last-complete status and accepted rows, compare-and-swap conflict and lock serialization; unsaved advisory status after a
  commit reported then reconciled; composition/area-provider validation failures.
- `app/tests/test_operations_history_observer.py` (16): hints before start, 500-hint burst during a held snapshot gives one
  further pass, periodic recovery with a scripted wake, bounded stop with a running snapshot, failure delay with no tight loop,
  error clearing, refusal of a second start while running, and a real `History` snapshot driven by a post-commit hint. Restart
  (`v1.2/history-resume`): ordinary stop/start with one initial pass and preserved counters; a timed-out stop refuses restart
  (also after the thread ended) until a later stop confirms, and the ended generation runs no further pass; start refused while a
  stop is joining, a second stop neither blocking nor changing anything, and no state-lock stall; concurrent start/stop/hint churn
  with no overlapping snapshot and no unexpected exception; hint coalescing in a restarted generation; counters, last error and
  failure delay across a restart; the default failure delay interrupted by stop in every generation; periodic scan after a
  restart; and a real `History` change made while stopped versioned by the restart's initial pass.
- `app/tests/test_operations_history_installed.py` (opt-in `QORQUT_HISTORY_INSTALLED_CHECK=1`, plus
  `QORQUT_HISTORY_OPERATIONS_SOURCE`): `git archive HEAD` of both repositories into a temporary directory, both wheels built
  (`--no-deps --no-build-isolation`) and installed together with `--no-deps` plus `jsonschema` into a fresh venv, then run with
  `python -I` from an unrelated directory: both packages import from `site-packages`, no source tree on `sys.path`, an
  Operations-only import pulls in no `qorqut` module, and one synthetic snapshot commits exactly the expected files with no
  secret in any object. The host wheel carries the whole existing `qorqut` tree; only the new package and
  `instance_paths` are imported.

Results: 22 + 8 passed and the installed check passed (see the final report for the exact commits). No existing host test and
no full suite was run.
The restart follow-up ran only the observer file (16 passed, repeated five times, against Operations `1ebcd2b` exported to a
temporary directory); the snapshot and installed-composition files were not rerun because nothing they cover changed.

## Remaining H1 work

- Final per-domain export definitions and classifications for every composed table (including engine-owned tables) with owner
  review of secret and private payload fields; until then every real table is "missing coverage" and a real snapshot would be
  `complete=false`.
- The real registration area provider (global agents, business agents, business slugs from the composed registrations) and the
  decision which generated projections each domain excludes.
- Host lifecycle: construct and call `initialize()` explicitly, start/stop the observer in the ordered service owner, and feed
  `notify_commit()` from the post-commit half of the change publisher; surface `status()` in health. None of that exists yet.
- Retained exact history integration (`qorqut-tables`, document versions, nightly maintenance) stays with its present code and
  refs; this framework neither replaces nor touches it. The private-DM history inclusion question remains pending; v1 exclusion
  is the only behavior.
- Event/DB snapshot instants differ from file instants: no atomic restore point is claimed.
