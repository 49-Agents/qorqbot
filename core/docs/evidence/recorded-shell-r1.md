# Local persistent recorded shell r1

Implementation against `f4506c705f77f9c1b6b1575f1905b95c77b7c27f`, branch
`codex/build-recorded-shell-r1`, 2026-10-08. Scope is D37/RECORDED-SHELL plus
Manager's ACLA amendment 163: one terminal per dedicated runtime/socket;
every tmux viewer is read-only; authorized raw input uses a separately gated
tmux buffer. No planning document or frozen runner/recorder contract changed.

## Delivered boundary

- `runner/shell.py` exports `ShellManager` and the imported `ShellSpec` values.
  Construction initializes only local `runner_shell_*` tables. `ensure` records
  immutable terminal/source/epoch UUIDs, spec/config binding and launch intent
  before starting the existing recorder CLI with fixed Bash arguments
  `--noprofile --norc -i`. Terminal UUID is the capture producer run ID, generation
  1; it is never a native-session claim. No provider/model/startup command is an
  input. Managed native/XDG directories contain no generated credentials,
  configuration, persona or instruction files.
- Per-agent flock, SQLite uniqueness, private path claims and dedicated path
  locks prevent competing allocations in the local store and reject another
  database using the same runtime/socket paths. Runtime/socket ownership,
  global/session launch markers, socket
  inode, server/session/window/pane coordinates, exact executable/argv and
  process fingerprints corroborate recovery. The recorder probe independently
  supplies its live direct child. A recorded attempt is never launched again;
  an uncorroborated or incomplete attempt stays inspectably unknown.
- `inspect` reports running/stopped/unknown, separate verified recorder/Bash
  fingerprints, terminal source coordinates, live probe and historical capture
  fields. Saved healthy capture cannot certify liveness. Stopped terminals do
  not restart. Manager close has no process effect; explicit `shutdown` can
  terminate only the corroborated session. Normal Bash exit can precede final
  recorder drain, which remains visible in the historical capture fields.
- `ShellAttachment.read` is nonblocking and bounded to 64 KiB. Its PTY is raw
  before spawn; all clients use `attach-session -E -r`, never create/ensure.
  `write(data, epoch=, sequence=)` and `resize(columns, rows, epoch=, sequence=)`
  require logical write permission and the current human viewer/epoch. The
  per-terminal flock covers durable sequence consumption and exact-target
  rechecks through the effect. Raw `paste-buffer -r` adds no framing, newline
  conversion or Enter. Resize changes the exact tmux window, then mirror
  geometry; read-only clients cannot impose size. Unique-buffer cleanup only
  addresses its verified server. Close reaps the attachment's own client/fds.
- Takeover/release advance the durable epoch; disconnect retains the hold.
  Release returns `unclaimed`, with no automated/composer readiness assertion.
  Already admitted bytes cannot be retracted. An uncertain frame is consumed
  and cannot be replayed on reconnect. Viewer display is not an archive API.

The helpers are `runner/shell_models.py` and `runner/shell_storage.py`; the
owning checks are entirely in `tests/runner/test_shell.py`. Implementation uses
the existing process, lock, spool and probe primitives, and the existing narrow
tmux effect pattern. No upstream implementation was copied; notices, package
metadata, dependencies, host schema and other modules remain unchanged.

## Validation

Private validation root `V`:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/recorded-shell-r1-validation`.
Checkout `R`: `/home/alp/qorqut-core/worktrees/build-recorded-shell-r1`.
Python 3.12.3, SQLite 3.45.1, tmux 3.4, Bash 5.2.21(1), Linux
6.8.0-138-generic. Both private venvs use all 31 unchanged locked distributions.
`source-env` has this checkout installed editable with `--no-deps
--no-build-isolation`; `wheel-env` has only the built wheel plus locked packages.
Child recorders use their selected environment's absolute Python with `-I` and
an explicit clean environment, without host authentication or PYTHONPATH.

Final implementation source run, cwd `R`:

```sh
env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  "$V/source-env/bin/python" -I -m unittest discover \
  -s tests/runner -p test_shell.py -v
