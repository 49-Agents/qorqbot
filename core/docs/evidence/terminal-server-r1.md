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

## Checks

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

Final wheel SHA-256:
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
