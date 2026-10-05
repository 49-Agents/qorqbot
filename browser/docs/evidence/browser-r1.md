# Browser R1 local effect foundation

Bounded M06 implementation, 2026-10-07. Branch `codex/browser-r1`, based on
`3f0ff930bee1bb2dc9cf38478de0a1c4605ca9b3`; the completed SHA is supplied in the
ACLA completion report. Initial status was clean. Scope follows browser's
`BROWSER-PLAN.md` and core `BUILD-EXECUTION.md`/D21 at the assigned coordination
baseline `db25fc97a4127f8f46f3092ed96578bcdba685ce`. No global gate or whole-module
completion is claimed. No core/UI files, shared plans, live profiles or services
were changed.

## Implemented boundary

- Installable Python package with immutable Pydantic configuration, explicit
  agent/recovery/environment/boot/browser/protocol/control binding and loopback
  resources. Imports do not launch or provision anything.
- Explicit `Store.initialize()` claims already existing managed directories for
  the agent and provisions `browser.sqlite3` in reconciling mode. It cannot make
  a missing profile/HOME/mount. Each effect checks ownership, mountinfo identity,
  the configured readiness record, process fingerprints and owned listeners.
- One `Gate` persists actor, command UUID, canonical payload hash, exact binding,
  pre-effect intent and outcome. Actor comes from the trusted caller argument;
  the command model rejects payload actor/target overrides. Exact replays return
  historical receipts, including after control changes. Changed actor, payload
  or binding conflicts. A receipt is historical evidence, not live readiness.
- An inode-stable `flock` serializes effects across processes. The subprocess
  inherits its open lock description, preserving exclusion if its caller dies.
  SQLite uses WAL, foreign keys, FULL synchronization and bounded busy waits;
  no transaction spans an external effect or lock wait.
- Startup closes admission, fences the preceding service incarnation and retains
  human ownership/epoch. Abandoned intent becomes uncertain under the effect
  lock. Application reconciliation explicitly advances the expected epoch;
  close-before-wait transitions remain pending until the actual command settles.
  Missing/unknown participants cannot confirm readiness. An explicit application's
  settlement assertion can acknowledge an uncertain command is no longer in
  flight, with attribution/evidence, while its uncertain receipt remains intact.
- Local adapter supports snapshot/navigation/click/fill/screenshot using fixed
  CDP/session/config argv. No pass-through flags or caller screenshot path.
  Helper environment uses managed HOME/XDG/display and a private umask. Output
  streams to owned files; responses contain at most 8,192 text characters and
  four artifact references. Larger output becomes a hashed artifact. The default
  artifact budget is 64 MiB; overflow/error/timeout after possible dispatch is
  uncertain. No automatic replay occurs.
- Desktop helpers construct explicit start argv, bind actual owned child
  fingerprints and order shutdown Chrome, window manager, display. Unknown,
  reaped or unsettled Chrome prevents display teardown. Profile locks are never
  removed. These helpers do not install units, adopt existing browsers or start
  a desktop automatically.

These Python signatures remain module-local; no MCP/HTTP wire contract is
introduced. `BrowserTools` composes the adapter with the one gate. Its gate must
run startup and application reconciliation before admitting new commands.

