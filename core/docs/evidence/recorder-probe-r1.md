# Live recorder observation r1 evidence

2026-10-08 UTC (2026-10-07 America/New_York). Implementation of the Manager's
[RECORDER-PROBE.md](../RECORDER-PROBE.md) design and D30, starting clean at
`bbb116bd55fe11ba2a05e67fbc0cb16afe3a1264` on `codex/recorder-probe-r1`.
Run `649fed24-a7af-40d7-9d41-382ab248483a`; worker
`2a156e87-92d6-4d15-9177-24c4bd1c598f`; bound executor conversation
`01a11928-ab2c-76c1-9773-6f9b3615b67a`. Saved Codex/Astra identity retained;
no delegation or internal reviewers. Fresh independent review remains required.

## Delivered boundary

- `transcripts/probe.py` adds strict, extra-forbidding version-1 request,
  observation and result models, using the owning capture `RunBinding` and
  runner `ProcessFingerprint`. The supplied 1–80 character ASCII name selects
  exactly one Linux abstract datagram address. Datagrams are bounded to 8 KiB;
  body/ancillary truncation, unknown fields/versions and malformed coordinates
  cannot produce a verified result. Received descriptor ancillary data is
  rejected and any delivered descriptors are closed.
- `probe_recorder(address, expected_binding, source_id, source_epoch,
  expected_recorder, *, timeout_ms=200)` validates local arguments first. It
  verifies the expected live process before sending and after a matching reply,
  requires the same local PID namespace, uses a fresh UUID nonce and one random
  return address, and sends exactly one request. Kernel `SCM_CREDENTIALS`
  PID/UID, the returned full fingerprint, nonce and all source/run coordinates
  must match. One monotonic deadline and at most eight replies bound socket
  processing. There is no retry, cache or stored-health fallback. Results use
  only the specified fixed reasons; authentic inactive observations remain
  verified but cannot claim active capture.
- `transcripts/recorder.py` adds optional `probe_address=None` and
  `--probe-address`. Binding occurs in explicit `run`, before epoch reservation
  and fork. An occupied name fails without consuming the epoch or launching a
  child. The listener is included in the existing select loop; at most one
  request is handled after the existing I/O work each iteration. No thread or
  heartbeat answers independently. The existing child close range closes the
  listener before exec, and all exit/startup/failure restorations attempt its
  close along with the other restorations.
- A response uses the committed exact recording/run/source/epoch link and
  checks its boot against the observed recorder boot. The exact direct-child
  fingerprint is recorded after fork, checked against the recorded start and
  verified anew for replies. Active capture requires process-local admission,
  that live child, no observed child exit/PTY EOF, and a final admission check
  after the observations. No name search or replacement-child adoption occurs.
- Malformed requests do not write the spool or terminal. Departed or slow
  clients cannot block sends. Fatal receive/send endpoint failures disable the
  observer and let capture/draining continue. Endpoint cleanup errors are kept
  for the existing complete restoration/error path; they do not skip later
  restorations. The new spool `recording_binding` accessor is one read-only
  SELECT; it neither initializes tables nor links/adopts a run.

Only these three production files, `tests/transcripts/test_probe.py`, narrow
fixture extensions in `support.py`/`test_recorder.py`, and this evidence changed.
Existing test assertions and timing bounds remain unchanged. There are no
runner/provider, transport, host migration/registry, package metadata, global
plan, browser or UI edits. This is new code using the accepted runner identity
primitive; no upstream source was copied and existing notices remain intact.

## Checks and results

Private validation root (`V` below):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/recorder-probe-r1-validation`.
Checkout (`R`): `/home/alp/qorqut-core/worktrees/recorder-probe-r1`.

Python 3.12.3, SQLite 3.45.1, Pydantic 2.12.3/pydantic_core 2.41.4,
Linux 6.8.0-138-generic x86_64, glibc 2.39. Both private virtual environments
were created with `/usr/bin/python3.12 -m venv` and installed the unchanged
31-distribution `requirements.lock`. Pip subprocesses used only
`PATH=/usr/bin:/bin`, private child `HOME=V/home`, `LC_ALL=C.UTF-8` and
`PIP_DISABLE_PIP_VERSION_CHECK=1`.

The source owning suite ran once, before the final one-line unsigned-UID
correction and its focused regression described below:

```sh
# cwd R; PYTHONPATH=src resolves only this checkout.
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /usr/bin/python3.12 "$V/run_owned.py" "$V/source-env/bin/python" -P \
  -m unittest discover -s tests/transcripts -v
```

**33 tests passed in 29.658 seconds**, no failures or skips. This includes all
22 existing recorder/spool tests: exact bytes and large/partial records,
durable-before-display and partial writes, startup signal delivery, D27 owned
session cleanup/reaping and complete terminal/mask/descriptor restoration,
storage failure latching, ACK/reclaim integrity and crash boundaries.

The wheel was built from this checkout and installed only into `V/wheel-env`:

```sh
"$V/source-env/bin/python" -I -m pip wheel --no-cache-dir --no-deps \
  --no-build-isolation --wheel-dir "$V/wheel-unsigned" .
"$V/wheel-env/bin/python" -I -m pip install --force-reinstall --no-cache-dir --no-deps \
  "$V/wheel-unsigned/qorqut_core-0.1.0-py3-none-any.whl"
# cwd V/staging, containing only an exact tests/transcripts copy, no src/package.
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3.12 "$V/run_owned.py" "$V/wheel-env/bin/python" -I \
  -m unittest discover -s tests/transcripts -p test_probe.py -v
