# Agents r1 implementation evidence

Verification: 2026-10-08 UTC (2026-10-07 America/New_York).
Branch: `codex/agents-r1`. Clean original and Manager design revision:
`242a7a9e5c2f6f30e3d5a8f4c12deba9c14ddd9d`.
Final implementation/test SHA: recorded in the delivery update below.
The implementation, tests and this evidence are committed together; the final
SHA reference is then recorded by an evidence-only commit. The resolved delivery
SHA and normal branch-push verification are also sent through ACLA.

Run `649fed24-a7af-40d7-9d41-382ab248483a`; worker
`9675a36b-df99-433e-a119-1384db6667ec`; bound executor conversation
`01a11910-6aff-7cf0-ac41-ae70eb922ab0`; review thread
`ad01dfef-9b16-41c0-8d57-e74ee0a1e433`. The assigned Codex/Astra executor was
preserved. No subagents, internal reviewers, model turns or provider launches.

This is the bounded implementation of AGENT-REGISTRY.md: M01.01–03 and the
initial directory portion of M09.01. It requires fresh independent review.
It does not establish full M01/M09 or any G0–G6 acceptance, resource readiness,
managed execution, capture health or whole-system readiness.

## Changed ownership

- `src/qorqut_core/agents/__init__.py`, `models.py`, `registry.py`, `workspace.py`:
  typed directory values, SQL operations and two concrete workspace jobs.
- `src/qorqut_core/app.py`: fixed assembly, explicit setup of onbash, reserved
  identity validation on open and explicit installation INSERT columns.
- `src/qorqut_core/storage/migrations.py`: migration 3 only. Migrations 1/2 and
  their checksums are unchanged. No storage algorithm was modified.
- `src/qorqut_core/contracts.py`: typed `dependency_unavailable` wait reason.
- `tests/agents/{__init__,support,test_registry,test_workspace,test_agent_flow}.py`:
  19 owning tests, reusing foundation clocks/installations and the real transport
  server/authentication/official SDK helpers.
- `tests/test_foundation.py`: synthetic onbash workspace setup, registry inventory
  and bootstrap-relative counts. The 70 ms busy timeout, 16-call concurrency
  assertion, original fault coverage and import-origin checks remain intact.
- `tests/transports/support.py`, `tests/transports/test_transports.py`: bootstrap
  event expectations and the passive import inventory. Production transports
  remain unchanged; there is no transport SQL or agent business logic.
- This evidence file. No global plans, package metadata/lock, runner, recorder,
  provider adapters, browser or UI implementation changed.

## Public schemas and durable state

The immutable registry now has nine concrete operations: the four accepted
foundation operations plus the following five. HTTP, MCP and the HTTP operator
client discover/use these same application descriptors.

| Operation | Arguments and receipt |
| --- | --- |
| `agents_create` | Required display name and complete `CLIDefaults`; optional complete resources, catalog icon (default circle), exact persona and strict start boolean (default true); shared idempotency key. Returns `{agent, provisioning_job_id}` with `accepted`. Public calls always create VM-kind records. |
| `agents_get` | Typed agent UUID; returns `AgentView`. |
| `agents_list` | Strict limit 1–200 (default 100), optional UUID cursor and recovery epoch. Returns items, next cursor, has-more, epoch and reserved onbash UUID. Canonical UUID keyset ordering; empty pages preserve the supplied cursor. Wrong epochs reject with `target_changed`. This remains a live directory. |
| `agents_update` | UUID, strict bounded expected metadata revision, at least one nonnull display name/icon and key. Changed presentation returns `accepted` and queues `agent_project`; unchanged confirmation is `completed`, without a domain event/job/revision/timestamp change. |
| `agents_status_set` | UUID, strict bounded expected status revision, UTF-8 text and key. The handler checks the status revision before normalized word/character bounds. Returns `completed`; metadata revision is independent. |