```

**7 checks passed in 8.018 s**, no failures/skips. Grouped coverage includes:

1. Passive manager initialization, rejected provider/invalid paths/dimensions
   and byte bounds, and unchanged local `user_version`.
2. Ensure twice/reopen, literal Unicode and tmux/shell-sensitive paths,
   fixed Bash/recorder process topology, sanitized environment, no model CLI or
   generated provider configuration, exact output capture and reattached display.
3. Two concurrent real manager subprocesses converge and exit; Bash and its
   positively identified background child survive viewer disconnect/reconnect;
   capture continues without a viewer. Changed specs and a second agent conflict.
4. Two observers are input/resize-inert, including initialization; takeover
   rejects a deliberately delayed stale write; typed text survives takeover;
   sequence/epoch rejection, resize observed by real `stty`, release, normal
   shell exit, and no implicit restart.
5. A manager subprocess exits with code 71 after launching, before saving any
   process binding. Reopen corroborates the same attempt without launch. A
   changed live marker prevents attach, recovery and shutdown without killing
   the existing shell; restoring that exact fixture marker recovers it.
6. An occupied foreign fixture socket is untouched; an injected launch failure
   remains unknown on retry; another database cannot claim the same paths.
7. Injected reply loss after actual input consumes the sequence; reconnect
   cannot duplicate the file append. A short SQLite writer hold stalls the real
   capture loop while saved health stays healthy; live admission closes. Killing
   the exact recorder never yields live capture or a replacement shell.

Build/install commands used private HOME and `PATH=/usr/bin:/bin`,
`LC_ALL=C.UTF-8`, `PIP_DISABLE_PIP_VERSION_CHECK=1`:

```sh
"$V/source-env/bin/python" -I -m pip wheel --no-cache-dir --no-deps \
  --no-build-isolation --wheel-dir "$V/wheel" .
"$V/wheel-env/bin/python" -I -m pip install --no-cache-dir --no-deps \
  "$V/wheel/qorqut_core-0.1.0-py3-none-any.whl"
# cwd V/staging: only the copied owning test file, no source/package tree
"$V/wheel-env/bin/python" -I -m unittest discover \
  -s "$V/staging" -p test_shell.py -v
```

**All 7 installed checks passed in 10.197 s**, including the representative
ensure → attach → human command → disconnect → reattach → recorded-output flow.
After adding an explicit physical Ctrl-U assertion to the existing takeover
check, that check passed once in each environment (2.410 s source, 3.041 s
installed). Those focused runs used `-P -m unittest -v
test_shell.ShellTests.test_observers_control_takeover_stale_frames_resize_and_exit`
with PYTHONPATH pointing only at `V/staging`; production bytes did not change.

`V/verify.py` verified **59 package Python/NOTICE file hashes**, all **31 locked
versions**, and the loaded shell/helper/recorder module paths in both
environments. An import audit rejected process/socket/SQLite creation, writes
and procfs observation; imports created no thread. Both `pip check` commands
passed. Build/install logs, the wheel, staged tests, manifest and verification
script remain under `V`; no global installation was performed.

| Artifact | SHA-256 |
| --- | --- |
| Wheel | `79fa918615502ae66f59cd13f9b688792d8e554b944d9d9df59a88afaddcc5c4` |
| Unchanged requirements.lock | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| Unchanged pyproject.toml | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |

## Development failures, cleanup and limits

Initial bring-up failed because a path lock used tmux's own `.lock` filename;
it now uses `.shell-lock`. The installed tmux also aborted its first window
when global `window-size=manual` was set before creation; the fixed startup
sequence now sets it on the created exact window. These diagnostics used only
disposable processes/configuration. A first SIGSTOP stall fixture was invalid:
[tmux 3.4 resumes stopped pane leaders](https://github.com/tmux/tmux/blob/3.4/server.c#L480).
It was replaced by the real, short capture-database hold described above.
The normal-exit check exposed a shutdown race between recorder death and tmux's
dead-pane observation. Shutdown now accepts positive death of its exact saved
pane while still requiring the same server/session/window/pane/PID, and never
requires a dead process to pass a live fingerprint check.

One early failed cleanup left a disposable tmux server after its temporary
directory was removed. It was subsequently identified through that exact
private socket inode and deleted fixture cwd, then terminated via its verified
pidfd. No name/newest-process lookup or owner session was a kill target. Final
test runs reaped their descendants under a test-only subreaper; no disposable
shell socket remained. Failure cleanup now retains exact recorded fingerprints
for bounded fallback cleanup before reporting a test failure.

This proves local process/object/viewer restart survival only. It does not
prove systemd cgroup survival, reboot, VM provisioning, a network/WebSocket/UI
endpoint, native CLI ownership/capture/readiness, inbox/Chat dispatch, host
archive transfer or dated native chat publication. Arbitrary detached process
trees still need the separate supervision boundary. Probe and fingerprint
checks are observations, not leases or atomic immunity from subsequent death;
kernel/filesystem stalls can exceed userspace deadlines. An incomplete launch
without corroboration remains unknown and needs later explicit reconciliation.
The trusted local API assumes the same cooperating OS user, not tamper-proof
isolation from that user. No provider calls, native/browser stores, live Qorqut
fixtures, host service/package changes, merge or deployment were used.

Commit/clean status and the normal assigned-branch push are reported through
ACLA. No internal reviewer or self-approval was run; independent review and
integration belong to Manager.
