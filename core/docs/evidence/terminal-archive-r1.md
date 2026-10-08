# Recorded Terminal host archival r1

Implementation of [D41](../TERMINAL-ARCHIVE.md) on
`codex/build-terminal-archive-r1`, based on
`daef9f8d63cd21121fb18966b0656b00513a78ee`. Manager review is pending.

`runtime/terminal_archive.py` transfers at most four original chunks per
observation from the existing terminal database. It validates the saved shell,
source metadata and original recording run; registers that exact terminal
producer using the agent's Invocation; and saves each intent and matching host
receipt before ACK/reclaim. Host conflict/recovery holds stop cleanup even after
local ACK. An empty pending page cannot authorize reclamation. Connections are
call-local and the host lifetime/effects guards cover receipt settlement.

`Terminals.observe` invokes it under the existing workspace/provisioning guards,
keeps transfer errors separate from shell availability, and refreshes capture
counters before publishing. Stopped or unprobed recordings can drain without a
native owner or relaunch. Recording health is preserved. No native collector,
capture, setup/hook, host migration, operation, dependency or UI contract changed.

Focused checks use the unchanged lock in a private Python 3.12 environment:

```text
PYTHONPATH=src:tests <venv>/bin/python -m unittest runtime.test_terminal_archive -v
PYTHONPATH=src:tests <venv>/bin/python -m unittest runtime.test_terminal_archive.TerminalArchiveObservationTests transports.test_terminal.TerminalTests.test_console_recording_control_authentication_and_restart -v
git diff --check
```

Six synthetic groups passed: exact binary/multiple-chunk transfer and metadata,
stopped/error capture drain, interruption at intent/receive/receipt/ACK/reclaim/
retirement, identical retry, all receipt binding fields, conflict/recovery holds,
missing receipts/identity mismatch, empty-page retention and host guards through
local ACK. The real-shell observation/failure group and affected existing
Terminal control/authentication/restart group also passed. Existing byte
assertions now follow immutable chunk coordinates to the host copy after local
reclamation; their behavior assertions are retained.

The first real-shell run failed because its private Python environment had only
the dependencies installed; the isolated recorder could not import core. After
installing core there, both affected groups passed (2 tests, 19.863 seconds).
Both failed-fixture tmux processes were identified by the exact private Python
launch arguments and terminal IDs, fingerprint checked, and terminated through
pidfds; neither had children or a remaining socket. No production fix was needed
for this harness setup error.

Private commands/logs and the copied completed Terminal helper are retained in
this lane's `build-terminal-archive-r1` evidence directory. Prior integration
evidence remains unchanged. A pinned normal installed-server outage/catch-up and
stopped-shell drain check is the remaining implementation validation and will
be recorded here before submission. No full G3, backup, native-provider, merge
or deployment acceptance is claimed.