Names use Unicode trim and 1–120 characters; duplicates are allowed. All twenty
catalog icons are validated. Persona is exact UTF-8, at most 65,536 bytes; explicit
empty text remains empty. Omission uses the small plain-text seed. Resources
resolve once from committed installation defaults; later default changes do not
rewrite agents or the reserved host configuration.

Agent views include permanent agent/actor/conversation UUIDs, creator/time,
presentation, kind, data-relative workspace path, resolved CLI/resources,
metadata/lifecycle revisions, start intent, provisioning lifecycle, workspace
initialization time, observed projection revision and the independent status
text/revision/update/review/actor fields. Persona seeds are absent from results
and directory responses. Activity and capture health are explicitly `unknown`;
observation/contact/current-run fields are null and needs-user-input is false.

Registration atomically commits the agent, registered agent actor, three stable
UUIDs, configuration, initial `Initializing.` status, two attributed domain
events, concrete provisioning job and immutable operation result. Creation
history records the seed SHA-256 and byte length, not another full seed copy.
Status normalization is Unicode split/single-space join, bounded to 1–100 words
and 4,096 characters. Changes append old/new text and revision with original
actor attribution. Same-text confirmation changes only reviewed-at; replay of
an identical key returns its saved result without refreshing that timestamp.
`events_list` remains the sole permanent journal/pagination path.

Migration 3 adds the agent table and reserved installation pointer, unique actor
and conversation coordinates, a partial one-host index, binding/permanent-ID
constraints and workspace/projection progress. Synthetic previously registered
agent actors do not require corresponding agent rows. There is no empty
conversation-entry subsystem or second status history table.

Explicit `Application.initialize` uses the private registration handler and
immutable `installation_ensure_onbash` / `onbash:v1` receipt. It captures current
defaults, terminal icon, default persona and start=false. Renames, restart,
changed defaults and setup retries retain that identity. Setup queues work but
creates no agent directories and launches nothing. `Application.open` never
migrates or repairs: missing onbash requires explicit setup (`not_ready`), and
an inconsistent reserved pointer/binding is `target_changed`.

## Filesystem ownership, projection and recovery evidence

Both fixed handlers run under the existing installation lifetime/effect gates.
Job/recovery/lifecycle coordinates are checked before effects and SQL
finalization, including rechecking a lease that expired at the publication
boundary. File work is outside SQL. Directory descriptors reject symbolic links
and non-directory ancestors; paths derive from stored UUIDs.

Provisioning uses `agents/.qorqut-agent-stage-<job UUID>` and a versioned
`.qorqut-owner.json` containing installation, agent, creation operation and job
UUIDs. Preparing/complete phases distinguish initial work. All initial files and
directories are fsynced before the atomic complete-marker replacement. Publication
uses Linux `renameat2(RENAME_NOREPLACE)` and parent fsync, with a matching complete
ownership marker required at any existing destination. Unsupported no-replace
capability fails explicitly. No replacing-directory rename fallback exists.

The layout includes the six authored workspace subdirectories, exact persona,
empty memory, runtime and the four architecture history subdirectories. Provider
entrypoints, native homes, credentials, VM disks and services are not created.
A positively owned preparing stage preserves existing regular seed bytes and
rejects links/special files. An interrupted mkdir before its marker remains
uncertain. Missing, malformed, mismatched or unmarked ownership is neither
adopted nor removed. An expired intent with no stage/final evidence is not
silently replayed.

After publication, retries preserve authored edits, intentionally removed seeds,
scratch and data. A missing previously initialized directory stays unresolved.
One deduplicated `agent.workspace_initialized` event and initialization timestamp
record verified publication. Tests cover twelve injected boundaries from stage
mkdir through marker/publication/projection and SQL finalization, plus an abrupt
owned child-process exit after projection replacement before SQL completion.
The child asserts its imported package root before acting.

