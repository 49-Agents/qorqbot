# Terminal control follows the open view

Decision D56, 2026-10-09. The owner requests direct Terminal input with no
View only / Take control / Release control toolbar. This supersedes D38's
retained browser-viewer hold on disconnect, not native-session inbox ownership.
No automatic CLI launch, new auth, schema, public operation or protocol version.

UI opens the existing socket and sends one control/take after a valid hello.
Only the matching successful reply enables input and viewport resize. A new
Terminal visit/reconnect is the user's request to use that terminal, including
replacing another viewer using the existing observed-epoch control operation.
Do not automatically fight later takeovers or replay uncertain input. Show a
small error with Reconnect terminal on conflict, lost control or disconnection;
normal successful control has no toolbar/status row. Chat/agent navigation
closes that view's socket. The core close path releases only that stream's own
human hold, after its outstanding input/control effect has settled.

## Core implementation

Extend TerminalStream.close, keeping ownership of final attachment cleanup.
Under its existing stream lock, and the existing live binding/effect gate,
read the current exact terminal control. If human control belongs to this
viewer, release it with that current expected epoch using existing runner
release semantics and publish the resulting terminal observation. An unrelated
viewer, unclaimed control, changed target, unavailable runner or failed release
must never be replaced or reported as released. Do not retry a raced release.
Always close the owned attachment and remove stream registration in finally,
including repeated close and cleanup failure. Do not require attachment.closed
false: a transport can disconnect before the owned hold is cleaned up. Keep
runner-level attachment.close semantics unchanged; only core's managed browser
Terminal stream adopts this behavior. No shell, native CLI or recording stop.

A lost network socket releases when the server detects and closes it. Backend
unavailability cannot promise immediate release; reopening Terminal takes the
current epoch. Native inbox owner lifetime is unchanged. Preserve all framing,
input ordering/receipts, binding checks, recovery and capture behavior.

## Verification and rollout

Two parallel Astra MAX implementations: core cleanup and UI simplification.
Manager reviews the finished work directly. Core adjusts only the existing
owning Terminal transport/runtime tests for release-on-close and adds a compact
regression where an old closing viewer cannot release its replacement. Verify
real local socket disconnect, no CLI stop, and repeated close; keep the input
cancellation/backpressure checks. Do not duplicate suites or test owner data.

The UI presentation is specified in qorqutbot-ui docs/UI-SIMPLIFICATION.md.
After paired acceptance, publish to the existing local test URL. Preserve its
installation, native process, tmux and history. Test the real combined flow in
disposable data rather than typing into the owner's now-active native session.
