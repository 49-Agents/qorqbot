# Core package integration r1 evidence

2026-10-07 America/New_York; verification completed 2026-10-08 UTC. Branch:
`codex/core-integration-r1`. Clean starting/Manager revision:
`7d15cb504d01e995f180aecc9b1eadd0fc9056a6`.
Run `649fed24-a7af-40d7-9d41-382ab248483a`; worker
`7a037eb5-987b-4ee5-9ead-a0be7a54db23`; bound executor conversation
`01a118de-518d-78c3-9596-b548dcb457b6`. No delegation or internal reviewers.
The completed candidate requires fresh independent review before Manager lands it.

## Exact integration history and owned files

The five ordinary `--no-ff` merges completed in this order, without conflicts:

| Accepted input | Integration merge |
| --- | --- |
| `d2e251c4cd02be91b95c392d225cf52d25c2958a` | `de20c0f4b0fd5b69da5b80b8674d81c67998d142` |
| `2db2be6d051e83b383e240aed41424a212153107` | `241fd454fea681a2df7402482133cb0a25eee291` |
| `170412438596a98845e2fee8ed3f48ce984b899b` | `255317e71d381b98a2257cea1236be9516395cb2` |
| `08a0273cccf172d8f5a3c0d98fdff93a36531521` | `3cfce73fdce63640777ce2f00126b417fb706732` |
| `44328807c84db01c3351bef74ada285171117d15` | `a50ce83118cb8cb7d88a26cc7baf5c70ab44aff7` |

Each second parent is exactly its accepted input; each first parent is the
previous integration revision. All five inputs and foundation
`ef18e59e2df372963f5fba222156fe6b6e50195f` are verified ancestors. The newer
Manager `AGENTS.md` and planning documents remain byte-identical to the base.
All six accepted evidence documents remain byte-identical to their input SHAs.

Merge inventory: 65 added paths. Transport/foundation contributes package
metadata, root application/config/contracts/entry points, `storage/`,
`transports/`, `tests/test_foundation.py`, `tests/transports/` and two evidence
documents (27 paths). Adapters contributes its subpackage, owning tests/fixtures
and evidence (16); runner its subpackage, owning tests/Receiver and evidence
(13); transcripts its subpackage, owning tests and evidence (8); environments
its evidence document only (1). No browser revision was imported.

Authored integration inventory:

- `tests/test_foundation.py`, `tests/adapters/test_adapters.py`,
  `tests/runner/test_crash_and_lock.py`, `tests/transcripts/support.py`: only the
  required `qorqut_core` import and child import-root expressions changed.
  Roots derive from the loaded package's `__file__`; assertions, timeouts,
  private environments and cleanup remain intact.
- `tests/transports/test_transports.py`: the existing passive-import probe also
  imports the combined adapter, runner and transcript module inventory.
- `tests/integration/__init__.py` and `test_shared_local_db.py`: one scenario,
  parameterized with two initialization orders, reusing `runner.support.Receiver`.
- This evidence document.

No production or package metadata correction was needed. Every production
namespace matches its owning accepted SHA. `pyproject.toml`, `requirements.lock`
and `storage/NOTICE` match the accepted transport input. No shared schemas,
registrations, readiness defaults or algorithms changed. Public operations
remain `config_get`, `config_update`, `operations_get`, `events_list`.

## Shared local file result

Both runner-first and capture-first cases create a fresh private real-tmux
Receiver and use its agent/recovery/environment/boot/run binding for capture,
with separate synthetic installation/source/epoch UUIDs. Both components use
the Receiver's exact `spool.sqlite3` path, verified on their owning connections.
This is a fixture `terminal` source; no recorder/native agent is attached.

Two separate small appends precede one safe terminal input with a stable attempt
ID. The receiver observes one exact bracketed-paste frame and Enter. A control
take persists a nondefault human mode/epoch/viewer. The fixture acknowledges only
the first chunk and reclaims exactly its payload, retaining its coordinate/hash
receipt and the unacknowledged suffix.

After closing/reconstructing both owners, source/run/link identities, offsets,
ACK, chunks, attempt and control receipts remain exact. Retry of the reclaimed
append preserves its receipt without recreating payload; identical terminal
retry preserves the receipt and produces no second paste/submission. Repeated
schema initialization leaves the complete database dump unchanged. Both owning
connections retain WAL, FULL (`2`), foreign keys (`1`), `user_version=0`, their
5,000/2,000 ms busy waits, `integrity_check=ok` and empty foreign-key diagnostics.
The separate foundation host database is never passed to these components.

Receiver readiness/composer callbacks and the host ACK are synthetic fixture
evidence only. Saved capture health is not used as live recorder readiness.

## Exact validation commands and results

