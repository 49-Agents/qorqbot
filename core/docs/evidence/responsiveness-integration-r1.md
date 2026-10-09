# Combined D49 responsiveness verification r1

2026-10-09 UTC. **D49 passed the bounded request/connection check and failed
archive throughput.** All 24 burst reads succeeded and the controlled Terminal
remained live, but automatic host archival missed the fixed 30-second gate.
Later natural catch-up does not change that failure. Independent outage/restart
and normal Bash-exit checks passed under Manager decision 332.

Only this report is tracked on `codex/verify-responsiveness-integration-r1`.
This executes [D49](../RESPONSIVENESS-CORRECTIONS.md) against its three accepted
components; Manager acceptance of this evidence is separate.

## Exact installation and containment

| Input | Verified pin / result |
| --- | --- |
| Core source and installed package | `6c1ad0e11f8e29ba1e23d50a1314bc6fd2c06fdd` |
| UI source and compiled production files | `6e3cb819764dd706accb12528d9f40fd4ff70da6` |
| Fresh core wheel SHA-256 | `f0b61fea40e5d9b834c45bd02441a742993932b47578a7c1329462493f8108a5` |
| Installed inventory | Schema 9, 17 operations, five handlers |
| Programs | Python 3.12.3, tmux 3.4, Chrome 153.0.8010.36, Node 22.22.0 |

