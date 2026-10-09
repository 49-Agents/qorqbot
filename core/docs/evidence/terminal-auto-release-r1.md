# Terminal view close releases its control

2026-10-09. Implements the core cleanup in [D56](../TERMINAL-AUTO-CONTROL.md)
on `codex/build-terminal-auto-release-r1`, based on
`6f8da96258028ea5e0e26ef800fe3f855c004add`.

`TerminalStream.close` waits for its existing effect lock and validates through
the existing application binding/effect gate. A fresh live shell observation
supplies the exact viewer and epoch for one runner release attempt. Only a
confirmed release updates the cached Terminal observation. An already closed
mirror does not prevent this cleanup. Repeated close does not retry release;
attachment cleanup and registration removal run in nested `finally` blocks.
Runner attachment behavior and shell/native/capture lifetimes are unchanged.

Verification used Python 3.12.3, tmux 3.4 and a private editable installation;
all 32 distributions in `requirements.lock` matched. The owning group ran once:

```text
PYTHONPATH=src:tests <private-venv>/bin/python -m unittest -v transports.test_terminal
Ran 7 tests in 34.165s — OK
git diff --check — passed
```

The existing real local WebSocket flow now verifies release on disconnect,
published control state, restart persistence, and survival of the same recorded
Bash, recorder, tmux and owned child. Its changed-tmux-binding case now holds
control before invalidation and verifies that failed validation retains it.
Existing recovery, uncertain input, cancellation, blocked runner control,
slow output and teardown checks remain. A blocked control call finishes before
close attempts release.

One additional grouped regression covers an old viewer closing after takeover,
release after its replacement's mirror has already closed, repeated close,
and a synthetic release conflict combined with attachment-cleanup failure.
It verifies no release retry or false publication and removal of registration
even when attachment cleanup raises. Recovery refusal also verifies cleanup
without changing the prior hold.

Private logs are retained in the assigned evidence directory as
`terminal-tests.log`, `dependencies.log`, `editable-install.log` and `versions.txt`.
The test log includes expected injected observation/release warnings and asyncio
slow-task diagnostics. All processes, ports and data belonged to disposable
fixtures; no owner session or model inference was used.

Release remains best effort when the runner/live binding is unavailable, recovery
has changed, or application shutdown has already closed effect admission.
Cleanup does not bypass those gates or clear a different viewer's hold. This is
core source verification evidence; Manager owns the installed paired browser/UI
smoke, integration and deployment. No merge or deployment was performed here.
