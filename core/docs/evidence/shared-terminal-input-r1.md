# Shared recorded-terminal input r1

Implementation of the fixed D44 plan in `docs/SHARED-TERMINAL-INPUT.md`, on
`codex/build-shared-terminal-input-r1` from
`0fc5ffbc71cc0036f81967e34e9b670846e1fc6e`. The final task commit, clean status
and branch push are reported through ACLA; this evidence is included in that
commit. Manager review and integration remain separate.

## Delivered boundary

`TerminalRunner.register_binding(binding, *, terminal_id=None)` preserves
standalone registration and optionally saves the exact existing recorded-shell
mapping. Local `runner_binding_terminals` records the run, terminal and saved
shell spec hash in the same transaction as the run binding. Update, delete and
replacement of mappings are refused. Multiple historical native runs can refer
to the same terminal. Registration validates saved spec/agent/recovery/environment,
boot/socket/tmux/recorder identity, non-stopped state and existing shell control.
A conflicting registration, a prior standalone binding, or inconsistent saved
records fail with a typed error without changing control.

Mapped input, inspections, registration and attempt recovery acquire the
recorded terminal's existing `input_lock(database, terminal_id)`. Mapping reads
are revalidated under the gate. A concurrent initial registration cannot cause
input or recovery to proceed under a stale run-ID gate; competing first
registrations also recheck under their atomic database write. Standalone effects
retain their run-ID gate and existing nonblocking saved-state observations.

Mapped control comes directly from `runner_shell_control`, including the exact
epoch, viewer and sequence. `unclaimed` translates only to the internal
`automation` eligibility enum. There is no new positive readiness default and
no mapped `runner_control` row. Mapped `control`, `human_input` and `resize`
reject with `unsupported_capability`; the existing ShellManager protocol is
unchanged. Callbacks execute under the shared gate, outside SQLite transactions,
and must not reacquire it or write input.

The existing attempt state machine now accepts optional finite
`paste_settle_timeout` in 0..1 seconds, default 0. After its single paste it
refreshes external readiness and snapshots, sleeping at most 25 ms per check.
Timeout, late positive proof, or lost evidence remains uncertain. The durable
Enter intent is still followed by the final pure verifier using a fresh tmux
snapshot and the final external-readiness check, without another settling wait.
Exact attempt replay returns saved receipts; interrupted intent stays uncertain
and never repeats input, including after restart or a later human hold/stop.

Only runner terminal/storage implementation, its owning tests, and this new
evidence file changed. No previous evidence was rewritten.

## Checks and original artifacts

Working directory:
`/home/alp/qorqut-core/worktrees/build-shared-terminal-input-r1`.
Private validation root `V`:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/shared-terminal-input-r1-validation`.

A private Python 3.12 venv contains the checkout installed editable with
`--no-deps --no-build-isolation`. Only the existing runner's required Pydantic
and the repository-pinned build packages were installed; no project dependency
or lock changed. Setup commands (output in `V/install.log`):

```sh
python3.12 -m venv "$V/env"
"$V/env/bin/python" -I -m pip install --disable-pip-version-check --no-cache-dir \
  'pydantic==2.12.3' 'setuptools==80.9.0' 'wheel==0.45.1'
"$V/env/bin/python" -I -m pip install --disable-pip-version-check --no-cache-dir \
  --no-deps --no-build-isolation -e .
```

Final owning-suite command:

```sh
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  "$V/env/bin/python" -I -m unittest discover -s tests/runner -v
```

**48 tests passed in 28.961 seconds; no failures or skips.** Original output:
`V/runner-suite.log`. This includes 14 new mapped/settlement checks, the new
real shared-gate shell check, and all existing owning runner checks:

- Registration identity/conflict/immutability, multiple historical runs, atomic
  mapped-versus-standalone registration races, and initial lookup/gate races.
- Restart lookup and receipt recovery under the terminal gate; saved human
  hold, epoch and sequence refusal without input; absent readiness remains
  deferred even when the shell is unclaimed.
- Callbacks can read original SQLite state and are outside write transactions
  while the actual terminal flock remains held.
- One real temporary Bash/recorder/tmux flow pauses after automated paste.
  A separate ShellManager process cannot take human control, and concurrent
  restarted-runner inspections cannot read/recover through the gate, until
  the attempt and its receipt settle. After takeover, automation defers and
  accepted human input produces exact ordered file contents `autohuman`.
  Release and sequence observation use the unchanged ShellManager path.
- One paste across settlement, bounded sleeps/fresh snapshots, fresh final
  proof after Enter intent, timeout/late proof/capture or hold loss, no extra
  Enter, immutable replay, and interrupted-after-intent uncertainty.
- Existing standalone receipts, exact bytes/bracket framing, stale controls,
  fault/crash recovery, process serialization, isolated shell recording,
  geometry and viewer behavior remain passing.

The initial narrower runs are retained as `V/shared-tests.log` (13 tests before
adding the late-proof case), `V/shared-shell-test.log` (one real shell test) and
`V/standalone-receipts.log` (14 tests). The final 48-test log supersedes those
intermediate test counts. A probe-first import check found an import cycle while
developing shell validation; the mapped-only local import fixes it. The final
probe-first import passed and is recorded with package versions in
`V/environment.json`: Python 3.12.3, SQLite 3.45.1, tmux 3.4, Pydantic 2.12.3,
pydantic-core 2.41.4. `git diff --check` also passed.

After making the new real-shell check's file poll tolerate not-yet-created
output, its exact focused rerun passed in 1.616 seconds (`V/shared-shell-final.log`):

```sh
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  "$V/env/bin/python" -I -m unittest discover -s tests/runner \
  -p test_shell.py -k test_mapped_automation -v
```

## Limits

Tests use private disposable stores, sockets, descendants and synthetic
readiness/composer evidence. The real Bash flow is a deterministic terminal
receiver, not native CLI ownership or provider compatibility. No provider call,
live owner session, browser profile, global service/configuration, VM or host
composition was used or changed. There was no nested delegation or independent
self-review actor.

Core still must supply verified incumbent native ownership/ancestry, active-owner
admission and positive capture/owner/hook/composer callbacks, and choose the
planned production allowance of 0.5 seconds. Those callbacks must be prompt,
pure observations where required. The deadline does not interrupt synchronous
external calls or kernel stalls; late settlement proof is refused. Fingerprint
and readiness observations cannot eliminate subsequent asynchronous process
death. This is local input-gate/receipt evidence, not full-system readiness,
Chat integration, native delivery acceptance, merge or deployment evidence.