Private validation directory (`V` below):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/core-integration-r1-validation`.
Checkout (`R`): `/home/alp/qorqut-core/worktrees/core-integration-r1`.
No installed package from another worktree/environment was used.

```sh
/usr/bin/python3.12 -m venv "$V/source-env"
/usr/bin/python3.12 -m venv "$V/wheel-env"
# Each pip command used PATH=/usr/bin:/bin, a private child HOME=$V/home,
# LC_ALL=C.UTF-8 and PIP_DISABLE_PIP_VERSION_CHECK=1, without inherited variables.
"$V/source-env/bin/python" -m pip install --no-cache-dir -r requirements.lock
"$V/wheel-env/bin/python" -m pip install --no-cache-dir -r requirements.lock
"$V/source-env/bin/python" -I -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir "$V/wheel" .
"$V/wheel-env/bin/python" -I -m pip install --no-cache-dir --no-deps "$V/wheel/qorqut_core-0.1.0-py3-none-any.whl"
```

All test invocations use these exact command prefixes, followed by the discovery
arguments in the table and `-v`. Source cwd is `R`. Wheel cwd is `V/staging`,
containing a byte-identical test-only copy and no `src` or product package.
The owning suites execute in separate Python processes, avoiding collisions
between the two top-level test `support` modules.

```sh
# Source prefix:
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$R/src" /usr/bin/python3.12 "$V/run_owned.py" "$V/source-env/bin/python" -P -m unittest discover
# Wheel prefix:
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3.12 "$V/run_owned.py" "$V/wheel-env/bin/python" -I -m unittest discover
```

| Discovery arguments | Source | Installed wheel |
| --- | --- | --- |
| `-s tests -p test_foundation.py` | 26 passed, 5.093 s | 26 passed, 4.380 s |
| `-s tests -p test_transports.py` | 15 passed, 21.301 s | 15 passed, 22.391 s |
| `-s tests/adapters` | 28 passed, 2.096 s | 28 passed, 2.445 s |
| `-s tests/runner` | 25 passed, 12.543 s | 25 passed, 13.426 s |
| `-s tests/transcripts` | 22 passed, 21.776 s | 22 passed, 23.315 s |
| `-s tests/integration -t tests` | 1 scenario / 2 subcases passed, 1.002 s | 1 scenario / 2 subcases passed, 0.733 s |

Every suite ran once per environment; source integration ran before the other
source suites. All exits were 0, with no failures or skips. No checks were
weakened or retried. The accepted transport report's earlier foundation
observation remains relevant: one of 16 concurrent calls once returned
`storage_blocked` at the fixture's 70 ms timeout; later unchanged checks passed.
It did not recur here. Its test/timeout/assertions remain unchanged; these passes
do not remove the known timing sensitivity.

Additional commands, using the corresponding sanitized environment above:

```sh
"$V/source-env/bin/python" -I -m pip check
"$V/wheel-env/bin/python" -I -m pip check
# Source uses -P with PYTHONPATH=$R/src; wheel uses -I with no PYTHONPATH.
"$V/source-env/bin/python" -P "$V/verify_package.py" "$V/manifest.json" "$R/src/qorqut_core"
"$V/wheel-env/bin/python" -I "$V/verify_package.py" "$V/manifest.json" "$V/wheel-env/lib/python3.12/site-packages/qorqut_core"
# Both interpreters, with the same respective isolation flag:
python -m qorqut_core --help
python -m qorqut_core serve --help
python -m qorqut_core call --help
python -m qorqut_core.transcripts.recorder --help
"$V/wheel-env/bin/qorqut-core" --help
git diff --check
```

All passed. The two inventory probes check every locked distribution version,
every package `.py`/NOTICE file against source SHA-256, and every imported module
path. Each launches a separate child with the loaded package's import root and
verifies the same 35 module paths. Source parent/child resolve under `R/src`;
wheel parent/child resolve under `V/wheel-env/lib/python3.12/site-packages`.
Exactly 35 Python files plus NOTICE match, with no missing or extra source files.
Both environments match all 31 lock entries, including Pydantic 2.12.3 and
pydantic_core 2.41.4. Both `pip check` results report no broken requirements.
The existing audited import test passed for the combined inventory in both
environments, forbidding data writes/SQLite opens/processes/listeners/upstream
runtime access. Installed transport tests also exercise actual console serve/call.

Private logs: `V/logs/{source,wheel}-{foundation,transports,adapters,runner,transcripts,integration}.log`,
`{source,wheel}-inventory.log`, build/install/pip/help logs. `manifest.json`,
`artifact-evidence.json`, result JSON and merge file/ancestry inventories retain
the complete probe outputs. These are private evidence, not packaged fixtures.

## Package, environment and cleanup

| Artifact | SHA-256 |
| --- | --- |
| Combined `qorqut_core-0.1.0-py3-none-any.whl` | `d3b1a86d895839f3be992459a59de8a421eccbc3727125289ca6f7b59a967e2e` |
| Retained `requirements.lock` | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| Retained `pyproject.toml` | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |
| Source and installed `storage/NOTICE` | `c9a80f7b863ca7ca6a1d640a5ad0cb72ca991eec41d862741038ef40a859ef0d` |
| `/usr/bin/tmux` | `034b15c64035f783d43862f2775eb4828f61571ca62c8199796000b97d556ecd` |

Observed Python 3.12.3, SQLite 3.45.1, tmux 3.4, Ubuntu 24.04.5 LTS, Linux
6.8.0-138-generic x86_64. No upstream source adaptation was needed; accepted
provenance and the packaged MIT notice are preserved.

The private `run_owned.py` wraps each command in a temporary process subreaper.
Fixture cleanup retains its existing ownership checks; the wrapper only reaps
its own adopted synthetic descendants. For each environment it reaped 47 after
runner, 1 after transcripts and 4 after the two integration cases. Every command
ended with no remaining child; no fallback kill was needed. Receivers closed
their exact private sessions/sockets/data, tests removed temporary databases,
credentials and PTYs, and server fixtures shut down their loopback listeners.
No shared tmux/process, personal HOME/CODEX_HOME, host installation/service,
VM/network, native account/store, profile or live Qorqut data was changed. Only
private disposable virtualenvs, test staging, logs and wheel/build outputs remain.

This establishes compatible packaging and local table coexistence only. It does
not connect the recorder to tmux/adapters or implement agents/personas, messaging,
managed runtime, host archive/fsync-before-ACK, browser/UI, recovery, native
provider, real VM/systemd/cgroup or G0–G6 acceptance. Cooperative PTY/subreaper
checks do not establish product cgroup supervision or power-loss durability.
Final authored SHA, clean status and normal dedicated-branch push verification
are reported through ACLA. No main merge, PR, release or deployment is included.