```

**11 probe tests passed in 10.203 seconds** on the final wheel, no failures or
skips. The preceding wheel/probe run passed 11 tests in 9.721 seconds before the
unsigned-UID correction. The live child
reports its actual fingerprint and loaded package file; the tests verify its
import origin equals the parent's loaded source/installed package. Probe
fixtures use observed OS boot/process coordinates, leaving existing deliberately
synthetic recorder/spool fixtures unchanged. The focused checks cover:

- Real nested PTYs with a synthetic idle child, fresh observations before and
  after exact binary/Unicode bytes, normal exit with a historical healthy row,
  and no positive result after exit. Whole database snapshots prove query-only
  traffic leaves bytes, cursors, ACKs, health and links unchanged; the synthetic
  child detects unexpected terminal input.
- Positively owned pidfd SIGSTOP/SIGCONT: the stopped recorder cannot answer an
  80 ms query (asserted between 70 and 800 ms), its saved healthy state remains,
  and the same resumed process answers with a new nonce. A resumed reply to the
  vanished timed-out client cannot satisfy a new query.
- All run/source coordinates, nonce, version, boot/start/UID/namespace and
  returned fingerprints; strict booleans; nested extra fields; oversized,
  malformed and truncated datagrams; eight-reply exhaustion; authentic inactive
  responses and failed post-reply process verification. An actual distinct live
  subprocess attempts to impersonate the expected responder and is rejected by
  kernel credentials. Missing/wrong/duplicate/malformed credential and flag
  cases additionally use explicit receive-boundary fault injection; these are
  not claims of forging kernel credentials. Actual descriptor-bearing responses
  are rejected without descriptor leaks.
- Wrong stored boot never yields an invented identity. A local unpersisted latch,
  inactive loop, changed direct-child start, already observed exit/EOF and
  admission lost during child observation each return verified inactive. The
  local-latch fixtures leave the stored row healthy to distinguish those facts.
- An occupied endpoint causes no fork or epoch mutation; later startup failure
  closes its socket. Oversized/ancillary-bearing/bad requests, a vanished client
  and an unread full return queue leave capture working. Injected fatal receive,
  send and closed-endpoint cases preserve subsequent exact bytes and healthy
  normal drain. Listener/client abstract names can be rebound after owned close.

Before the final suite, a protocol diagnostic passed strict JSON round-trip and
nested extra/coercion rejection. An initial focused development invocation of
the same source test command with `-p test_probe.py` passed **11 tests in
9.375 seconds** before adding full-client-queue/closed-endpoint/descriptor-leak
coverage.

Final wire inspection identified that Linux `ucred` uses a signed PID but
unsigned UID/GID. A new synthetic ancillary regression for UID `2**31 + 7`
failed once against the original `struct.Struct("3i")` decoder (**1 failure in
0.001 seconds**), demonstrating a false negative for high UIDs. No OS credentials
were changed. Changing the format to `"iII"` made that focused source test pass
(**1 test in 0.339 seconds**). Its exact command was:

```sh
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=src:tests/transcripts "$V/source-env/bin/python" -P -m unittest -v \
  test_probe.ProbeBoundaryTests.test_credentials_and_truncation_are_required_and_replies_are_bounded
```

The final wheel was rebuilt/reinstalled, all 11 installed probe tests rerun as
above, and both source/installed passive-import/hash/pin diagnostics repeated
against the final manifest. This was the only failed test; no build, install or
other diagnostic failed. No assertion/timing was weakened and no whole-system
suite was run. The 22 unchanged existing tests were not unnecessarily repeated.

`V/verify.py V/manifest.json <expected package directory>` ran in both environments
(source `-P`, `PYTHONPATH=src`; installed `-I`, no PYTHONPATH). It verified all
**37 package Python/NOTICE files** against source hashes, all **31 locked versions**,
and the loaded probe/recorder/spool/runner module paths. An audit hook forbids
SQLite, socket/process creation, writes and procfs observations during import;
imports and Recorder construction were passive, with no added thread. Both
`pip check` calls passed. `git diff --check` and byte-identical metadata/lock/NOTICE
comparisons passed. Logs are under `V/logs`; scripts, manifest, environments,
test-only staging and wheel remain there as private validation artifacts.

| Artifact | SHA-256 |
| --- | --- |
| Final built wheel | `009d87535b72bf276fa82fca7a3c1f2a84b5998c604b5ac3595a71d55c5104af` |
| Unchanged `requirements.lock` | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| Unchanged `pyproject.toml` | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |

## Cleanup and limits

Tests use disposable databases, PTYs, abstract sockets and synthetic Python
programs. The accepted private subreaper wrapper was reused without changes:
the source suite reaped one adopted child from the existing killed-recorder
fixture; the initial/installed probe suites reaped zero. Every wrapper returned
with no remaining child and no fallback kill. Live fixture signals target held
pidfds with observed identities; cleanup never signals a name or unowned PID.
Temporary fixture data and descriptors were closed/removed. Private virtualenvs
are not global installations. No owner session/store/auth/profile, provider/model
turn, persistent service, cgroup, VM, network configuration or browser was used.

A positive response is only a fresh observation of this exact recorder loop,
direct child and terminal byte source. It is not successful exec, native provider
identity/authentication/session/composer readiness, native source ingestion,
machine-hold policy, managed launch ownership, host archive/backlog durability,
independent process supervision, or a lease. Processes can die immediately after
responding; failures/timeouts are not proof of death. Kernel/filesystem calls can
stall outside userspace socket deadlines. Linux abstract sockets/credentials and
the same local PID namespace are required. The existing cooperative PTY cleanup
scope and storage/fsync assumptions remain; detached-session/cgroup supervision
is unproven. No production `TerminalRunner.capture_ready` callback was connected
and no capability/lifecycle/G0–G6 gate advanced.

Final commit, clean status and normal dedicated-branch push are reported through
ACLA. Manager owns independent review and landing. No main merge, force push,
rebase, PR, release or deployment is part of this assignment.
