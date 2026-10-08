# Host message dispatch r1

Implementation evidence for the fixed D46 assignment in
`docs/HOST-MESSAGE-DISPATCH.md`, recorded 2026-10-09 UTC. Branch
`codex/build-host-message-dispatch-r1` starts at
`e74369c0e214570f4a6892650f3ae59e3689787c`. This file belongs to the implementation
commit; its immutable SHA and pushed status are reported through ACLA. Manager
acceptance, integration and real pinned-native Chat acceptance remain separate.

## Implemented behavior

`RuntimeInput` prepares an immutable, specific target for the reserved onbash
agent without registering a run or supplying input. It verifies the owned
workspace and original recorded shell, installed managed hook configuration and
endpoint, exact incumbent owner and accepted idle hook attribution, current
native executable and bounded ancestry to Bash, fresh recorder/capture evidence,
collector observation and admitted primary native source, unclaimed control and
the D43 empty-composer verdict. An old idle hook can remain valid when these
fresh checks pass. Rejected rival hooks do not replace the incumbent observation.

Submission delegates only the 51-byte UUID notification to the existing D44
runner with a 0.5-second settling bound, saved terminal mapping, mandatory exact
`still_current` boolean and repeated ownership/hook/capture proofs under the
shared terminal gate. The final paste verifier checks the notification's digest
and its exact fresh screen contents. Callback tests acquire temporary SQLite
writers and probe the flock to verify that observations are outside host/runner
write transactions and inside that gate. The paste state machine is unchanged.
Human control remains ShellManager-owned. The local boundary supplies no machine
hold; a future machine takeover must participate in this same gate.

Inspection uses the existing deterministic runner store, validates the saved
mapping/binding and original epoch, and reconciles the original attempt without
registration, launch, paste or adoption. A closed native process does not prevent
receipt inspection. Missing/replaced/inconsistent evidence cannot establish
receipt absence or authorize new input.

Migration 9 adds immutable delivery attempt identities and linked sequences,
canonical target/digest and notification digest/length, mutable receipt-backed
observations, terminal-state protection and attributed transition events.
Migrations 1–8 remain unchanged. `deliveries_get` exposes typed, ordered attempts
without process fingerprints, paths or complete runner receipts. The existing
DeliveryView, immutable messages, handling, operation receipts, 17 operations and
5 job handlers are preserved.

The normal message job composes RuntimeInput, current-epoch recipient FIFO and
the one-notification-per-idle-hook barrier. Prepared intent, dispatching intent,
receipt and delivery transitions are durable. Deferred/no-input receipts permit
a fresh linked attempt; uncertainty requires inspection of the original attempt
and never causes another paste. Current-epoch offline message jobs can wake when
due; other dependency jobs and recovery-held work do not wake. The existing
bounded uncertain-job reconciliation includes message delivery and performs no
new submission. `runtime_get.input_ready` remains the conservative literal false.

## Checks and original artifacts

Working directory:
`/home/alp/qorqut-core/worktrees/build-host-message-dispatch-r1`.
Private validation root `V`:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-host-message-dispatch-r1-validation`.
`P` below is `$V/venv/bin/python`, a private Python 3.12 environment with the
checkout installed editable and the unchanged pinned project dependencies.
Installation output is in `$V/install.log`. Tests use disposable data, processes,
ports and synthetic transcripts; no live installation or owner session is a
fixture.

All commands below use `PYTHONDONTWRITEBYTECODE=1` and
`PYTHONPATH=src:tests`, except the runner check, which adds `tests/runner`.

```sh
"$P" -m unittest messaging.test_dispatch messaging.test_migration \
  messaging.test_messages messaging.test_transports runtime.test_input_dispatch -v
```

37 passed (`$V/d46-final.log`, 78.626 seconds). Coverage includes all fixed
preflight refusals, composed Application delivery, exact notification and message
body/control-character preservation, FIFO and new-idle admission, linked deferred
retries, uncertain/missing/mismatched original receipts, claim loss, every host
fault seam and restart, post-paste and post-Enter-intent proof loss, native-exit
receipt inspection, recovery holds, attributed/no-duplicate events and matching
HTTP/MCP typed attempts. The schema-8 fixture is built by running the exact base
source from `git archive` in a disposable directory. It verifies migration
rollback, original checksums/rows/JSON receipts, recovery holds and offline wake.

```sh
"$P" -m unittest runner.test_shared_terminal \
  runner.test_shell.ShellTests.test_mapped_automation_and_shell_human_control_share_thread_and_process_gate -v
