# Observe the manually started Codex session before input

Manager decision D51, 2026-10-09. The accepted
[cold-start facts](evidence/native-cold-start-manager-acceptance.md) show Codex
0.160.0 holding its native UUID-named writer lock before any prompt, while its
SessionStart hook and primary transcript do not yet exist. Claude 2.1.287 already
connects through the official startup hook. This increment admits the observed
Codex session through the existing inbox-owner boundary. It does not launch a
CLI, synthesize a hook, create a transcript or authorize first-Chat input.

## One observer on the existing setup tick

Add a small private runtime/native_startup.py helper owned by NativeSetup.
Construction is passive. After normal native configuration preparation and
inside its existing owned-workspace/lifetime/effects guard, NativeSetup.tick
calls the helper with the inspected ShellView, configured NativePrograms, the
registered agent Invocation and its directory/binding guard. No additional
thread, service, timer, credentials, public operation or host migration.

Only the existing local onbash recorded shell and pinned Codex 0.160.0 are
eligible. Claude remains hook-driven. Missing setup, terminal, proof or supported
profile leaves startup discovery unavailable; it does not prevent Terminal use
or change successfully installed configuration into a setup failure. Return a
private typed result with a fixed outcome/reason and optional owner identity.
Do not expose paths, process command lines, arbitrary exceptions or credentials.

Enumerate descendants from the exact saved Bash fingerprint through Linux
/proc/<pid>/task/<pid>/children, with at most 32 parent levels and 256 visited
processes. Inspect only those descendants. Match resolved executable paths
against the configured native programs; require exactly one configured native
root in the discovered tree and that it is Codex. Missing process facts, cycles,
an exceeded bound, multiple native roots or unsupported ancestry yield no new
connection. Reuse the existing native ancestry verification with include_start
where applicable, retaining the second-pass fingerprint/parent/executable check.
Do not use process names, timestamps, newest files or directory-wide UUID guesses.

For that exact root process, inspect at most 256 numeric descriptors. Require
one canonical .codex/thread-writer-locks/<UUID>.lock descriptor under this
shell's managed HOME. Exclude .coordination.lock. Verify its regular-file and
device/inode identity against the named lock and fdinfo, plus a held advisory
exclusive whole-file FLOCK belonging to the native PID. Follow the existing
Qorqut proof algorithm at the immutable PORTING pin where useful; preserve source
notices and never import or inspect the live installation. Do not take/release
the lock or open a new CLI/session. Never read lock payloads as session identity.
Require a unique matching UUID, then recheck descriptor/lock/process and saved
shell facts immediately before admission. There is no known primary path yet;
leave that absence explicit.

## Existing owner rules remain authoritative

Build the existing OwnerCandidate from registered agent/terminal/recovery/
environment identity, configured provider/version, observed native UUID and
exact native process fingerprint. Call Owners.connect; do not insert owners
directly. Its incumbent-wins, same-session reconnect and live/unknown/exit rules
are unchanged. A rival remains disconnected; never kill either CLI or transfer
ownership because a transport disconnected or a response finished. Unknown
proof is unavailable, not a session conflict.

For a successful observer admission, append a distinct native.startup_observed
event through existing event storage. Record agent attribution, owner_id,
run_generation, provider, cli_version, native_session_id, native_process,
terminal_id, recovery_epoch, environment_generation and evidence_kind set to
codex_writer_lock. This is a session binding observation, with no idle/readiness
or transcript-path claim. Never manufacture native.hook or SessionStart.
An ordinary subsequent real SessionStart must reconnect to that same owner.

Avoid one event or reconnection per tick: when the currently saved owner exactly
matches the freshly verified candidate and the matching startup event exists,
leave it in place. Normal owner monitoring maintains freshness. Reconcile a
crash after connect but before the observation event by recording that event on
the next matching observation. Use the host effects guard and revalidate the
active owner before the event transaction. Restart must not add another owner,
run generation or identical observation. Observer failure never closes an owner.

## Scope and checks

Own runtime/native_startup.py, the small integration in runtime/native_setup.py,
their focused owning tests, and a concise evidence report. Do not change Owners,
native hook semantics, input_dispatch, native collector, schemas or UI. A real
startup identity is not healthy capture; input_ready remains false and the
existing first-message refusal remains until the separately designed capture
bootstrap is integrated.

Group tests around exact lock/PID/ancestry proof and ambiguity, successful
incumbent/reconnect/crash reconciliation, unrelated or stale identities retained
without admission, and normal setup integration with no input/launch. Use private
synthetic proc fixtures for bounded failure cases and the existing owner tests
for ownership policy; do not duplicate their suite.

Then execute one fresh isolated startup of the pinned Codex binary, using the
completed cold-start fixture as a reference and the new installed core. No
prompt, seed, native tools/call, fake hook, fabricated UUID or transcript file.
Verify native startup connects before input, a normal core restart retains the
same owner/run and real native lock, and no native source/readiness is claimed.
Use ordinary fixture trust dialogs, preserve any refusal, and never bypass or
replay a safety block. Exit the owned CLI normally and verify owned cleanup.
The broader daemon/resume/child matrix is outside this bounded increment.
