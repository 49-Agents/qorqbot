# Agentic foundation integration verification

This combines the reviewed standalone foundation and the offline importer on
`v1.3/agentic-integration`. It establishes the source and installed-wheel
contracts. Host operation registration, authority, native projection, execution
bridging and inbox/nudge delivery remain a separate Manager-designed assignment.

The module's reviewed core head was
`1819dc13a14226607e2331b90e52ce5f7a907a7a`; execution head was
`b662d01ff671ca97a55046bd58d86fadf30fe02f`, sharing base
`46963bdae925bd1cab65f53fe3fdac31dcee737d`. Ordinary local merge
`49348f71c748416e4c4d5f6a8e35562b7ae45a87` preserves both parents.
The importer started at `b4a02815addfd20a2580eabf403222a5852a956f`.
The verified module head is `43ea62961f61541ff231390adf64acc3a8214d18`.
No final-assembly merge or deployment is part of this result.

## Public contract and checks

Use `AgenticLayer.initialize(root, state)` or `AgenticLayer.open(root, state)`
with explicit separate paths, then `layer.workflows`. The cached public facade
owns the same durable runner used by JSON operations such as `workflows.start`,
`workflows.supply_arguments`, `workflows.complete_step` and `workflows.get`.
The console reads one `{ "operation": "...", "arguments": {...} }` request and
returns an `ok` result/error envelope. See the module's `docs/INTERFACES.md` and
`docs/WORKFLOWS.md` for exact signatures and examples.

The new module integration tests exercise both Python and real JSON subprocesses:
required, confirmed, default and derived inputs; an independent step completing
while another awaits input; declared and verified completion; reopen and exact
idempotent retry; durable `verification_blocked` errors for missing runtime;
subsequent completion with a new operation ID; and actor/selection forwarding
for receipt reads and reconciliation of a genuinely interrupted synthetic call.
The shared Git fixture now distinguishes `AgenticLayer` from `pathlib.Path`,
whose `root` property denotes the filesystem root. Existing CLI tests explicitly
forward the loaded module location to their subprocesses, so a source-only test
environment needs no editable install.

The focused host test imports a complete synthetic source set through
`apply_import_plan`, checks every native byte and every resulting file hash,
reads every catalog entry/resource and revision, reopens the store, verifies one
durable event covering all files and entries before a commit, and confirms an
unchanged rerun emits no extra event. No host service fixture is loaded.

A wheel is built locally and installed into a separate scratch environment.
Tests run outside both checkouts with `PYTHONPATH` cleared and pytest's source
path injection disabled. The actual installed `qorqut-agentic` entry point
initializes, imports the synthetic package, reads its catalog, invokes its
resource-backed custom tool, finishes/reopens workflows, and reports a missing
runtime honestly. The complete host import test also passes against the wheel.
An isolated import audit rejects writes, subprocesses, socket activity and
SQLite connections; importing the module, CLI and runner passes. All three
packaged management resources match source SHA-256 byte-for-byte.
`LocalProcessRuntime` is used only by these explicit synthetic standalone tests.

Verification commands (run from the indicated checkout; `GROUP` is this copied
worktree group, and all environments/temp paths reside under its `scratch`):

```sh
# qorqut-agentic: full standalone suite after integration corrections
TMPDIR="$GROUP/scratch/tmp" "$GROUP/scratch/build-env/bin/python" -m pytest \
  --durations=5 --basetemp="$GROUP/scratch/pytest-standalone-final"

# qorqut-code: focused importer suite, excluding host service fixtures
TMPDIR="$GROUP/scratch/tmp" PYTHONPATH=app:../qorqut-agentic/src \
  "$GROUP/scratch/build-env/bin/python" -m pytest -q \
  --confcutdir=app/tests/agentic_import app/tests/agentic_import \
  --basetemp="$GROUP/scratch/pytest-host"

# qorqut-agentic: local wheel build, no dependency downloads
TMPDIR="$GROUP/scratch/tmp" "$GROUP/scratch/build-env/bin/python" -m pip wheel \
  --no-cache-dir --no-index --no-deps --no-build-isolation . -w "$GROUP/scratch/dist"

# scratch: installed public contract, actual console and cleared source path
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 TMPDIR="$GROUP/scratch/tmp" \
  QORQUT_AGENTIC_TEST_CONSOLE="$GROUP/scratch/wheel-env/bin/qorqut-agentic" \
  "$GROUP/scratch/wheel-env/bin/python" -m pytest -q -o pythonpath= \
  "$GROUP/qorqut-agentic/tests/test_public_execution.py" \
  --basetemp="$GROUP/scratch/pytest-wheel"
```