```

15 passed (`$V/runner-gate.log`, 3.623 seconds). Includes the existing isolated
real tmux/Bash cross-thread/process serialization, mapped human control and
receipt integration, alongside synthetic D44 cases.

```sh
"$P" -m unittest test_foundation transports.test_worker runtime.test_native_hooks \
  runtime.test_native_collector runtime.test_storage runtime.test_native_setup \
  files.test_migration archive.test_inventory archive.test_passive -v
```

The original `$V/regression-final.log` records 55 tests: 51 passed, 3 assertion
failures and 1 setup error. The four affected tests had old message-inspector
eligibility, schema-8 or 16-tool expectations. These expectations were updated
for the assigned message job behavior, schema 9 and existing 17-operation
inventory. Their focused rerun plus related affected checks passed:

```sh
"$P" -m unittest transports.test_worker runtime.test_native_setup.Observations \
  runtime.test_native_setup.SetupServer.test_normal_console_setup_http_mcp_attribution_and_restart \
  agents.test_registry.RegistryTests.test_old_code_start_true_receipts_replay_unchanged_after_reopen_and_never_launch \
  transports.test_terminal.TerminalTests.test_passive_settings_registration_cache_and_owned_workspace -v
```

8 passed (`$V/corrected-inventory-worker.log`, 12.505 seconds). Following the
explicit queued recovery-review claim exclusion, its two affected checks passed:

```sh
"$P" -m unittest \
  messaging.test_dispatch.Dispatch.test_restore_holds_all_unfinished_attempts_and_does_not_wake_them \
  messaging.test_dispatch.Dispatch.test_offline_dependency_job_wakes_without_other_dependency_jobs -v
```

2 passed (`$V/claim-final.log`, 3.418 seconds). Following the exact managed
endpoint comparison, the changed preflight and composed job checks passed:

```sh
"$P" -m unittest \
  runtime.test_input_dispatch.InputProofs.test_unknown_owner_and_live_executable_or_managed_hook_change_refuse \
  runtime.test_input_dispatch.InputProofs.test_normal_application_job_composes_runtime_and_saves_typed_attempt -v
```

2 passed (`$V/config-final.log`, 2.578 seconds). The existing
`transports.test_terminal.TerminalTests.test_console_recording_control_authentication_and_restart`
also passed after its 16-tool inventory expectation was updated to 17. Its
original `$V/config-terminal-final.log` records that success and two unittest
loader errors from mistakenly using the class name `InputDispatch`; the correct
class is `InputProofs`, and those exact two methods passed in `config-final.log`
above. Earlier failed development logs remain in the private validation root;
they are not presented as successful checks.

`git diff --check` passed. Original log SHA-256 values:

| Artifact under V | SHA-256 |
| --- | --- |
| d46-final.log | 075f287c3994d2cc148c9097fcda58fc3e7eea67dfd2846e209ad001bb57b40e |
| runner-gate.log | 8984a6d15ed2e0dde9ec0c3ab46fa493d3d5e1ee7040c14ad5e0a1b1235ae436 |
| regression-final.log | 658038b3700912e9c5b498b627159d7c9352a3f56f231e6f92209deee9a856c9 |
| corrected-inventory-worker.log | c6edd6275c23e57c20ebcbe575f05d1d4fd448dc19ca0d90531b03d4404d7428 |
| claim-final.log | a015d26da6f0ea44c243b1f5be96855d8b00b668c5972a0026854c8d6725a0cd |
| config-final.log | 5d3cff3570b4bf4462579e00d89345a7b4936fe0eeea7774e162d0b7c6c30ba8 |
| config-terminal-final.log | cc46903c452c8ef62b886febe3e0346d6bd003e09299b2099b3889ebca14ae52 |

## Evidence limits

Native ancestry checks use real disposable Bash/Python processes with copied
synthetic provider executables. Recorder, composer and provider-ready evidence
in the new composition fixtures is explicitly synthetic; host crash fixtures
use synthetic runtime receipts. The real tmux checks prove local serialization
and receipt behavior, not real Claude/Codex acceptance. No paid inference,
provider installation, UI/browser change, VM work, service deployment or owner
native home is involved. A submitted receipt proves one verified notification
paste plus normal Enter, not processing or handling. Initial primary-source
discovery is required and may still need the already-observed first manual
Codex Terminal prompt. No zero-backlog or complete-source-coverage assertion is
made. Notification-echo association remains the following projection increment.
