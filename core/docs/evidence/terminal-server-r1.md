# Recorded Terminal server D38: implementation evidence

2026-10-08. Candidate is the accompanying commit on
`codex/build-terminal-server-r1`, based exactly on
`c3d5b633aedcc1fdd056a02136259eb26f56adb1`. This report requests independent
review; it is not Manager acceptance or deployment.

The optional local Terminal configuration composes the accepted D37
`ShellManager` into the normal console lifespan for reserved onbash. Application
assembly remains passive. Owned workspace validation precedes the fixed local
generation-1 binding and fsynced directory publication. Ordinary agents remain
unsupported, and provisioning retains its resource dependency hold. Repeated
observation preserves the same recorded attempt; changed configuration/recovery
does not replace it. Shutdown joins observers and viewers without stopping Bash.

`terminal_get` is the sixteenth shared operation, with immutable cached views
and five-second monotonic expiry. Host schema remains 6 and typed handlers remain
5. The cookie-only protocol-1 WebSocket delegates attachment/control/input to the
application service and existing shell gates. Output stays binary and bounded;
all sends are serialized with a five-second deadline. Cookie checks also run
while the greeting is waiting to send. Durable human holds survive disconnect,
logout and console restart. `runtime_get.input_ready` stays false.

Only the service/models, narrow config/application/server/boundary assembly,
one reusable workspace verification context, dependency inputs, owning tests and
mechanical inventory/CSP assertions changed. Shell/recorder/native adapters,
ownership/messaging/conversation implementations, migrations and public plans
are unchanged. The implementation composes this repository's accepted APIs;
no external Qorqut code, live installation data, native stores or other builder
worktrees supplied implementation or fixtures.

## Initial candidate checks

These results describe the initial submission
`5d56a45feda60cdcec7bea10457a4e1223f92eb8`. Its independent review found the four
issues corrected below; these initial checks did not establish those boundaries.

The disposable validation interpreter is Python 3.12.3. Actual local processes
used tmux 3.4 and Bash 5.2.21. Uvicorn 0.54.0 with the required
`websockets-sansio` backend and `websockets==17.2` passed real HTTP/WebSocket
connections. The other 31 locked distributions are unchanged.

- Affected source regression: 77 tests passed in 140.219 seconds, covering
  foundation, workspace/agent flow, ownership transport, passive archive,
  HTTP/MCP/operator, console lifetime, dated Chat publication and Terminal.
- After moving the initial greeting into the watched output writer, all 14
  focused final source tests passed in 25.157 seconds: Terminal, transport
  admission/lifetime and console lifetime.
- All six owning groups passed again from the final installed wheel under
  syscall tracing in 34.085 seconds, without skips. The tests-only import root
  contained byte-identical copies of the owning tests and required test helpers.

The real console group starts the generated `qorqut-core serve` script twice
on an isolated loopback port. The server's normal job worker provisions onbash,
and its Terminal loop creates real recorded Bash. Tests verify Bash's fixed
argv and initial child topology, two cookie viewers, raw Unicode/Ctrl-U input,
explicit takeover/release, stale-frame refusal, real resize observed with
`stty`, malformed/oversized frames, the four-viewer limit, rejected origins and
headers, logout and fresh viewers after restart. HTTP, official MCP SDK and
installed operator all read the shared Terminal operation. Shell exit reports
stopped without replacement while server readiness stays healthy.

Across normal console stop/restart, exact terminal/tmux/recorder/Bash identities,
an owned `sleep` child, recorder offsets and the human viewer/epoch/sequence
survive. The retained synthetic raw capture includes output emitted while the
console is stopped. Local host-archival offset remains zero; capture is not
relabeled as native conversation or processed work.

The remaining groups use real recorded Bash with controlled local seams for
failure timing: an actual input effect followed by a lost reply produces an
uncertain receipt and cannot reuse its consumed sequence; cancellation and
application close wait for an admitted effect before closing its attachment or
storage. An ASGI send barrier exercises bounded slow output and revoked greeting
handling. These barriers are deterministic transport tests, not a measured
network/browser congestion limit. Idle output-only expiry, recovery changes,
workspace-marker conflict, changed configuration, stale cache, synthetic global
capacity, strict framing and unexpected observer death are also exercised.

## Package and retained evidence

Private artifacts are under the named run
`01a116b0-8d6d-72f2-9da9-76bd2e898f04/terminal-server-r1-validation/`:
source/final-source logs, final installed logs, wheel/build/install logs,
`run-installed.py`, `installed-provenance.json`, `final-trace.*`, synthetic
`capture/` bytes and `final-verification.json`. No fixture credentials or
operational databases were committed.

Initial candidate wheel SHA-256:
`3c203d420c676c923d677ada665808c8157bada67433d99a792a30c6e3d4860c`.
All 66 package Python/NOTICE files match source, wheel and installed bytes; all
32 installed lock pins match. All 62 loaded core modules in the test process
come from the verified installed package. Syscall traces independently locate
both normal console starts and the four actual recorder processes' imports in
that package. Owned fixture processes were reaped and temporary data/HOME/socket
roots removed; only synthetic evidence remains in the private run directory.

