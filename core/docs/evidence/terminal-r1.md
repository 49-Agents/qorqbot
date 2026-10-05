# M03 terminal r1 evidence

Bounded local primitives on `codex/terminal-r1`, based on
`db25fc97a4127f8f46f3092ed96578bcdba685ce`. Initial status was clean.
This increment does not complete M03 or any G0–G6 gate.

The Manager's ACLA message 6 adds D23's automated terminal-text rule, recorded
in coordination commit `688590ab9419dbd4171bb1a8fa8cec0855849067`. The shared
documents were neither copied nor edited in this worktree. Automated text
rejects C0 except LF/TAB, DEL and C1 (including CR and ESC), and unencodable
Unicode, with `RunnerError.code == "invalid_argument"` and structured
`reason == "unsafe_terminal_text"`. Validation precedes attempt creation and
all tmux operations. Bytes are never normalized, escaped or truncated. The
64 KiB limit counts exact UTF-8 bytes. Interactive key frames remain separate.
Later messaging integration owns retaining the original message and recording
permanent delivery failure for unsafe text; it is not implemented here.

## Implemented boundary

- Immutable Pydantic bindings include agent/recovery/environment/boot/run
  generations, socket device/inode, tmux server/session/window/pane IDs, and
  recorder/native process fingerprints. Linux checks use boot, PID, start
  ticks, UID and PID namespace; zombie/absent/reused processes fail verification.
  Inspect validates an already supplied binding; it never starts or searches
  for a process by name. Observation rechecks identity after screen capture.
- Tmux operations use an explicit socket and structured subprocess argv.
  Payload is stdin to a unique buffer, followed by bracketed `paste-buffer -r
  -p` and a separate normal Enter. Effects recheck Linux identity and tmux's
  server/session/window/pane identity and input flags in its command queue.
  There is no shell payload interpolation, default socket or CLI launcher.
- `runner_*` tables share the caller-configured eventual recorder spool file.
  `ensure_schema(connection)` is idempotent, preserves global `user_version`
  and other tables, and refuses an already active transaction. Connections use
  WAL, foreign keys, FULL synchronization and a 5-second SQLite busy timeout.
- Exact attempt ID, binding, payload hash/length, epoch and unique buffer name
  are durable before terminal input. A separate durable dispatch barrier
  precedes each possible paste/Enter. Receipts distinguish deferred, submitted
  and uncertain. A pre-effect crash recovers as deferred; an unfinished
  dispatch recovers as uncertain. Same-ID retries return saved receipts;
  payload/binding/epoch conflicts fail without input. A committed submitted
  receipt survives a crash before the caller receives it. Submitted only
  means the transport sent Enter, not semantic processing or handling.
- A private per-run `flock` covers admission, effects, control changes and
  receipt persistence. SQLite transactions do not span effects/callbacks.
  Inspection of unfinished attempts obtains that lock before recovery, so
  it cannot classify a still-running dispatcher as crashed. Persisted human
  mode/epoch/viewer/sequence survives reconstruction. Explicit take/release
  advances epochs; stale viewers/frames fail. Release requires a fresh empty
  composer and never clears typed text. Observing changes no ownership.

## Callable integration seam

`TerminalRunner(database, tmux=..., capture_ready=..., classify_composer=...,
verify_paste=..., machine_hold=...)` is a local Python interface, not a frozen
network protocol. `register_binding(binding)` accepts the future lifecycle
owner's exact trusted launch receipt and persists it; it does not discover or
adopt processes. `inspect_binding`, `inspect`, `inspect_control` and
`inspect_attempt` expose saved identity, live observation and receipts.
`attempt`, `control`, `human_input` and `resize` are the serialized entrypoints.
The lower-level `Tmux` effects require the caller's input lock.

The recorder readiness callable must return literal `True` for this binding.
The composer callable receives the binding and a styled terminal snapshot and
returns `empty`, `busy`, `blocked` or `unknown`. Busy/typed/modal/unknown are
not admissible. The paste verifier receives binding/snapshot/exact SHA-256 and
must positively confirm that this owned paste is present and normal Enter is
safe; its implementation owns bounded native settling. Missing callables,
unconfirmed capture or missing proof never default to ready. No adapter imports
or native composer recognition are supplied in this increment.

`machine_hold(binding)` is an explicit read of the external hold; only literal
`False` admits input. It is rechecked at effect boundaries. The higher-level
coordinator must establish its durable hold first and then acquire the exported
`input_lock` before acknowledging quiescence. Run-local methods cannot mutate
or clear that hold. All cooperating users must use the same canonical database
path and its adjacent private `.runner-locks` directory; never unlink live lock
files. Stop/replacement and environment-wide reconciliation remain upstream.

