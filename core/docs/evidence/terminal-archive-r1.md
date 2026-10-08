# Recorded Terminal host archival r1

Implementation of [D41](../TERMINAL-ARCHIVE.md) on
`codex/build-terminal-archive-r1`, based on
`daef9f8d63cd21121fb18966b0656b00513a78ee`. The implementation/test candidate is
`3994fae36ba2411aba19330853d99873e0766ffc`; the subsequent evidence completion
changes only this document. Manager review is pending.

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

The normal installed-server flow passed against a Git archive of that exact
candidate, with a fresh disposable installation, managed HOME, private user/
network namespace and loopback port. It used the normal generated
`qorqut-core serve` entrypoint twice, without UI assets or native programs.
Initialization registered onbash passively; the normal worker provisioned its
workspace and the observer started its recorded Bash. No job pumping, manual
observation/transfer call, native owner or export command drove archival.

Build and acceptance commands, abbreviating only private paths:

```text
git archive --format=tar --output=<private>/core-export.tar 3994fae36ba2411aba19330853d99873e0766ffc
tar -xf <private>/core-export.tar -C <private>/core-source
<venv>/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir <private>/wheels <private>/core-source
<venv>/bin/python -m pip install --force-reinstall --no-deps <private>/wheels/qorqut_core-0.1.0-py3-none-any.whl
unshare --user --map-root-user --net /bin/sh -c '/usr/sbin/ip link set lo up && exec <venv>/bin/python <private>/installed-flow.py'
/usr/bin/python3 <private>/verify-imports.py installed-attempt-2
```

Both core processes used the identical installation and transport configuration,
clean fixture HOME and installed Python, with no source import override.
Python 3.12.3, tmux 3.4 and Bash 5.2.21 ran the flow. All 32 distributions matched
the unchanged lock. All 237 Git-exported files remained unchanged, and all 77
packaged files matched source, wheel and installation. Both console logs showed
73 core modules imported exclusively from the installation, with completed
startup and shutdown. Wheel SHA-256:
`60de9ab02338da79d5a21157424b7a26c830d27360c114d5b8948b82028b86c9`.

| Normal-server phase | Local bytes | Host bytes / local ACK | Retained payload bytes | Chunks |
| --- | ---: | ---: | ---: | ---: |
| Initial recording, no viewer | 18 | 18 | 0 | 1 |
| Before core outage | 116 | 116 | 0 | 3 |
| Output written with only core stopped | 211 | 116 | 95 | 5 |
| Automatic catch-up after restart, no viewer | 211 | 211 | 0 | 5 |
| Bash exited normally, final bytes drained | 297 | 297 | 0 | 7 |

Three trusted local attachment frames wrote unique initial/outage/final markers
and an append-only effect file. The outage frame targeted the surviving exact
Bash with core stopped; the attachment closed before core restarted. Every
marker occurred once and the effect file had exactly its three intended lines.
Core restart preserved terminal/spec/source/epoch, original recorder/Bash/tmux
fingerprints and the epoch-2 human hold with sequence 1. Final Bash exit was 0,
capture recorded `drain_complete=1`, Terminal reported stopped and its remaining
bytes archived automatically. No CLI, native owner or Chat entry was created.

Every chunk's original offset, length, SHA-256 and bytes matched the immutable
host object. There was one source, epoch, recording run and run link, with one
event for each registration fact (three events total), attributed to the
registered agent. Original capture capability/metadata and boot/recovery
identities matched exactly. No completed transfer intent remained. The final
297-byte recording SHA-256 was
`9ca40f35f193fb127239647a616df26a7396ed5fe85db24abf866fc18249ca61`.
Both saved SQLite snapshots passed `integrity_check`.

The first installed-flow harness attempt constructed its trusted ShellManager
before the normal worker could create the runtime directory. It stopped before
starting any server/process; its exact passive fixture was removed. The harness
was corrected to obtain that manager after normal provisioning. The fresh
second attempt above ran the complete flow without a product change or mock.

Owned cleanup passed: normal console shutdown, explicit exact-terminal cleanup,
fingerprint-checked signals and descendant reaping left all eight recorded
process identities gone. Listener inodes belonged to each exact console while
running; no TCP listener or matching Unix socket remained. The disposable
fixture was removed. Schema 7, 16 public operations and five job handlers stayed
unchanged. No global service/configuration or VM/browser installation changed.

Private commands/logs, source/wheel provenance, synthetic raw bytes, snapshots,
process/socket receipts and the copied completed Terminal helper are retained
in this lane's `build-terminal-archive-r1` evidence directory, particularly
`installed-attempt-2/`. Prior accepted evidence was neither changed nor reused
as a live fixture. The runtime code depends on none of these private artifacts.
The normal flow covers a small recorded Bash stream; the focused tests cover
the binary 1-MiB observation bound and transfer interruptions. Archive file
durability retains its existing owning tests rather than duplicating that suite.
No full G3, independent backup, native-provider, VM, reboot, merge or deployment
acceptance is claimed.
