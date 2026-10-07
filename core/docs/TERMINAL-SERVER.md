# Recorded Terminal in the host application

Manager decision D38, 2026-10-08. Implement the first usable onbash Terminal
through the normal core server, in parallel with the UI's Chat/Terminal switch.
Integration correction 217 is specified in [Terminal geometry](TERMINAL-GEOMETRY.md).
It supersedes the original display-size behavior and upgrades the corrected
pair's stream to protocol 2; other D38 requirements remain.
Use the accepted `ShellManager`; do not replace it or start a native CLI.
The [accepted connection evidence](evidence/manual-runtime-seams-manager-acceptance.md)
informs subsequent native startup and history work. This increment exposes the
real recorded shell; it does not pretend those later connections already exist.

## Host composition

Add `local_terminal: LocalTerminalSettings | None = None` to installation
settings. The optional record contains explicit absolute `runtime_root`,
`python_executable`, `tmux_executable`, `bash_executable`, and the explicit `path`
string passed to `ShellSpec`. Validate paths and the resulting socket length
before creating resources. Missing configuration disables this capability;
opening or initializing `Application` alone never starts a shell. Preserve old
installation files and defaults; do not rewrite them merely to add a null field.
Review clarification 202: preflight the complete UTF-8 bytes that would be saved
to `installation.json`, including formatting/newline, against the existing
16 KiB read limit before initialization creates directories, acquires its lock
or writes a database. Reuse that exact encoding for saving. An individual PATH
character bound cannot establish that the complete installation will reopen;
keep the existing read limit and reject oversized input without partial setup.

`Application.terminals` is a small application service, implemented in
`runtime/terminals.py` with an owned models/helper file if needed. It owns target
selection, cached observation, attachment admission and control delegation.
The HTTP/WebSocket layer only authenticates, validates framing and delegates.
No new daemon, database or host migration is required. Host schema stays 6 and
the five existing typed-job handlers stay unchanged.

The normal console lifespan starts one supervised background loop after opening
the application. At most once per second it selects the reserved onbash row and
the current installation epoch, outside any process/filesystem effect. Once the
existing agent workspace has a complete matching ownership marker and a durable
`workspace_initialized_at`, it creates/verifies these locations and calls
`ShellManager.ensure` with that exact binding:

| Input | Fixed location/value |
| --- | --- |
| Workspace | `data_root/agents/<id>/workspace` |
| Managed HOME | `data_root/agents/<id>/runtime/home` |
| Existing runner/spool database | `data_root/agents/<id>/runtime/terminal.sqlite3` |
| Dedicated terminal runtime directory | `local_terminal.runtime_root/<agent UUID hex>` |
| Dedicated tmux socket | That runtime directory's `tmux.sock` |
| Environment generation | `1` for this first fixed local onbash environment, persisted in its ShellSpec |
| Capture producer | The terminal UUID, as in D37; never a native-session owner |

Reuse workspace ownership validation, including the current registered agent,
installation and creation binding. A small reusable context/helper in
`agents/workspace.py` is authorized; do not add a competing marker format or
change its existing provisioning/projection behavior. Use the existing owned
directory and shell claims; sync newly created parent entries before publishing
success, including retry paths. Filesystem/process work stays outside host SQL
transactions. Do not reseed authored files or copy host login/configuration.

This local path supports only reserved onbash. Ordinary agents return
`unsupported_target` while VM selection is open; never run them on the host as a
fallback. Keep provisioning's remaining resource dependency truthful rather
than marking browser/environment setup complete because Bash exists.

Repeated ensure uses the same durable shell attempt. A stopped, changed or
uncertain binding is reported, never automatically replaced, killed or adopted.
No host environment-rebuild operation or generation allocator is introduced.
Changed recovery epoch/configuration cannot authorize another shell or rebind
an old one. Normal application/server close stops its observation loop and
viewers only; recorded Bash, its child and tmux continue. There is no public
shutdown/restart command in this increment. Explicit owned shutdown remains
available to test cleanup through the existing local API.

