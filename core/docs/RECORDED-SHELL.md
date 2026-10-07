# Persistent recorded shell and Terminal attachment

Manager decision D37, 2026-10-08. Build the local terminal backend in parallel
with inbox ownership and adapter fixes. Creation ultimately invokes this shell
setup; it never invokes a model CLI. This assignment delivers a real persistent
shell and bounded PTY attachment API, without installing a host service or
advertising an unfinished WebSocket route.

## Fixed composition

Add `runner/shell.py` and, if needed, small owned `shell_models.py` and
`shell_storage.py` helpers. Do not change existing automated TerminalRunner,
RunBinding, process, tmux or recorder contracts. Use existing local runner/spool
SQLite storage, with new `runner_shell_*` tables initialized by this module; no
new database, host migration, dependency or daemon service.

The public Python entry is `ShellManager(database, runtime_root, socket_path,
python_executable, tmux_executable, shell_executable)` with explicit absolute
paths. Shell executable is Bash; its argv is fixed to `--noprofile --norc -i`.
No arbitrary startup command, native CLI argv, provider or model is accepted.
`ShellSpec` contains installation/agent/recovery UUIDs, environment generation,
explicit distinct managed HOME/workspace paths, and an explicit PATH string.
The caller chooses the local target: the future host composition permits this
onbash path only, and will not silently substitute it for a VM agent.

`ensure(spec)` allocates and records a terminal UUID and terminal source/epoch,
then establishes exactly one owned private tmux session with one recorder pane.
The recorder launches Bash as its child using the accepted recorder CLI. Use
the terminal UUID in the existing capture run_id field for this terminal
producer, generation 1; it is not a claimed native CLI session/run. Native
execution owners get separate IDs later and can link to this terminal source.
The recorder writes complete PTY bytes into the shared local spool before
display; no viewer must be required for capture to continue.

Construct the new environment explicitly: provided PATH, managed HOME,
CODEX_HOME, CLAUDE_CONFIG_DIR, XDG config/cache/data paths, TERM=xterm-256color,
LC_ALL=C.UTF-8 and QORQUT_TERMINAL_ID. Do not inherit host authentication or
global CLI configuration. Create owned native/XDG directories, but do not
write provider credentials/configuration, authored persona files or instructions.
Use structured argv and proper tmux argv handling, never interpolate workspace,
PATH or user input into shell source. Validate path/socket length constraints
before launching. Dedicated socket/runtime paths must be owned by this uid and
must not name a foreign or ambiguous existing tmux server.

Serialize ensure with one per-agent flock and enforce one active/unknown shell
slot for an installation/agent in the local store. A changed spec for an occupied
slot is a conflict, not another allocation. Concurrent identical ensure calls
converge on the same recorded attempt. No stale-lease expiry authorizes a launch.
Record the spec hash and launch intent before the process effect. The terminal
UUID selects a unique deterministic session name on the private socket. Return
an existing receipt only after verifying its exact socket/server/session/pane,
recorder and shell fingerprints plus the live recorder probe. Never search by
command name or newest process. A timeout or crash after launch is unknown,
not permission to create another shell. A subsequent ensure may finish the
same recorded attempt only by corroborating its private socket/session launch
marker and exact process identities. If it cannot, return an inspectable
uncertain result without creating/killing/adopting anything.

A stopped shell is reported stopped. Automatic replacement/restart is outside
this increment; ensure is not an implicit restart. Normal server/application
close and object destruction leave the shell/recorder/tmux intact. Exiting Bash
ends that shell normally. Only a separately explicit shutdown of the exact
recorded terminal may terminate the owned session; no kill-server by guessed
name and no cleanup of any owner's sessions.

`inspect(terminal_id)` returns a typed view containing the spec binding,
terminal UUID, state `running|stopped|unknown`, TmuxBinding, recorder/shell
fingerprints when verified, source/epoch, probe address, and capture observation.
Saved health alone never means a live recorder. Keep process probing outside
SQL transactions. This trusted local view is not an unauthenticated network
response and must not label Bash as a native agent or connected inbox.

## Attachments and human input

`attach(terminal_id, viewer_id, *, read_only=True)` attaches a disposable tmux
client on a newly allocated PTY to that exact existing session. It never invokes
new-session or ensure. The returned `ShellAttachment` has bounded `read`,
`write`, `resize` and `close` methods, or equivalently named documented methods
with these semantics. Keep its fd nonblocking; read/write chunks at most 64KiB,
validate dimensions with the existing helper, and add no unbounded output queue.
This is the byte-stream API a later authenticated WebSocket handler will use.
Its close reaps only its own attach client/fds; it never kills the recorder or
shell. Reattaching receives tmux's current display, while raw capture continues
independently. The viewer stream is not an archive/replay guarantee.

Persist one per-terminal human-input control record: mode, epoch, viewer UUID,
last input sequence. `take_control(terminal_id, viewer_id, expected_epoch)`
advances the epoch and binds that viewer, explicitly replacing an earlier human
viewer only when the expected epoch still matches. `release_control` matches
that viewer/epoch, advances the epoch and clears human ownership. It does not
clear typed text or assert native/composer readiness. Disconnect preserves the
human hold until explicit release/takeover. Read-only viewers cannot type or
resize; inactive/stale viewers cannot type after release or takeover.

Writable attachments accept each input/resize with current viewer/epoch and an
increasing sequence, consume the sequence durably before any possible effect,
and never replay it after uncertainty/reconnect. Serialize control and input
with a per-terminal local flock and reverify exact bindings at the effect. An
I/O failure after admission is uncertain. Physical owner keystrokes can contain
ordinary terminal controls; this is not automated message paste. Future
automated delivery must consult this same terminal hold before its existing
native-run gate. There is no automated dispatch in this assignment, and release
must not expose a ready-to-paste default.

All viewers must be input-inert until the service writes an authorized frame;
no automatic tmux attach-client initialization sequence may become shell input.
Use tmux's read-only client mode for observers. User terminal escape processing
is not a new security boundary against the machine's owner.

## Checks and ownership

Own the new shell module/helpers, compact `tests/runner/test_shell.py` and
`docs/evidence/recorded-shell-r1.md`. Other runner modules are read-only;
ask Manager before modifying a frozen existing contract. No host app/server,
agents/workspace, adapters, UI, package/dependency or global-plan changes.

Use a dedicated short socket path, disposable HOME/workspace/database and real
installed tmux/Bash/recorder. Group checks for ensure twice/manager reopen,
literal Unicode output and capture, two observers/control takeover and stale
frames, disconnect/reconnect while Bash and its owned child remain alive,
resizing, shell exit, failed/ambiguous launch and recorder failure. Verify no CLI
was started by setup and no input got replayed. Build/install and exercise the
representative ensure -> attach -> human command -> disconnect -> reattach ->
recorded output flow with installed bytes. Cleanup only positively owned test
processes/sockets. No provider calls or live Qorqut fixtures.

This proves normal manager/viewer process restart survival, not systemd cgroup,
machine reboot, VM, WebSocket/UI or native assistant capture acceptance. Those
remain explicit integration checks after the parallel components pass review.