Commands used the private venv's Python/pip, `PYTHONPATH=src:tests python -m
unittest ...` for source checks, `pip wheel --no-deps --no-build-isolation .`,
`pip install --no-deps --force-reinstall <wheel>`, then
`strace -ff -e trace=execve,openat .../python -I .../run-installed.py` and the
retained `verify-final.py`. Final owning command:

```text
python -m unittest transports.test_terminal \
  transports.test_transports.AdmissionAndLifetime \
  transports.test_console.ConsoleLifetime -v
```

This is local installed Terminal acceptance evidence only. Actual compiled UI
integration and independent review remain Manager-owned follow-ups. Native CLI
startup/inbox/Chat delivery, assistant ingestion, VM/browser integration,
systemd-cgroup/reboot survival and full G0–G6 acceptance were not exercised or
claimed. There were no provider calls, owner sessions, VM/browser changes,
merge, PR or deployment.

## Review corrections

Manager message 204 supplied four corrections and the amended D38 requirements.
The revision changes only application settings persistence, Terminal stream
admission/reads, the WebSocket endpoint, the existing six grouped tests and this
evidence. The frozen runner and shared `transports/lifetime.py` are unchanged.

- Endpoint shutdown marks reply delivery stopped before cancelling child tasks.
  Writer exit cancels every outstanding delivery future. A runner error that
  settles after cancellation completes cleanup without waiting for an error
  reply from the stopped writer.
- Stream state and each admitted binary read require an unexpired available
  observation for the same binding. An unavailable/stopped/changed target
  closes the viewer without replacing the shell or releasing its human hold.
- Output reads acquire the existing stream effect lock without waiting. A
  pending input/control effect therefore cannot stall periodic state and cookie
  checks. Descriptor and effect cleanup retain their existing serialization.
- Initialization encodes the complete saved JSON, including indentation and its
  final newline, and refuses more than 16,384 UTF-8 bytes before directory, lock
  or database effects. Persistence writes those exact checked bytes. Existing
  bounded readers and installation files remain unchanged.

The cancellation group now delays a real stale-control error with the runner's
actual input lock, starts endpoint shutdown, then releases the gate. Shutdown
finishes without a second cancellation, leaves zero streams and retains the
exact human control record. This is distinct from the group's retained synthetic
successful-effect/reply-loss barriers.

The real console group holds that same runner lock for 3.25 seconds while a
resize waits. Its fast real WebSocket continues receiving available state every
second (the test allows 150 ms scheduling jitter); readiness stays responsive,
and exactly one sequence settles when the gate opens. It also creates an exact
returned tmux test window, waits for the core's `binding_unconfirmed` observation,
and verifies viewer closure before subsequent output from that window. The
original recorder does not misattribute the window's marker, Bash and the hold
survive, and cleanup kills only the exact returned window before ordinary
fixture cleanup. Observation remains bounded by the existing loop/cache cadence;
this does not claim atomic verification of every output byte.

The settings group checks both ASCII and non-ASCII documents: a 16,385-byte
document is rejected without creating its data root or acquiring its host lock;
exactly 16,384 bytes initialize, remain byte-identical and reopen successfully.
The stale-cache group verifies that an existing attachment cannot read output
after its observation expires.

Revision validation passed without skips:

- All 26 affected source tests passed in 46.166 seconds: the six Terminal
  groups, all console tests, transport admission/lifetime and explicit-input
  tests, plus foundation configuration, installation identity, passive import
  and external-I/O checks.
- All six owning groups passed from a fresh installed wheel in 51.748 seconds
  under `strace -ff -s 4096 -e trace=execve,openat`. The normal console restart,
  real Bash/recorder, owned child, raw capture and human-hold checks remain in
  the same installed flow.
- All 66 source Python/NOTICE files matched wheel and installed bytes, all 32
  locked distributions matched, and all 62 imported core modules came from the
  installed package. Traces verified both normal console starts and all four
  recorder processes importing that package. All 14 recorded process identities
  were gone after cleanup, and all four fixture data/HOME/socket roots were
  removed. Retained raw capture still contains the console-away marker.

Revised wheel SHA-256:
`32e8f69216c8f74356aa21ceb08d95a5625fc8441730cd9b62b5d3460da76d3b`.
Revision artifacts are retained separately in the same private validation root's
`revision-204/`: `source-terminal.log`, `source-regressions.log`, build/install
logs and wheel, `installed-checks.log`, `installed-provenance.json`,
`final-verification.json`, `final-trace.*`, exact test copies and synthetic raw
capture. Initial-candidate and Manager/reviewer evidence were preserved.

The revised candidate still awaits fresh independent review and Manager's
compiled-UI/installed-core integration. These checks do not establish native
startup/inbox/Chat processing, VM/browser behavior or deployment acceptance.