Same-ID receipts, including deferred receipts, are replayed without input.
The future M05 coordinator owns whether another distinct attempt is allowed
after an uncertain message delivery; this library has no message identity or
message-reconciliation workflow. Human frames consume their sequence before
effects and cannot be replayed after uncertainty or reconnect.

## Verification

Command, with inherited user/provider environment removed:

```text
env -i PATH=/usr/bin:/bin PYTHONPATH=src tests/runner/.venv/bin/python -m unittest discover -s tests/runner -v
```

Result on 2026-10-07: **21 tests passed**, no skips. The staged diff also passed
`git diff --cached --check`.

The focused suite covers:

- Actual isolated tmux: Unicode/multiline/LF/TAB and shell-like literal bytes,
  exact 65,536-byte Unicode payload, oversize rejection, bracket delimiters,
  separate Enter, raw human input, resize and owned buffer cleanup.
- Invalid boot/run/recovery/environment/socket/pane identity and synthetic PID
  reuse/death; identity change between metadata and capture is rejected.
- Missing/negative readiness, busy/blocked/unknown composer, capture/hold loss,
  pre-load deferral, and post-paste/Enter/receipt failures and replay.
- Disposable dispatcher processes exit at prepared intent, buffer load,
  pre-paste barrier, completed paste, Enter, and either side of final receipt
  commit. Fresh runner objects recover without any repeated input bytes.
- Concurrent processes attempting input/replay/receipt inspection/control
  wait through effect and receipt. Distinct attempt buffers do not interleave;
  conflicting attempt IDs on different run locks have only one dispatcher.
- Persistent human control, typed-text release deferral, explicit takeover,
  stale epoch/viewer/sequence rejection, uncertain human frame consumption,
  external hold retention, and invalid resize rejection before sequence use.
- Every prohibited C0/DEL/C1 character, CRLF, embedded paste terminator plus
  CR, and invalid Unicode are rejected with no tmux call and no attempt row.
  Interactive ESC/CR frames remain available to the current input viewer.

The receiver is a deterministic raw Python process, not a native CLI or the
recorder. Its test binding uses that single process in both recorder/native
slots solely for the harness. Each fixture creates its own temporary private
tmux socket/session and files; cleanup verifies ownership, kills only that
session, and verifies its processes exited. Nothing attaches to a personal CLI,
shared tmux server, CDP/VNC endpoint or owner-account session.

Observed versions: Python **3.12.3**, SQLite **3.45.1**, tmux **3.4**, Pydantic
**2.13.5**, pydantic-core **2.46.5**, Linux **6.8.0-138-generic**.
`/usr/bin/tmux` SHA-256:
`034b15c64035f783d43862f2775eb4828f61571ca62c8199796000b97d556ecd`.
Python dependencies were installed only in ignored `tests/runner/.venv` using
`python3.12 -m venv` and that environment's `pip install 'pydantic>=2,<3'`.
No competing root package manifest or dependency lock was created.

## Source provenance and limits

The code is a small new implementation informed by the pinned P04/P05 sources:
core `e71fd1e527368bfd41b9815126f4bf33452aeebb`,
`bazarlyk/sandbox/tmux_session.py`, `sandbox/tmux.py`,
`transport/agent_terminal_control.py` and `transport/terminal_composer.py`;
dashboard `fd9c1a924db70373eb8d06f3970a87c26bd84452`, `pty_broker.py`.
Read the source's process checks, load/paste/Enter ordering, restart/resize
mechanics and pre/post-effect failure handling before implementing. Retained
concepts are unique stdin-loaded buffers, separate normal Enter, process start
identity, fail-closed composer admission, and uncertain-effect handling. No
provider recognizer, PTY broker, business policy or source runtime package was
copied/imported. Read file headers and searched those pinned roots for
LICENSE/NOTICE/COPYING files; no applicable notice text was found or removed.

Unperformed: real recorder/adapters integration, native Claude/Codex
compatibility or provider calls, managed CLI launch/stop/replacement, HTTP/WS
viewers, actual runner-service/systemd restart, independent cgroup supervision
and producer pause, VM/onbash deployment, desktop/browser/machine takeover and
environment-wide hold coordination. The subprocess crash harness proves local
receipt recovery, not service/cgroup survival or recorded native execution.
No global acceptance gate, complete archive or whole-module readiness is claimed.
No merge, deployment or release is authorized by this evidence.