## Passed verification

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`

Final result: **20 tests passed**, 10.067 seconds. Tests use temporary owned
directories, synthetic actions/state, forked workers, actual fake-helper
subprocesses and two private loopback listener fixtures. No real Chrome/VNC
session was started or attached. Assertions cover:

- Two agents with the same command UUID have distinct receipts/profile paths;
  reusing another agent's paths/database fails. Payload/actor/binding conflicts
  preserve the original receipt.
- Abrupt process exit immediately after committed intent and after a witnessed
  synthetic effect; restart never dispatches that UUID again.
- Concurrent duplicate commands; a separately held effect during takeover;
  pending takeover timeout/retry; concurrent control retries produce only one
  confirmation event; stale service/epoch/instance and saved human mode.
- A surviving subprocess retains the actual command lock after the service is
  killed. An explicit control request cannot report readiness until it exits.
- Real subprocess argv/environment, option-override rejection, bounded large
  snapshot and screenshot artifacts, overflow, nonzero exit, timeout and target
  readiness loss between the version probe and dispatch.
- Missing readiness/profile/mount/database, actual loopback socket ownership,
  readiness-instance mismatch and process exit, stale process fingerprints,
  shutdown order, stuck Chrome, reaped handles and retained profile locks.

One early artifact check exposed inherited group-write permissions; setting the
subprocess umask to 0077 fixed it. A held-command test once failed to observe
yielding within its short fixture deadline. Three focused re-runs passed; the
fixture's safety/observation deadlines were then widened and the final suite
passed. Product timeout behavior is tested separately using short explicit
timeouts. Inherited test-method duplication was removed before the final run.

Standalone packaging checks passed:

```text
.venv/bin/python -m pip wheel --no-build-isolation --no-deps --wheel-dir dist .
.venv/bin/python -m venv .venv/install-check
.venv/install-check/bin/python -m pip install -c requirements.lock dist/qorqut_browser-0.1.0-py3-none-any.whl
.venv/install-check/bin/python -m pip check
```

An isolated installed interpreter (`.venv/install-check/bin/python -I`) imported
all six package modules from an empty task-local working directory. Imports
created no files there and loaded no `bazarlyk`, `qorqut_core` or UI modules.
`git diff --cached --check` also passed before commit.

## Observed environment and prerequisites

| Item | Observed version/status |
| --- | --- |
| Observation time / test storage | 2026-10-07 21:20 UTC / ext4 temporary directories |
| Python / SQLite | 3.12.3 / 3.45.1 |
| Kernel | Linux 6.8.0-138-generic |
| Pydantic / pydantic-core | 2.13.5 / 2.46.5 |
| annotated-types | 0.8.0 |
| typing-extensions / typing-inspection | 4.16.0 / 0.4.4 |
| setuptools build backend | 84.0.0 |
| Google Chrome | `/usr/bin/google-chrome`, 153.0.8010.36 |
| TigerVNC | `/usr/bin/Xtigervnc`, 1.13.1, underlying X server 12101011 |
| Openbox | `/usr/bin/openbox`, 3.6.1 |
| agent-browser | Absent from PATH and the pinned source's `node_modules`; real compatibility not run |

Python dependencies were installed only in disposable worktree virtualenvs.
`requirements.lock` records the exact tested runtime/build dependency versions.
Desktop version probes were read-only; binary presence is prerequisite evidence
for a later Manager-coordinated rehearsal, not proof of desktop readiness.

## Source provenance

Read-only source root: Qorqut core revision
`e71fd1e527368bfd41b9815126f4bf33452aeebb` under its pinned component directory.
P08/P09 were narrow adaptations; no Qorqut imports/runtime, business/proxy leases,
configuration, credentials, data or desktop registrations were copied.

| Inspected source | Retained mechanism / SHA-256 |
| --- | --- |
| `bazarlyk/bootstrap/agent_browser.py` | Fixed external-CDP mapping, compact snapshot/ref translation, override rejection; `3ca727cc4a19a4d49a08f257f1a75e6fe947484decc6471bbaee9f7a924361e4` |
| `bazarlyk/bootstrap/browser.py` | Explicit target, timeout uncertainty and artifact behavior; `4020579f675753b06a8b74b3d671765fbd726b50db12c1f35275fd9ea3b586e8` |
| `tools/browser/desktop_session.py` | Fixed desktop argv, retained profile locks, unreaped-child ownership, Chrome-first shutdown; `c77e41e10934ed1f9e33a5b36fc4ccbb50e548b889da142e31a3948d7d21c085` |
| `bazarlyk/browser/isolated/session.py` | Readiness/process ownership and ordered flush boundary; `0a82dbf99f723bc476a4141bad3b5d78bce10127bb156a14935cc013b6d21d81` |
| `bazarlyk/browser/isolated/processes.py` | Unreaped process-group ownership; `ec792967190ab5308bcb1ff398b03b29668165ee39274626226a7c44b2ef7c1d` |
| `bazarlyk/browser/control.py` | Boot/start fingerprint, listener ownership, never-unlink lock inode; `4829c01508d44b3392c88ee7c576690838cf5ee4fac3dc6d44d840ae338a4363` |

Read the surrounding failure paths and source tests
`test_agent_browser_backend.py` / `test_browser_desktop_session.py`. Source files
carry no file-local third-party copyright/license notice. No third-party helper
code is bundled; its source lock identifies agent-browser 0.38.1 as Apache-2.0.
The adapter version check matches that inspected mapping; deterministic fake
execution is not evidence that the real binary is compatible.

## Unavailable / not run / remaining limits

- **Not run:** real helper, Chrome DOM/cookie/download behavior, browser restart,
  VM reboot, independent same-site cookies, VNC or RFB input enforcement, MCP
  discovery, native CLI integration, application HTTP handoff, Done/Cancel and
  machine-wide holds. No synthetic result counts toward these acceptance gates.
- **Pending assignments:** official MCP SDK server, frozen handoff/control wire
  contract, relay/filter, setup/unit installation and deployment. Browser owns
  no core request/Done/Cancel/message records. No placeholder tools are exposed.
- Runtime/cgroup integration must prove desktop survival across service restarts,
  detached-descendant containment and real profile flush. The current shutdown
  helper deliberately leaves the display in place on uncertain/stuck Chrome;
  callers must retain exclusive ownership of unreaped child handles.
- Configuration/readiness records and reconciliation evidence are supplied by
  trusted provisioning/application code. This is cooperative target/control
  integrity, not an OS permission boundary. Trusted readiness/executor callables
  are injected in fixture tests; production composition uses actual local checks.
- Readiness's CDP identity check is bounded and never follows redirects. The
  download directory is explicit/owned, but download actions/preferences remain
  later integration. Leading-dash helper operands are rejected because the
  inspected helper parses global options after operands; other action families
  remain unimplemented.
- No merge, PR, deployment, system installation or live-account action occurred.
  Independent Manager-assigned review remains required after this commit/push.
