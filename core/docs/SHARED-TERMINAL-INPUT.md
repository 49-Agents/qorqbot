# D44 — One recorded-terminal gate for human and automated input

Manager decision, 2026-10-09. The existing TerminalRunner already persists paste
and Enter intent, but its run-ID lock and runner_control are separate from the
recorded shell's terminal-ID lock/control. Connect those authorities before
using it for Chat. Keep one receipt implementation and no new service.

## Frozen local interface

Extend `TerminalRunner.register_binding(binding, *, terminal_id: UUID | None =
None)`. None preserves the existing standalone contract. A supplied terminal ID
binds this exact native RunBinding to an existing recorded shell in the same
runner SQLite file. Add an immutable `runner_binding_terminals` table keyed by
run_id with terminal_id and the saved shell spec hash; use existing local schema
creation, no host schema migration. More than one historical native run may
reference a terminal; active-owner admission remains core's responsibility.

Validate before registration: the shell row exists and is not stopped, spec
agent/recovery/environment matches, saved tmux identity equals the binding and
recorder equals its pane. A core caller remains responsible for supplying the
verified native owner/ancestry. Never discover/adopt a session by name or cwd.
Persist binding and mapping together; replay requires exactly the same mapping.
Do not convert a prior standalone binding or change an existing mapping. Reject
inconsistent records with a typed error and leave all control state unchanged.

Every mapped runner input/inspection/reconciliation method uses
`input_lock(database, terminal_id)`, exactly the ShellManager gate. Read the
mapping before acquiring it and revalidate it under that gate before any effect;
a concurrent initial registration must not allow an old run-ID gate to be used.
The registration path also verifies its selected mapping under the chosen gate.
All runner-attempt inspection/recovery uses this mapping, including after a new
TerminalRunner instance starts. Legacy bindings keep their original run-ID gate.
Do not acquire the same flock recursively through ShellManager public methods.

Mapped control is read directly from runner_shell_control under this gate.
Unclaimed maps only to the old runner's internal automation eligibility enum;
it is not a declaration that native input is ready. Human maps to human with the
same epoch/viewer/sequence. Do not create/update/reset runner_control for mapped
runs. Reject TerminalRunner.control/human_input/resize on a mapped run; actual
human input and resize stay exclusively in the accepted ShellManager protocol.
Do not change its public wire protocol, human hold, input receipts or behavior.

The host composition will construct a RunBinding from the incumbent owner and
saved shell, pass terminal_id at registration, and supply positive capture,
owner/hook and composer callbacks. This assignment does not implement that
composition or invent a permissive callback default. Binding checks are not
capture readiness. Readiness callbacks still run under the shared terminal gate
and outside SQLite transactions; they may inspect original state, but must not
recursively acquire that same gate or write input.

## Bounded repaint settlement

Add optional constructor `paste_settle_timeout: float = 0.0`, finite and within
0..1 seconds. Default preserves current behavior. After the single paste,
verify fresh snapshots until exact expected input matches or the deadline expires,
using at most 25-ms sleeps and fresh external-readiness checks on each attempt.
No second paste, key, clear, Enter or background thread is allowed while waiting.
A timeout or lost proof after possible input is uncertain. The production host
will use 0.5 seconds; this is a repaint allowance, not a retry of input.

After durable Enter intent, retain the existing final fresh snapshot callback
and external-readiness check immediately before Enter. That callback is pure and
must not accept the earlier settled snapshot. An exact saved attempt returns its
receipt and never replays input; interrupted-after-intent remains uncertain.

## Builder scope and checks

Own runner terminal/storage implementation, focused owning runner tests and
`docs/evidence/shared-terminal-input-r1.md`. No adapter, messaging, host database,
Application, runtime observer, UI, browser or VM edits. Keep old standalone
callers/tests compatible and avoid another copy of the paste state machine.

Focused checks must establish mapped registration/conflict and restart lookup;
human hold/epoch refusal without paste; real cross-thread/process serialization
on the same terminal gate (one bounded owning test using existing isolated shell
helpers is sufficient); immutable saved-attempt replay; final proof loss and
settle timeout stay uncertain without extra Enter; standalone behavior preserved.
Use temporary stores/processes only. No actual provider or live-owner session.
Report exact commands, committed SHA and limitations; no full-system readiness.