`agent.json` is a bounded 16 KiB version-1/layout-1 identity/configuration
projection. Both jobs share one writer: committed snapshot, sibling temporary
file, file fsync, atomic replacement of only this generated target, parent fsync,
then observed revision in SQL. A concurrent metadata update leaves the actual
observed older projection revision and existing/new queued projection work.
Reverse execution of older/newer queued jobs still reads the newest committed
row. Projection before initial publication waits with `retry_delay` (5 seconds).

Recovery-review inspection performs no mkdir, file write, fsync, publication,
projection repair or domain finalization. Even matching complete file evidence
retains the recovery hold. Tests trap those filesystem effects, compare retained
files/domain rows/history, and cover absent, staged, published and initialized
states. Ordinary inspection may resume positively owned stages or verify final
publication; it cannot guess ownership from an empty directory or absent files.

After workspace preparation, both VM agents and onbash remain lifecycle
`provisioning`, preserving their start intent. The job remains `waiting` with
`dependency_unavailable`, phase `runtime_integration_required` and bounded
`unsupported_capability` explanation. Its next-attempt time is 60 seconds later,
an earliest explicit inspection time only. Unchanged `Jobs.claim` excludes this
reason; advancing the clock by 1,000 seconds does not claim/replay it. There is
no background worker loop or public dispatch/runtime placeholder.

## Commands and results

All commands ran in this assigned checkout, using private virtualenv/build
outputs and temporary synthetic installations. Source commands:

```sh
/usr/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.lock
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/agents -t tests -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_foundation.py -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_transports.py -v
# After the final publication-fence refinement:
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/agents -t tests -p test_workspace.py -v
.venv/bin/python -m pip check
git diff --check
```

- Agent suite: **19 passed**, 19.154 s. The earlier registry-only run passed 7;
  the initial combined owning run also passed all 19.
- Foundation: **26 passed**, 12.242 s. No original concurrency/fault assertions
  or timeout values were weakened.
- Transports: **15 passed**, 21.874 s, after correcting the three bootstrap-count
  assertions described below.
- Final focused workspace suite: **11 passed**, 10.348 s, including expiration
  immediately before publication. The unrelated owning suites were not repeated
  for this local effect refinement.

Initial transport run: **2 failures and 2 subcase errors** in 15 tests (22.803 s).
Authentication/body-bound checks expected one initial event but observed three;
the HTTP/MCP disconnect subcases expected two events but observed four. All were
the new onbash creation/status events. The fixture now captures its initial
events; rejected requests must preserve that exact list, and disconnect/replay
adds exactly one event. No production adapter correction was necessary. The
historical 70 ms foundation timing sensitivity remains documented by accepted
foundation/transport evidence; it did not recur in this assignment.

Build and isolated installed checks:

```sh
.venv/bin/python -I -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir dist .
/usr/bin/python3.12 -m venv .venv/install-check
.venv/install-check/bin/python -I -m pip install --no-cache-dir -r requirements.lock
.venv/install-check/bin/python -I -m pip install --no-cache-dir --no-deps --force-reinstall dist/qorqut_core-0.1.0-py3-none-any.whl
# Cwd .venv/wheel-tests: test-only copy, no src/product package.
../install-check/bin/python -I -m unittest discover -s tests/agents -t tests -p test_agent_flow.py -v
../install-check/bin/python -I -m unittest discover -s tests -p test_transports.py -k test_imports_are_passive_and_packaged_notice_remains -v
# Back in the checkout:
.venv/install-check/bin/python -I .venv/verify-package.py /home/alp/qorqut-core/worktrees/agents-r1
.venv/install-check/bin/python -I -m pip check
```

