# Native startup connection: implementation evidence

2026-10-09. Implemented Manager D51 on
`codex/build-native-startup-connection-r1`, based exactly on
`0f74d2ad9deccdfe36744b0c5727341761cfdaab`. Manager review remains separate.

`runtime/native_startup.py` observes only descendants of the exact recorded
Bash, bounded to 32 levels/256 processes, with the existing twice-checked native
ancestry verifier. Admission requires one configured Codex 0.160.0 process and
one canonical managed-HOME writer descriptor, within a 256-descriptor bound.
Named file, descriptor, fdinfo inode/device, native PID and held whole-file
exclusive advisory FLOCK must agree. The descriptor, process, ancestry and
recorded shell are rechecked before `SessionOwners.connect`.

The observer runs inside NativeSetup's existing guards/tick. Its construction
is passive; unavailable proof leaves configuration readiness and owners alone.
Accepted bindings receive one agent-attributed `native.startup_observed` event,
deduplicated by the exact owner/candidate metadata. A missing event after a
committed connection is reconciled on the next observation. Existing owner and
hook policies remain unchanged. No idle, transcript-path or input grant is
created. The FLOCK field proof follows the immutable PORTING source
`e71fd1e527368bfd41b9815126f4bf33452aeebb`,
`bazarlyk/bootstrap/codex_session.py`; provenance is retained in the helper.
There is no live Qorqut import, lock acquisition or native-file modification.

## Focused checks

Private evidence root (`$E` below):

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-native-startup-connection-r1
```

Using `$E/venv/bin/python` and `PYTHONPATH=src:tests`:

- `-m unittest runtime.test_native_startup -v`: four grouped checks passed.
  Synthetic proc fixtures cover exact, missing, stale, mismatched and ambiguous
  lock/ancestry facts, bounds, final revalidation, incumbent retention, repeated
  observations, existing SessionStart handler reconnection, interrupted event
  persistence and restart deduplication.
- `-m unittest runtime.test_native_setup runtime.test_native_hooks runtime.test_owners runtime.test_process -v`:
  17 existing owning checks passed, including the small setup composition
  assertion that unavailable startup observation preserves installed readiness.
  No duplicated owner-policy suite or new dependency was added.
- Wheel build/install and `git diff --check` passed. The installed wheel SHA-256
  is `722d74b691943ab483bd50f1341933f3ec59d71a314d49b726ccbf07b6a4cead`.
  All 85 installed Python source files match the candidate worktree. Both real
  server logs show this installed startup module being imported.

The first startup-test invocation failed in fixture construction because its
synthetic `ProbeResult.reason` used an unsupported literal. It was corrected to
the existing `unavailable` value before the passing run. This was not a native
fixture failure; `$E/checks` retains the failure note and subsequent logs.

## One actual pinned startup and restart

Copied only completed cold-start helpers/artifacts into fresh private state.
The ordinary installed `qorqut-core serve`, workers, observers and recorded shell
ran in a new user/network/PID/mount namespace, with private HOME/native stores,
data, browser profile and `/tmp`; networking contained only loopback. Original
helpers and all 1,173 files in the accepted exploration evidence manifest still
match their saved hashes. No active archive-builder state was used.

Manually started Codex 0.160.0 once through the existing Terminal UI, retaining
the accepted `--no-daemon` and loopback fixture-provider configuration. Native
binary SHA-256:
`12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`.
Reviewed/trusted its eight configured hooks through the visible native dialog
and viewed/dismissed its bundled-bubblewrap warning. No prompt, seed, provider
request, native tools/call, classifier request, permission-mode change, SQL
mutation, fabricated native identity/file/hook or manual observer pumping.
The native default model label remained unchanged; no inference ran.

| Observed identity | Value |
| --- | --- |
| Agent | `2007f86b-67da-4e3d-8a28-0b16a69e3383` |
| Terminal | `42462d3c-a24f-45f3-b056-c2b159a4b46f` |
| Native process / start ticks | `1159 / 209919231` |
| Recorded ancestry | Codex 1159 → npm Node 1152 → Bash 55 |
| Native-selected UUID | `01a11f04-b558-7472-b28d-cfd0d6393f03` |
| Owner/run UUID; generation | `aab10961-f4c4-4f81-9b8b-853439fd2446`; 1 |
| Held writer proof | fd 41; device 09:01; inode 21513290; `FLOCK ADVISORY WRITE 1159`, 0–EOF |

Snapshots `10-startup`, `12-before-restart` and `13-after-restart` prove a
connected owner, exactly one startup event, zero official hooks/messages/input
attempts/native conversation entries, no primary native file, zero native sources,
`waiting_source` and `input_ready=false`. Core alone was stopped normally and
restarted. The native process, held descriptor/lock, shell, native UUID,
owner/run/generation and original startup event remained identical. The browser
session expired as expected; logging in again did not replay native launch.

Sent `/exit` exactly once. The immediate `19-exited` snapshot caught shutdown
still in progress: Codex emitted SessionEnd and materialized a primary file,
which the existing collector admitted. The later `20-exit-settled` observation
proved native absence and the original owner closed with `process_missing`.
These exit-time file/hook facts are separate from the pre-input absence. Every
terminal input receipt was submitted; no uncertain input was replayed.

Normal console shutdown and fingerprint-checked fixture cleanup left no live
saved processes, no namespace processes or launcher, and ECONNREFUSED on both
fixture ports. Cleanup retained its redundant already-exited browser stop
diagnostic; it sent no unverified signal. Browser outage/authentication and
favicon diagnostics remain in the private logs. `$E/audit.py` passed 51 evidence
assertions; these are artifact checks, not another product test suite.

First-Chat delivery remains unresolved and was not attempted. There is no
daemon/resume/child matrix, complete transcript-coverage, Claude classifier,
browser-service, VM or deployment acceptance here. No merge, PR or deployment
was performed; Manager retains integration and subsequent input-bootstrap design.
