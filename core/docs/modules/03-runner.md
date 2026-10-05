# M03 — Runner, tmux and terminal delivery

Status: Planned. Owns R03/R04/R05/R07 execution boundaries. Depends on M00–M02 and shared protocol; integrates M04 adapters, M05 messages and M07 recorder.

Repository: `qorqut-core`. Install the small runner from core's package; use a versioned client of the independent local browser service for Chrome/control/relay effects. Browser does not own tmux, message routing or the runner service. Its direct MCP calls and runner-proxied commands share the browser service's own command/control gate.

## Outcome and boundary

One small runner service manages local tmux/process bindings, execution/capture receipts and forwarding to browser's independent control service. It runs inside each VM and locally for onbash. The host remains the operational authority; guest-local stores hold execution/capture evidence, not another independent agent/task registry. The runner does not execute browser actions itself.

The runner exposes the versioned routes in [CONTRACTS.md](../CONTRACTS.md). Ordinary HTTP/WS transport is sufficient. Use bounded bodies and backpressure; terminal viewers do not become an unbounded output queue.

Resolve explicit surface IDs for locally hosted Chrome and, for onbash, the separate general desktop. VM graphical console transport stays with M02/libvirt. M06 machine takeover acquires coordinated terminal/browser holds and must not leave a competing writer after partial failure. The small file-inspect route supplies metadata for M09's path-only reference tool inside the exact target environment; it never interprets a VM path on the host or executes shell-interpolated path text.

Check the persisted agent/environment machine-control admission hold before any new managed launch or automated input, even when no old run exists. A CLI stop/run change cannot clear it. A confirmed absent participant may be reported quiescent during machine takeover; unreachable/unknown participants may not. Machine holds are distinct from a human terminal viewer's exact-run control epoch.

## Managed run identity

Before launch the host allocates run ID/generation and passes structured argv, cwd, environment and generated-config hash. The runner verifies agent/environment/boot identity and records the intention. `ensure` returns a matching saved/live run on retry; it does not start a second CLI.

The tmux session launches the M07 recorder, which owns the native CLI's child PTY. Binding includes boot ID, tmux server/session/pane identity, recorder process identity and native process start identity. PIDs and session names alone are insufficient. Native session ID is added when M04 proves it.

One live or unknown run owns an agent input route. Startup/capture failure after a process launched does not release ownership; only verified exit does. A timeout leaves process existence unknown. Start/restart/switch check current agent lifecycle intent before effects. `runtime_stop` stops only the CLI; restart means stop-confirm-exit followed by a new run, preserving the environment/browser.

Launch the agent-owned tmux server and recorder/native process tree in independently supervised runtime units outside the runner/host-service cgroups; a daemonized child alone still belongs to its parent's service cgroup. Keep desktop supervision separate so stopping a CLI does not stop Chrome. Disable automatic model-process restart. Use the whole owned runtime tree when proving termination, including detached descendants. Keep recorder/runner runnable when storage pressure pauses only the producer subtree; prove this cgroup layout in P0.

## Terminal mirroring and human control

Attach disposable PTY/WS mirrors to existing tmux. Disconnecting the browser or restarting the host/runner service preserves tmux/recorder/CLI. The ring buffer is a viewer convenience; M07 is the durable archive. A slow viewer may reconnect from a bounded display snapshot without affecting archival capture.

Serialize resize and input against the exact run. `terminal_control(action=take|release)` checks expected run/control epoch and reconciles through `POST /v1/terminal/control` under the same local input lock as paste. Each transition advances the epoch; human mode binds one input viewer. Automated input pauses during transition and human ownership. A stale same-run viewer cannot type after control changes.

Observation alone does not acquire control. A disconnected input viewer leaves human mode inspectable; v1 has no automatic expiry. Explicit takeover binds a new viewer/epoch; explicit release revokes the old viewer and permits automation only after runner confirmation and a fresh composer check. Never clear human text. Host/runner restart recovers the saved mode/epoch before admitting input. Ephemeral key/resize frames carry connection sequence and exact control binding; they are not replayed on reconnect.

Direct human keystrokes are not automatically replayable. A reconnect cannot resend an uncertain key frame as if it were a durable message. Owner-originated complete messages use the same durable attempt path as agent messages when possible.

## Guarded automated input

Each attempt carries ID, payload hash, expected run binding and submit intent. Under the per-run local input lock:

Before creating an attempt or touching tmux, validate the UTF-8 size and the
automated-text character contract in CONTRACTS section 6. Unsafe control text
returns `invalid_argument` / `unsafe_terminal_text`; no dispatch intent, paste,
Enter or transient deferred receipt is created. Preserve the supplied body at
the message layer rather than modifying its bytes. Raw human input remains a
separate epoch-bound control path.

1. Check current binding, capture readiness, terminal control mode and adapter composer readiness.
2. If text/dialog/unrecognized shell makes delivery unsafe, return deferred with no input.
3. Durably record dispatch intent before the first paste.
4. Paste through a unique tmux buffer containing exactly one explicit bracketed-paste frame around the validated body, using raw non-converting paste. Do not rely on conditional `paste-buffer -p` framing or interpolate the message into shell source. Keep the durable payload hash/size about the original body. Adapter eligibility must establish a supported native composer; framed bytes alone do not prove that compatibility.
5. After the durable Enter intent and its possible database wait, refresh binding and exact-paste/safe-Enter evidence immediately before a separate normal Enter. A verifier that settles must refresh its own evidence after waiting. After that final verification returns, recheck capture readiness and the external hold as prompt observations before admitting Enter; an earlier check cannot survive the verifier's wait. Do not add another deliberate wait or database write before the effect. Failed proof/readiness after possible paste is uncertain without Enter or automatic replay. See D24.
6. Persist deferred/submitted/uncertain/failed outcome and return the saved receipt on retries.

By default automatic delivery waits while a turn is busy. Add an adapter-specific “queue while busy” behavior only after its exact native composer contract passes tests; it is not required for v1 completeness. Any doubt after first input is uncertain. Semantic message handling belongs to M05.

## Implementation sequence

1. **M03.01 Local library/protocol:** extract process fingerprint and tmux primitives with injected subprocess boundaries; implement identity/version/mount readiness.
2. **M03.02 Managed sessions:** implement exact run ensure/inspect/stop plus persistent receipts and one-run ownership.
3. **M03.03 Recorder integration:** start every managed CLI through M07's wrapper and verify transcript capability before autonomous work admission.
4. **M03.04 Viewers:** add PTY/WS attach, bounded screen buffering, resize and human-control state.
5. **M03.05 Input delivery:** port guarded composer and buffer paste behavior; persist pre-effect intent and replay-safe outcomes.
6. **M03.06 Reconnection:** runner restart reattaches existing process bindings and resumes event/capture cursors served to the host collector; protocol mismatch fails only dependent actions with useful diagnostics.

## Source decision and acceptance

P04/P05: narrowly extract Qorqut's PTY broker, standalone tmux helper, input sequencing, process evidence and composer fixtures. Remove dashboard/business globals and role-specific commands.

Required tests include real isolated tmux input bytes through the public 64 KiB message limit, Unicode/multiline/bracketed paste, resize, actual systemd restart/process-cgroup survival, viewer reconnect, PID/name reuse, target switch during delivery, stale same-run control epoch, human typed text/modal composer, recorder failure, and crash before/after paste/Enter/receipt. Saved screen fixtures prove recognition rules; they do not replace a native CLI compatibility test.

**Done:** G2 proves durable run ownership and correct uncertain input; host and VM targets pass the same runner contract. No automatic launch/delivery can bypass recorder readiness or address a replacement run accidentally.