Final installed shared agent/HTTP/MCP flow: **1 passed**, 2.694 s. It uses actual
loopback HTTP, official stateless SDK clients, installed console-server startup,
all five operations, cross-transport immutable replay, schema/error/revision
parity, synthetic owner/agent attribution, reserved discovery and honest
accepted/queued/waiting progress. Only explicit fixture calls drive fixed jobs.
Final installed passive imports/NOTICE: **1 passed**, 0.850 s. Imports are audited
against filesystem writes, SQLite opens, processes, sockets and upstream runtime
access. The installed flow and passive check also passed before the final
publication-fence change; the wheel was rebuilt/reinstalled and these two checks
rerun for the changed final implementation.

The private package probe verifies all **39 Python files plus NOTICE** against
source hashes, all **31 pinned distribution versions**, all imported package
origins (including a separate child), nine public input/output schemas, twenty
icon schema keys and the three fixed handlers. Installed imports resolve only
under `.venv/install-check/lib/python3.12/site-packages/qorqut_core`. It also
compares migrations 1/2 against the original Git revision. Both pip checks and
whitespace validation passed. No unrelated whole-package test repetition or
runner/recorder/provider test modification was performed.

Private logs/probe: `.venv/{registry-initial,foundation,agents-source,agents-source-final,workspace-final,transports-source,transports-source-final,agents-wheel-smoke-final,wheel-passive-imports-final}.log`,
`build-final.log`, `wheel-install-final.log`, `package-verification-final.json`
and `verify-package.py`. These are ignored local evidence, not runtime fixtures
or package examples.

## Artifacts, provenance and limits

| Artifact | SHA-256 |
| --- | --- |
| Final wheel `qorqut_core-0.1.0-py3-none-any.whl` | `7d07d34e90084421d9f0cf150fb11cf80fdf99713fb12aa2bf989de0621018ee` |
| Retained `requirements.lock` | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| Retained `pyproject.toml` | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |
| Source and installed `storage/NOTICE` | `c9a80f7b863ca7ca6a1d640a5ad0cb72ca991eec41d862741038ef40a859ef0d` |
| Migration 1 checksum (retained) | `64faf705fa367df07470bb80d59c2363d97d11e8b1958661d86bcfd700479663` |
| Migration 2 checksum (retained) | `5e91778f3e01521f5a19d7d272ee5a15d0ef9432125a34abe9aab6811356db4c` |
| Migration 3 checksum | `18ca408f82d5c231eceb76357c9fd98bd4bc7a3ab5f43b2719550c31209cb0d5` |

Observed Python 3.12.3, SQLite 3.45.1, Pydantic 2.12.3, MCP 1.30.0,
Starlette 1.7.0, Uvicorn 0.54.0, HTTPX 0.28.1, AnyIO 4.15.1 and Linux
6.8.0-138-generic x86_64 with glibc 2.39. The checkout filesystem reports the
ext2/ext3 family. Dependency/package metadata and accepted source notices remain
unchanged. This increment is new implementation of the supplied design using
the accepted core primitives; no additional upstream source adaptation, live
Qorqut configuration, operational data, credentials, native stores or profiles
were imported.

Fixtures remove their temporary trees and credentials, shut down/reap only their
owned server/process children and close their listeners/databases. No owner
session, shared service, VM, network configuration or global setting was changed.
Only ignored task-local virtualenvs, logs, test staging and build/wheel outputs
remain. Product reconciliation intentionally preserves unknown staging evidence
and generated crash orphans; no cleanup scanner was added.

Durability assumes local storage honoring WAL/FULL, file/directory fsync, flock
and Linux atomic no-replace rename. Process crashes and injected faults do not
certify hardware power-loss behavior. The gates serialize cooperating core work;
unrestricted external shell writers are not sandboxed or universally fenced.
Unmarked interrupted staging requires later explicit repair. Presence, resource/
runtime readiness and launch, native credentials, archive/restore integration,
general authored-file APIs, browser/messaging/UI and service-driven job execution
remain later work. No ready/healthy/idle observations are fabricated. No merge,
PR, release or deployment was performed; Manager owns landing and acceptance.