Dependency installation used locally cached third-party wheels, with no provider
or native-agent requests. Build/install, test and import-audit logs, wheel, audit
script, resource hashes and the compact inventory report remain in group scratch.
The first full suite run exposed only the CLI subprocess module-path fixture
problem (14 failures, 143 passes); the corrected focused CLI suite passed 23 tests.
The final complete standalone run passed **157 tests in 71.54 seconds**.

| Verification | Final result |
| --- | --- |
| Public facade/CLI integration | 9 passed |
| Complete standalone module suite | 157 passed |
| Focused host importer suite | 28 passed |
| Installed-wheel public facade/real console | 9 passed |
| Installed-wheel complete offline importer | 1 passed |
| Isolated installed import audit and management bytes | Passed |
| `git diff --check` in both editable repositories | Passed |

The final wheel is `qorqut_agentic-0.1.0-py3-none-any.whl` (76,354 bytes),
SHA-256 `fe1183558f006ea4f664eaf4389b0735192f1619adb1da918262763bce713970`.
The final build and dependency installation used `--no-index`; package builds
and installations are local only. No host service, permission or bootstrap
implementation was changed.

## Copied-source coverage

The full inventory and dry import plan use only copied roots in this group.
Selection explicitly includes email, terminal-control, content, Paper and Fastmail.
No custom production source was supplied. Custom-source behavior is covered with
synthetic fixtures. Seven clean source repositories yield **117 asset rows**,
**7 packages**, **45 catalog entries**, **12 bindings**, and **124 proposed file
creates** including wrapper manifests. There are **0 errors**, **0 conflicts**
and **3 pending composition diagnostics**. The real plan is incomplete and is
rejected with `incomplete_coverage`, leaving zero authored changes/events.

| Source | Inventoried commit | Asset rows |
| --- | --- | ---: |
| roles | `6fd3176e52a96f8598c44276c44fe7e32dc84cc1` | 33 |
| host | `f2946c62d856ee60418c5125aaa26173c9a30ac5` | 24 |
| email | `b9a926171ab73c5be8c8dfb63a3f3a33a726100c` | 10 |
| content | `d955c7570d754155ec40e8f024129defd6290494` | 25 |
| content-library | `c502ab26968079309d6088396fcd026f5fdca9e5` | 11 |
| Paper | `6cd3b152a4d7120103dae9080c51eade26fb9067` | 9 |
| Fastmail | `292130194a452981e41f9b87e869a423ba31f700` | 5 |

The host commit above includes the focused integration test; this documentation
was added afterward. No inventoried authored asset changed. The report records
explicit source roots, source/plan digests, package and kind counts, selection,
all diagnostics and unresolved source paths, without copying source bodies.

The three unresolved mappings are preserved:

- Host `app/qorqut/agents/context.py`: runtime identity, scope, available
  operations and configuration are needed for composition.
- Host `app/qorqut/bootstrap/email_guidance.py`: the email-management instance
  path must be composed by the future host adapter.
- Paper `qorqut_paper/native.py`: orientation depends on connection preparation
  and caller role. Static guidance is separately retained.

Paper contributes metadata and static guidance without an invented skill;
Fastmail is metadata-only. Content bindings retain the canonical adapted content
instructions and explicitly record their relationship to the source-library
snapshot. Native metadata does not grant permissions. These results do not
claim complete production migration or a functioning host adapter.
