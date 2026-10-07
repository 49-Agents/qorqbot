# Manual creation r1: D36 core creation correction

Implementation evidence, 2026-10-08. Branch `codex/manual-creation-r1`, exact base
`862d989edf0fef0bc6c5100a051e936cb5afbcde`. Scope is only the creation contract in
`docs/TERMINAL-OWNED-SESSIONS.md` ("Immediate implementation: creation contract").
Nothing here shows a Terminal, shell, inbox connection, VM, native-session
observation or native capture. Operations stay 14, handlers five, schema version 5.

The r1 results below are historical for candidate
`020879eca62526652737f5d9dde5f2276903556c`; the latest test-only correction and its
new checks are recorded in the final R2 section.

## What changed

Production (`src/qorqut_core/agents/`):

- `models.py`: `AgentsCreate.cli` is `CLIDefaults | None = None` and `start` is a
  strict bool defaulting to `False`. Both carry `deprecated: true` plus a one-line
  description in the generated JSON schema (served identically by `/api/v1/schema`
  and the MCP tool). `json_schema_extra` is used rather than pydantic's
  `Field(deprecated=...)` so attribute access emits no runtime warning. The
  historical argument shape still validates, so an old `start=true` request reaches
  the `Application.execute` receipt lookup unchanged.
- `registry.py`: `create` refuses a fresh `start=true` as its first statement with
  `invalid_argument` ("Creation never starts a CLI: omit start, then start or resume
  the CLI in Terminal after the agent exists") before any read of defaults or write.
  An absent `cli` snapshots the defaults current inside the registration
  transaction; an explicit legacy `cli` is stored as that metadata snapshot only.
  `_register` lost its `start` parameter: the SQL stores literal `0` and the
  `agent.created` payload carries `start_requested: false`, for ordinary and onbash
  registration alike. The enqueued job is still only `agent_provision`.

Not changed: `Application.execute`, `app.py`, storage/migrations (checksums and
schema), `workspace.py`, locks, dependencies, `AgentView`, projection layout, other
production paths, global plans. Legacy `cli`/`start_requested` remain in
`AgentView`, `agent.created` event payloads and `agent.json` as registration
metadata; historical rows, receipts and event bytes are untouched.

Tests (`tests/agents/` only):

- `support.py`: the shared `create` helper is identity-only (name plus overrides).
  Other suites using it get the same stored cli (current defaults) as before.
- `test_registry.py`: existing grouped tests now use identity-only requests from
  owner and from an agent actor; fresh `start=true` (with and without legacy `cli`,
  from both actors) leaves every table byte-identical and leaves the key unspent;
  strict malformed `start` list gains `None`; omitted, explicit-false, legacy-cli
  and onbash registrations all store `start_requested=0`, with false event payloads
  and only `agent_provision` jobs; absent `cli` takes changed defaults while earlier
  rows keep their snapshot. One added test, below, covers historical replay.
- `test_workspace.py`: the existing no-launch/dependency-hold test no longer asks
  for `start=true`; expects `start_requested` false in the row and `agent.json`.
- `test_agent_flow.py`: the one shared HTTP/official-SDK flow is identity-only,
  asserts the schema marks `cli`/`start` deprecated and requires only
  `idempotency_key`/`display_name`, and adds a fresh `start=true` refusal identical
  over both transports with no extra agent. See "Pre-existing failures" for why its
  provisioning section changed.

## Historical replay evidence (actual old code)

`test_old_code_start_true_receipts_replay_unchanged_after_reopen_and_never_launch`
exports `src` of the pinned base with `git archive` into a temp directory, runs that
old source in a subprocess to initialize a disposable installation and commit two
`agents_create` receipts, then opens the same data root with the current code.
No receipt row is hand-built. The test checks that the imported module path is the
exported old tree and that both old results record `start_requested: true`.

- Explicit `start: true` (old shape): current `Application.execute` returns the
  saved result identical to the old-code output, before and after a reopen. All of
  agents/actors/events/jobs/operations are unchanged by the replay.
- Old omitted-start request (committed as the then-default `true`): the same
  arguments now return `idempotency_conflict` and write nothing; the same arguments
  with the effective `"start": true` replay the original result. Nothing mints a key
  or repeats creation.
- Stored `operations` rows are equal before and after running the old installation's
  three provisioning jobs under `subprocess.Popen`/`socket.socket` guards, which
  never trip. Every job ends `waiting`/`dependency_unavailable`; legacy agents still
  read `start_requested: true` and have `workspace_initialized_at` set.

If the pinned commit is not in a checkout the test skips with a stated reason (it ran,
not skipped, below). It also cannot run in the installed test-only copy, which has no
Git history.

## Checks run

Private temp environment (outside the repository): venv from `requirements.lock`,
`pip check` clean. `pip` reported every package as "Using cached" (zero
"Downloading"); it may still have queried the package index while resolving. No
owner credentials, VM, service, provider or live Qorqut data were used.

| Command (from the worktree, `PYTHONPATH=src`) | Result |
| --- | --- |
| `unittest discover -s tests/agents -t tests` | 20 tests OK (19 existing + 1 added), 42.9 s |
| `unittest discover -s tests/transports -t tests` | 30 tests OK, 89.5 s |
| `unittest discover -s tests/messaging -t tests` | 5 tests OK |
| `unittest discover -s tests -p test_foundation.py` | 26 tests OK |
| `unittest discover -s tests/archive -t tests` | 21 tests, 1 failure, identical to base (below) |

Affected set: `tests/agents` (owner), `tests/transports` (shared HTTP/MCP flow and
the server-driven creation in `test_console`/`test_chat_publication`), `tests/archive`,
`tests/messaging` and `test_foundation` (all create agents through the changed
operation or the changed helper). `tests/adapters`, `conversation`, `runner`,
`transcripts` and `integration` were not re-run. A mutation check (disabling the
refusal on a temp copy of `src`) fails the atomic-create test as intended.

Build and installed flow: `pip wheel --no-deps --no-build-isolation` from a copy of
the worktree, then a second venv with `requirements.lock` plus the wheel
(`pip freeze` equals the lock plus `qorqut-core`; `pip check` clean). A test-only copy
of `tests/` (no `src`) ran the shared flow with `python -I` and no `PYTHONPATH`; its
owned child server is the installed `qorqut-core serve` script:

`python -I -m unittest discover -s tests/agents -t tests -p test_agent_flow.py -v`
-> 1 test OK, 3.9 s. It creates identity-only over HTTP, replays the same key over
MCP, rejects fresh `start=true`, reads `start_requested: false` and watches the
headless worker leave the provisioning job waiting on the dependency.

## Provenance

- Wheel `qorqut_core-0.1.0-py3-none-any.whl`: sha256
  `002fb8f5200ea732e2a0adc629f5a2dd49eb44009cfb9fca0d02605b433c9b81`, built from a copy
  of the worktree whose `src/` and `pyproject.toml` are byte-identical to this commit
  (`diff -r`, excluding bytecode and the build's egg-info). Installed `qorqut_core`
  equals `src/qorqut_core` byte for byte by the same comparison.
- `models.py` sha256 `2d3abbc8493b42a8fb1749e44b3606f61da19bb05f3e33df37a45b58b7124c96`;
  `registry.py` sha256 `80f39dc976d9cf325e986887d5bd0a80344ad464fabd4eb31fba0cb329db0dab`.
- sha256 of `git ls-files -s src pyproject.toml` for this commit:
  `024d2e13c955efefa8671bf41c140d33135dd90cf36575463339bd9eec67691b`.
- Old code for replay is `git archive 862d989edf0fef0bc6c5100a051e936cb5afbcde src`.

## Pre-existing failures at the exact base

A full run of the pristine base export (119 tests) had two failures, neither caused
by this change:

1. `agents.test_agent_flow` assumed no job worker. Since `b51f309` the headless
   server runs a `JobWorker`, so the reserved agent's folder already exists when the
   test lists agents and its explicit in-process job step finds nothing to run. This
   file is owned by this task and the flow is the required shared check, so its
   provisioning section now polls the server's own worker (folder and persona seed
   present, projection revision 2) and drops the unreachable explicit job run.
   Assertions about the dependency hold after restart are kept.
2. `archive.test_inventory.test_legacy_v3_migration_preserves_data_and_prior_checksums`
   expects `PRAGMA user_version` 4 but the schema is 5. It is outside this task and
   still fails identically; it was not edited.

## Limits and open items

- Creation still never reports a shell or VM as ready. The provisioning job keeps its
  `dependency_unavailable`/`unsupported_capability` hold; `workspace_initialized_at`
  and the resource fields behave as before.
- `cli`, `start_requested`, and the projection/event copies remain legacy
  registration metadata, not a running-session observation or launch authorization.
  Their retirement is a later versioned cleanup. `cli` in `AgentView` is still the
  default snapshot even when the caller omitted it.
- A client that retried an old omitted-`start` request after this change gets
  `idempotency_conflict`; a client that sends `start=true` with a new key is refused.
  The module plans `docs/AGENT-REGISTRY.md` (create row) and `docs/modules/01-agents.md`
  (creation paragraph) still describe the earlier required-`cli`/`start=true` shape;
  they were outside this assignment's ownership and were not edited.
- No Terminal API, inbox owner, hook bridge, native observation/capture, VM
  execution target or UI change exists in this branch. The UI is a separate repo.

## R2: synchronize live reads with provisioning

2026-10-08, branch `codex/manual-creation-r2-astra`, starting candidate
`020879eca62526652737f5d9dde5f2276903556c`. Only `tests/agents/test_agent_flow.py`
and this evidence file changed. All 160 other tracked files were byte-checked
against that candidate, including production, dependencies, migrations and plans.

Manager review 164 identified an ordering assumption left in r1: the real worker
can initialize a workspace before the next live read, while the creation receipt
remains immutable. The existing single flow now uses one bounded HTTP polling
helper (the original 20-second deadline and 50-ms polling interval) for onbash
revision 1, the created agent's revision 1, and revision 2 after rename. Onbash is
identified from the initial directory; a fresh directory and fresh agent get are
compared in full with MCP after provisioning, with no intervening mutation.

The receipt explicitly retains `workspace_initialized_at=null` and
`projection_revision=0`; the live record has a non-null timestamp and revision 1.
Every other field is compared exactly using a new expected dictionary, without
altering the receipt. All replay equalities, persona seed, schema, no-start/legacy
metadata, refusal/conflict, attribution, dependency-hold and restart checks remain.

Two new private Python 3.12.3 venvs installed the unchanged `requirements.lock`;
all 31 locked distributions matched exactly (plus pip, and core in the installed
venv), and both `pip check` runs passed. Dependencies came from pip's cache; index
resolution was not claimed offline. Built with `pip wheel --no-deps
--no-build-isolation --no-index` from an owned tracked-file copy, then installed
that wheel with `--no-deps --no-index`. Installed checks used `python -I` from a
tests-only directory with no source tree, and the real `qorqut-core serve` script.

| Focused check | Result |
| --- | --- |
| Source: `PYTHONPATH=src python -m unittest discover -s tests/agents -t tests -p test_agent_flow.py -v` | 1 test OK, 5.215 s |
| Installed: `python -I -m unittest discover -s tests/agents -t tests -p test_agent_flow.py -v` | 1 test OK, 5.135 s |
| Private `check-delayed-response.py`, same unchanged installed flow | 1 test OK, 4.988 s; no failures/errors/skips |

The diagnostic delayed returning only the original first successful create
response until real read-only HTTP polling observed provisioning (27 polls,
0.722 s). It returned the same response object with unchanged body bytes, headers
and status. Receipt revision 0/null and live revision 1/timestamp differed in
exactly those two fields. It neither paused/mocked the worker nor supplied fake
server data or called in-process jobs. The unmodified flow passed under this
schedule, including exact original receipt replay after restart.

Byte/import provenance: all 56 package files match the starting candidate,
worktree source, build copy, wheel contents and installed package. Source imports
resolve to this worktree; installed imports resolve to the new venv. Wheel sha256:
`46f2390e21bad55801ca04d3d726858eaec279a70ee3bc6660024a763267c24a`.
Corrected test sha256 (worktree and tests-only copy):
`3f7eea53573927551058b25f15fddb50fb60b11ca65e25993bd9ef4bc29bf8df`.
Private round bundle `worker-manual-creation-r2-astra-b61d3c6b` retains the commands,
logs, diagnostic/result, source manifest and source/installed provenance JSON.

No unrelated backend suites were repeated for this test-only revision. The r1
limitations and recorded unrelated archive failure remain; this correction adds
no runtime readiness evidence. Tests used owned disposable installations and
loopback servers, which the fixture closed and removed; no provider, owner data,
VM or deployed service was used. Fresh independent review remains required; no
merge, deployment or self-approval is part of this revision.