Cache immutable observations under a thread lock. `terminal_get` is a new
read-only operation available equally through HTTP, MCP and the operator client;
the registry now contains 16 operations. Its only argument is `agent_id: UUID`.
It reads the registered identity and cached view without probing processes or
starting a shell inside an operation transaction. A cache older than five
monotonic seconds becomes unavailable; no cached healthy result grants input.
The service validates identity/epoch/lifecycle again and performs a fresh exact
shell check when attaching or applying controls. Retain existing D37 effect
gates and uncertain-input semantics.

## Public observation

`terminal_get` returns these fields, also used by WebSocket `state`:

| Field | Contract |
| --- | --- |
| `agent_id`, `recovery_epoch` | UUIDs of the current registered target |
| `status` | `disabled`, `waiting_workspace`, `available`, `stopped`, `unavailable`, or `unsupported_target` |
| `terminal_id` | UUID when a terminal attempt is known, otherwise null |
| `observed_at` | UTC timestamp for the observation, otherwise null |
| `reason` | Bounded machine-readable reason, at most 100 characters; no exception/path dump |
| `control` | Null, or D37's `mode`, `epoch`, `viewer_id`, `last_sequence` |
| `recording` | Null, or `state`, `local_captured_offset`, `host_archived_offset` |

Recording state is `starting|healthy|blocked|incomplete|unknown`. Preserve the
actual saved/probed offsets; local recording is not host archival or a complete
native transcript. `available` requires a currently verified running shell and
recorder; uncertainty must remain visible. Do not expose executable/socket
paths, fingerprints, environment values or credentials in this presentation view.
`runtime_get` and its `input_ready=false` remain unchanged: Bash is not a native
inbox connection. Missing settings return `disabled` without filesystem effects.

The loop's health joins `/health/ready`; disabled is healthy. A blocked target
is an inspectable domain state, not a dead server. Unexpected loop death makes
readiness fail. Shutdown joins in-flight work before closing application storage.

## Authenticated Terminal stream, protocol 1

Add a WebSocket route `/api/v1/terminal/{agent_id}`. It accepts no query parameters
and no path/user-selected commands. Require the existing valid browser session
cookie, exactly one allowed Host and Origin, and no Authorization/identity
override header. Reuse the existing header/cookie rules. Reject before acceptance
or attachment on invalid identity, origin, target, capacity or server shutdown.
Unknown WebSocket routes never bypass this boundary. All registered actors keep
v1's unrestricted application policy; there is no new role/permission system.

The server allocates a fresh viewer UUID per accepted connection and opens a
logically writable `ShellAttachment`. Its tmux client remains physically
read-only as required by D37; no input is accepted before explicit human control.
The browser receives raw output as binary frames of at most 64 KiB. UTF-8 and
escape sequences may span frames; preserve bytes rather than decode each frame
independently. Text frames carry the following strict JSON messages:

| Direction/type | Fields beyond `type` |
| --- | --- |
| Server `hello` | `protocol: 1`, `viewer_id`, `terminal` (the public view above) |
| Server `state` | `terminal` (same view, at least once per second while connected) |
| Client `control` | `action: take|release`, `expected_epoch` |
| Server `control` | The resulting `control` record |
| Client `input` | `epoch`, positive `sequence`, `data` (strict base64, 1–65536 decoded bytes) |
| Client `resize` | `epoch`, positive `sequence`, `columns`, `rows` (existing runner dimension bounds) |
| Server `frame` | `kind: input|resize`, plus D37 `ShellFrame` fields |
| Server `error` | `code`, bounded `message`, `related_type`, nullable `sequence` |

Reject unknown fields/types, booleans as counters, invalid base64 and oversized
messages before effects. Maximum text-frame size is 96 KiB. The connection owns
its viewer/agent/terminal IDs; clients cannot override them. Control calls use
existing `take_control`/`release_control`; input and resize use the attachment's
durable epoch/sequence gate. A submitted frame is not proof of shell/native
processing. Do not retry input, resize or uncertain control changes automatically.

