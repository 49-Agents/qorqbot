# Recorded Terminal geometry: core implementation evidence

2026-10-08. This candidate on `codex/fix-terminal-geometry-core-r1` implements
the core lane of [the Manager's geometry contract](../TERMINAL-GEOMETRY.md),
based exactly on `6149b1bd1e82f637e62d9e7e1f5d51ea29b8a8a2`. Independent
Astra MAX review and Manager acceptance remain separate from this report.

Verified shell observations now include immutable, strictly bounded pane
dimensions. Both existing target checks include tmux identity, live state and
geometry; invalid observations remain unknown. Public available Terminal views
require dimensions. Construction and `terminal_get` remain passive.

Each physically read-only `-E -r` mirror starts with the verified pane's PTY
size. Attachment verification also checks `read-only` and `ignore-size` client
flags; the pane retains manual window-size authority. The local display setter
serializes only its owned PTY and exact unreaped tmux child, signals that client
with SIGWINCH on changes, and uses no runner flock, process query, tmux resize
command or input bytes. Closed attachments refuse it. Authorized pane resizing
keeps the existing effect/sequence path; Terminal then publishes a fresh actual
observation without promoting an uncertain receipt or retrying the effect.

The stream returns each bounded read together with its immutable dimensions.
The single protocol-2 writer announces a changed size before that read's bytes,
including changes with no bytes. Busy reads still skip the stream lock. Cached
unavailable/stale targets cannot resize a mirror or supply another binary read.
The existing session checks, stop/reply coordination and five-second cache bound
are unchanged.

Only the five assigned source files, the existing shell/Terminal test groups and
this evidence change. Host schema 6, five handlers, sixteen operations, both
dependency inputs, shared lifetime helper, recorder, storage, ownership, native
APIs, adapters and messaging remain unchanged. The parallel UI worktree was not
read or changed; this actor did not build the UI or run a browser.

## Boundary checks

The installed checks use real isolated tmux 3.4, Bash 5.2.21 and the package's
recorder, with disposable HOME/data/socket paths and loopback ports. Synthetic
shell commands create the content; no provider CLI runs.

- The initial pane is 80x24. An authorized resize produces a verified 132x40
  pane, and a newly attached read-only mirror starts with actual PTY size
  132x40. Its tmux client reports the same grid and both required flags.
  `RIGHT_TAIL_217` starts at column 100 and `BOTTOM_ROW_217` at row 35;
  real pane capture and the new mirror's raw bytes contain both markers.
- Local mirror resizing to 90x28 completes while another thread holds the real
  runner input flock. The test forbids process/tmux queries and PTY input,
  checks one exact-client SIGWINCH and one winsize update, and verifies no
  repeated signal for the same size. Pane dimensions, captured bytes, viewer
  ownership, epoch and sequence remain unchanged. Invalid dimensions and a
  closed attachment are refused.
- Both Terminal mirrors follow an authorized 132x40 resize. An injected lost
  resize reply remains uncertain, consumes its sequence once and publishes the
  actually observed 120x35 pane for both reads. Malformed and out-of-bound tmux
  geometry, including the final target check, yields unknown with no dimensions.
- The normal installed console exercises protocol-2 hello, size announcements,
  actual 113x31 tmux clients, and another viewer's non-default initial hello.
  A deterministic read seam separately asserts old bytes, an empty-read size
  announcement, then new bytes in that order. This ordering seam is synthetic;
  the shell, recorder, console and socket checks use real processes and sockets.
- All four review-202 regressions remain: real runner-lock state cadence and
  readiness, cancellation settling a delayed real stale-control failure,
  changed-topology/stale-cache viewer refusal, and complete UTF-8 installation
  size preflight. The retained tests also check control takeover, literal
  Unicode/control keys, input uncertainty, authentication, bounded slow output,
  server restart preserving exact Bash/child/hold, and capture while disconnected.

Fault/reply-loss injection, bounded ASGI slow-send barriers, malformed observation
responses and the ordered-read seam are synthetic. No test claims that a
submitted frame proves shell or native processing. Live PTY resize/redraw is not
an atomic screen snapshot; local capture is not host archival or native history.

## Validation artifacts and limits

Private artifacts are under
`01a116b0-8d6d-72f2-9da9-76bd2e898f04/fix-terminal-geometry-core-r1-private/`.
They retain the wheel, build/install logs, exact test copies, process/import
traces, synthetic capture/mirror bytes and verification scripts. Nothing from
those fixtures, credentials, captures or operational databases is committed.

Final installed validation passed all 14 owning groups in 80.417 seconds, with
zero failures, errors or skips. `git diff --check` also passed. A fresh private
Python 3.12.3 venv used the unchanged lock and pinned build tools. The build
copied only the candidate package inputs; tests ran from four byte-identical
test/helper copies with isolated Python imports. Commands were:

```text
venv/bin/python -m pip install -r requirements.lock setuptools==80.9.0 wheel==0.45.1
venv/bin/python -m pip wheel --no-deps --no-build-isolation build-source -w wheels
venv/bin/python -m pip install --no-deps --force-reinstall wheels/qorqut_core-0.1.0-py3-none-any.whl
strace -ff -s 4096 -e trace=execve,openat,kill,ioctl -o trace \
  venv/bin/python -I run-installed.py
python3 verify-trace.py
```

`run-installed.py` loads exactly `runner.test_shell` and
`transports.test_terminal`. All 66 package Python/NOTICE files match the source,
wheel and installation; all 32 locked distributions match; all 62 imported core
modules resolve within the installed package. Wheel SHA-256:
`4f3710dbde4886e7bdf95a3711a010ac02c395fc611fb104cde9c14a2605bb31`.

Trace verification identifies two actual normal `qorqut-core serve` starts and
ten recorder processes importing that installation. It identifies 31 read-only
tmux attachment processes and confirms every attachment exited; explicit
positive-PID SIGWINCH targets are only those exact clients. Recorded PTY sizes
include 80x24, 132x40, 90x28, 113x31 and 120x35. All 55 recorded process
fingerprints were gone after owned cleanup, and all twelve recorded fixture
roots were removed. The retained final raw mirror includes both far-edge
markers; another capture includes output produced while the console was down.
Detailed hashes/identities are in `installed-provenance.json`,
`trace-verification.json` and `toolchain.json`.

Development validation initially closed a viewer during the console lock/resize
check in two traced runs. The isolated untraced check passed. A redundant new
runner inspection was removed, preserving the original resize effect path and
the required fresh Terminal observation. The corrected console group then
passed under full tracing in 39.366 seconds. Earlier logs and the earlier wheel
are retained as diagnostic evidence, not passing final-candidate evidence.

Actual compiled UI/Chrome interaction and screenshots belong to the separate
combined acceptance actor after both corrected pins are reviewed. This change
does not establish native startup/inbox/Chat delivery, complete native capture,
VM or browser-MCP operation, reboot/systemd survival, or deployment acceptance.
No provider calls, live owner fixtures, global installations, merge, PR or
deployment were performed.