Private evidence root (`$RUN`):

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/verify-responsiveness-integration-r1
```

Both pins were exported with `git archive`. A fresh private venv reused unchanged
locked dependency files from completed `connected-chat-codex-r2`, verified all
32 pins, built the assigned core wheel and installed it privately. All 85 package
files matched source/wheel/installation; 291 core and 74 UI exported files stayed
unchanged. Both ordinary server starts imported 81 core modules exclusively from
that installation and completed their normal startup/shutdown lifecycles.

The completed `fix-ui-read-recovery-r1` production artifact was reused: all
23 non-documentation tracked files, including the lock, matched the assigned UI
export. All four compiled assets matched bytes served by core before and after
restart. `exports.json`, `provenance.json`, `ui-reuse.json`, `build.log` and
`evidence/installed-import-provenance.json` retain the comparisons and hashes.

New user/network/PID/mount namespaces contained the fixture, with only loopback,
private port 18741, owned HOME/tmp/data and a fresh Chrome profile. Credentials
were newly generated. The normal installed `serve` process provisioned recorded
onbash through its actual background worker/observer. No native program was
configured or executed; no provider, external route, owner/live data, browser
service, VM, SQL mutation, manual job/observer pumping or application patch was
used. Completed helper originals and their hashes remain intact.

Key commands, with `$RUN` expanded to the private root above:

```text
/usr/bin/python3 $RUN/setup.py
$RUN/venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir $RUN/wheels $RUN/core-source
$RUN/venv/bin/python -m pip install --no-deps $RUN/wheels/qorqut_core-0.1.0-py3-none-any.whl
/usr/bin/python3 $RUN/launch.py
$RUN/venv/bin/qorqut-core serve --installation $RUN/run/data/installation.json --transport-config $RUN/run/transport.json --ui-root $RUN/ui-source/dist
/usr/bin/python3 $RUN/action.py '<exact request retained in requests/001.json–027.json>'
$RUN/venv/bin/python -I $RUN/ns.py restart-observation.py
$RUN/venv/bin/python -I $RUN/provenance.py
/usr/bin/python3 $RUN/verify-imports.py evidence
/usr/bin/python3 $RUN/final-audit.py
```

`setup.py` records exact archive/build commands. `launch.py` records the full
`unshare --user --map-current-user --keep-caps --net --pid --fork --mount
--mount-proc` invocation. No broad suite or earlier native-resume flow ran.

## Ordinary two-tab workload: requests pass, archival fails

The actual sign-in form established an HttpOnly, SameSite Strict session. Two
Chrome tabs showed Terminal and Chat/details with their normal polls. Only the
Terminal viewer took control, confirmed by its actual epoch-1 receipt. Terminal
input ran one disposable Python producer: 400 distinct numbered ASCII markers,
40 padding bytes each, one `os.write` per marker, approximately 10 ms apart.
Its first-to-last write interval was 4.134 seconds. Expected LF bytes are saved
separately; actual CRLF output was preserved.

At approximately 0.5, 1.7 and 2.9 seconds after Enter, authenticated browser
`fetch` issued three bursts of eight concurrent `terminal_get`, `runtime_get`
and `config_get` requests. All 24 returned HTTP 200 / `ok=true`; individual
latencies were 20.8–87.3 ms, median 43.5 ms. Whole bursts took 114.6, 47.2 and
52.3 ms. The same socket/control stayed live; all 400 markers appeared exactly
once in live Terminal output. No API/WebSocket interception, synthetic refusal,
input replay or automatic takeover was introduced.

| Original observation | Local bytes | Local ACK / host bytes | Retained bytes |
| --- | ---: | ---: | ---: |
| First sample after 30 seconds from final producer write | 29,014 | 6,460 / 6,460 | 22,554 |
| Later natural catch-up, 149.843–149.867 seconds after final write | 29,014 | 29,014 / 29,014 | 0 |

The original ledger contained **420 chunks**, including shell echo/prompt output;
371 were 72 bytes. The complete size/count distribution is retained in
`ordinary-original-byte-audit.json` and `final-audit.json`. Thus this run exceeded
129 original chunks. Chunk coordinates, sizes and hashes were never modified.
The later interval comes from adjacent original passive samples, including the
remaining 18-byte local ACK/reclamation lag; it is not a new passing gate or
recorder final-drain proof.

Original request 008 returned the 30-second assertion failure. The copied action
helper separately timed out at 25 seconds and returned `PENDING` / exit 2 before
that response arrived. Its original response was reconciled without replay.
The helper already propagated failed actions with exit 1, independently checked
before startup; its wait was subsequently extended to 45 seconds for observing
30-second checks. These harness facts do not explain away the archival miss.
Blocker 329 and Manager decision 332 preserved the failure and authorized the
remaining independent checks. No package was changed or repinned.

## Independent restart, receipts and cleanup

Decision 332 specified 20 markers for the second, previously unstarted producer.
The original 12-marker fixture definition is retained alongside the amended
20-marker definition. Actual Terminal input started its ten-second delay once.
Core then stopped normally; its port returned connection refused while the exact
Bash, recorder and tmux fingerprints remained live.

Before outage, local/ACK/host offsets were all 29,175, with no prior backlog.
During outage, 20 new markers plus prompt output added 1,418 local bytes and
21 original chunks: local 30,593, ACK/host still 29,175. Restarting the identical
core caught up automatically within 8.635 seconds of the restart command. The
terminal/spec/source/epoch, original process identities and human hold remained
unchanged. Real-form sign-in created a new view-only viewer. Only its later
explicit acknowledged epoch-2 takeover authorized the final `exit` command.
Each producer's launch log contains one execution; exact input bytes show no
replay. This restart result does not accept the failed ordinary throughput gate.

Across the flow, 10 input frames carried exactly the two producer commands and
`exit` (309 bytes); two resize frames and both control actions had their actual
matching receipts. Every frame sequence/outcome/byte count was checked. All
447 final local chunk coordinates/hashes matched immutable host objects, and
reconstruction preserved every ordinary/outage marker exactly once. There was
one source, epoch, recording run and link, with unchanged original metadata and
no native owner or Chat entry. Both saved databases passed `integrity_check`.

Read-only sampling at 20 ms retained 52,941 local/host observations and **148
original saved ArchiveReceipt values**, validated against their exact intents,
bindings and chunk coordinates. This is a sampled receipt trace, not evidence
of every individual returned receipt. Observed watermarks were never treated
as authority to ACK or reclaim, and the harness performed neither operation.

Bash exited with code 0. Before cleanup, the recorder reported
`drain_complete=1`, `finalized_offset=30614`; local/ACK/host all equaled 30,614,
no payload or transfer intent remained, and Terminal reported stopped. Final
reconstruction SHA-256:
`a97d74bd3e1f2de38b5c7c84250320d6885e92f3d67181cced51a41a7675dd3e`.
This records this disposable shell's observed final drain only.

The desktop Terminal and 390px Chat/details screenshots were inspected directly:
output and controls were readable, text stayed within its viewport, and the
inspector used normal vertical scrolling. Original screenshots, UI frames,
HTTP responses, timings, snapshots and error logs remain under `evidence/`.
All 4,794 observed schema/operation responses were HTTP 200, with no operation
or WebSocket domain errors and no page errors. Eight connection-refused requests
belong to the intentional outage. Two sign-in session-discovery 401s and one
favicon 404 are separately retained. No synthetic UI fault tests were rerun.

Normal Chrome/core closure and exact-terminal cleanup left all six recorded
process identities absent, no TCP/Unix listener, no remaining namespace process,
and no Chrome profile. No extra forced cleanup signals were needed. The namespace
launcher exited 0; the stale owned socket pathname was removed afterward.
`final-audit.log`, `namespace-cleanup.json` and the private evidence manifest
retain final verification. Submission SHA/remote/clean-status metadata is saved
separately in `submission.json` after committing this report.

No product, tests, dependencies or global plan changed; no PR, merge or deployment
was performed. This bounded Bash/UI run does not accept initial native Chat,
Claude MCP, general shutdown/child/attachment completeness, VM/browser integration
or full G0–G6. D49's archive-throughput failure remains open for Manager review.