There are at most 16 active streams per server and four per agent. Have one
bounded output writer; do not accumulate output behind a slow browser. Read a
chunk only when the previous send settles, with a five-second send deadline.
Serialize JSON and binary sends. Slow/disconnected viewers are closed and
discard only their live view; recording continues without them. A transient
no-data read is not EOF: consult `attachment.closed`.
An admitted input/control/resize call may wait for a runner gate. It must not
make this output loop wait for the stream's effect lock: skip that read using a
nonblocking acquisition, then continue bounded state/session checks. No extra
reader queue or modification to the frozen ShellAttachment API is needed.

Recheck cookie validity on every received frame and at least once per second,
including output-only connections; logout, expiry and server shutdown close
viewers. Recheck the installation/target binding before effects. A recovery or
target change closes the stream without taking over another terminal. Do not
block the event loop with runner/process calls. Settle in-flight input and owned
attachment cleanup across task cancellation, using the existing shutdown style;
never close storage/fds while their admitted effect is still running.
Review clarification 202: mark endpoint shutdown before cancelling its child
tasks, and make reply delivery stop when its writer/endpoint is stopping. A
settled RunnerError after cancellation must not enqueue an error and wait for
an already cancelled writer. Pending reply futures cannot outlive that writer.
Keep the shared lifetime helper unchanged; this endpoint owns its reply/stop
coordination and still joins admitted effects before closing the attachment.

The stream's observation gate must also require the same currently available
terminal/capture binding, not only matching UUIDs. An unavailable, stopped or
stale observation closes the viewer. Check that cached gate before another
binary read and in the independent watch path so a tmux topology change cannot
keep displaying another pane after core observes loss of the exact binding.
This is bounded observation, not atomic verification of every output byte.
Discard the viewer only; do not repair tmux topology, transfer control or stop
the recorded shell. Preserve the established one-second observation and
five-second cache-expiry bounds.

Disconnect, expiry, logout and view changes preserve the durable human hold.
Explicit release/takeover changes it; no timer releases it or makes Chat ready.
Reconnect allocates a new viewer and never replays queued keystrokes. Rendering
and protocol-response bytes from a terminal emulator are not human input and
must not be sent to the raw pane-input path.

Use Starlette's WebSocket route and pin `websockets==17.2` in project/lock inputs
for Uvicorn's `websockets-sansio` backend. Keep the other locked distributions
unchanged. Preserve script CSP; allow inline **styles only** for xterm's runtime
renderer styles (`style-src 'self' 'unsafe-inline'`). No inline script/eval or
external viewer asset host is introduced. These pins were checked against the
package registries; actual installed-server compatibility must pass below.

## Owning checks and files

Core builder owns the new service/models, narrow application/config/server/
boundary assembly, that workspace validation helper, dependency lock and owning
tests/evidence. Existing shell, recorder, native adapters, ownership, messaging,
conversation and host migration contracts are read-only. Ask Manager if a frozen
primitive prevents the specified composition; do not silently redesign it.

Use a few grouped tests at this new boundary: disabled/ordinary-agent no-effect
behavior; real installed onbash creation-to-recorded-Bash and `terminal_get`;
two cookie viewers, literal Unicode/control-key input, resize, explicit takeover
and stale-frame refusal; missing/foreign Origin and revoked sessions; slow/
disconnected viewers; changed recovery binding and uncertain input; normal
console restart preserving exact Bash/owned child, recording and human hold.
Verify setup starts Bash only and no CLI. Existing owning runner tests need not
be duplicated. Mechanically update operation inventories to 16, preserving all
behavioral assertions. Host schema 6 and five handlers must stay unchanged.

Run the normal installed `qorqut-core serve` entrypoint on isolated ports with a
short private socket root, disposable agent data/HOME and real tmux/Bash/recorder.
Verify imported wheel bytes, capture content and process identities across
restart; never use live Qorqut or owner sessions. Cleanup only positively owned
test processes. No native/provider calls, VM/browser work, usage examples or
deployment. Record commands/results/limitations in
`docs/evidence/terminal-server-r1.md`, then commit/push the assigned branch and
stop for fresh independent Astra MAX review. Actual compiled UI/core acceptance
follows both independent source reviews; synthetic UI checks do not replace it.
