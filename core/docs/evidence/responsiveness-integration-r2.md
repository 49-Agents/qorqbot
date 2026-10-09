# Combined responsiveness verification r2

2026-10-09 UTC. **The original combined workload passed the fixed 30-second
archive gate:** automatic catch-up was verified 2.895 seconds after the final
producer write. All 24 burst reads succeeded and Terminal remained connected.
Outage recording, automatic restart transfer and this Bash's normal final drain
also passed. Two unresolved core diagnostics are retained below; this is not a
claim of an error-free run. Manager acceptance is separate.

This executes the assigned [D49](../RESPONSIVENESS-CORRECTIONS.md) flow with
accepted [D50 transfer ranges](../TERMINAL-TRANSFER-RANGES.md). The original
[r1 failure and later natural catch-up](responsiveness-integration-r1.md) remain
unchanged. Their original request, failure, later observation and helper hashes
were preserved and rechecked, never reused as r2 passing evidence.

| Input | Exact executed source/artifact |
| --- | --- |
| Core source and private installation | `ef07b0b899f0b20e3058385921457aaa34d810cd` |
| UI source and production bytes | `6e3cb819764dd706accb12528d9f40fd4ff70da6` |
| Fresh core wheel SHA-256 | `a886d725ee789ac52992a5a5609bf26e3f5d437a408bad7fc581857c72b02933` |
| Inventory | Schema 9, 17 operations, five handlers |
| Programs | Python 3.12.3, tmux 3.4, Chrome 153.0.8010.36, Node 22.22.0 |

Private evidence root (`$RUN`):

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/verify-responsiveness-integration-r2
```

Both pins were exported with `git archive`. A fresh venv reused compatible
unchanged locked dependencies from completed r1, then built/installed this core.
All 32 locked versions and the complete 85-file package inventory matched;
299 core and 74 UI exported files remained unchanged. Both normal server starts
loaded 81 core modules exclusively from that installation.

The completed `fix-ui-read-recovery-r1` artifact was reused after comparing all
23 production source/configuration files, including its lockfile, with the
assigned UI export. Its build candidate was
`d0ae01ccaaa3ac9b88999eec4aa366b3ddf2f339`; its production bytes equal the assigned
UI pin. All four compiled assets matched actual served bytes before and after
restart. `exports.json`, `provenance.json`, `ui-reuse.json`, build logs and
`evidence/installed-import-provenance.json` retain exact comparisons.

Fresh user/network/PID/mount namespaces contained only loopback, port 18742,
owned HOME/tmp/data, fresh fixture credentials and a new Chrome profile. Normal
installed `serve`, background jobs/observers and recorded onbash Bash were used.
No native CLI/provider, external route, live owner data, browser service, VM,
SQL mutation, manual observer pumping or product patch was used.

Key commands, with `$RUN` expanded to the root above:

```text
/usr/bin/python3 $RUN/setup.py
$RUN/venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir $RUN/wheels $RUN/core-source
$RUN/venv/bin/python -m pip install --no-deps $RUN/wheels/qorqut_core-0.1.0-py3-none-any.whl
/usr/bin/python3 $RUN/adapt.py
/usr/bin/python3 $RUN/launch.py
$RUN/venv/bin/qorqut-core serve --installation $RUN/run/data/installation.json --transport-config $RUN/run/transport.json --ui-root $RUN/ui-source/dist
/usr/bin/python3 $RUN/action.py '<exact requests retained in requests/001.json–026.json>'
$RUN/venv/bin/python -I $RUN/ns.py restart-observation.py
$RUN/venv/bin/python -I $RUN/provenance.py
/usr/bin/python3 $RUN/verify-imports.py evidence
/usr/bin/python3 $RUN/final-audit.py
/usr/bin/python3 $RUN/preserve-and-clean.py
/usr/bin/python3 $RUN/reconstruct-preserved.py
```

The launch record retains the exact `unshare --user --map-current-user
--keep-caps --net --pid --fork --mount --mount-proc` command. Completed r1 helpers
were copied with originals/hashes intact, retaining the corrected 45-second
action timeout and failing exit status. Private archive checks were adapted to
compare original chunks against contiguous host ranges and preserve actual
immutable objects. No broad suite, dependency matrix or synthetic HTTP failure
test ran.

Actual form sign-in established an HttpOnly, SameSite Strict session. Two Chrome
tabs displayed controlled Terminal and Chat/details with normal polls. An extra
desktop `Open inspector` helper click failed before any input because the
inspector was already visible. Request 004, its result/screenshot and exit 1
remain intact. Manager decision 338 authorized continuing from that state;
the click was never replayed and no product/helper code was changed for it.

One acknowledged epoch-1 takeover preceded the ordinary Python producer. It
wrote 400 unique numbered ASCII markers with 40 padding bytes, one `os.write`
each: 71 LF bytes, **72 captured CRLF bytes per line**. First-to-last writes took
4.083 seconds, with mean spacing 10.233 ms. Expected bytes and original producer
timings are preserved separately. The three bursts occurred during production,
around 0.5, 1.7 and 2.9 seconds after Enter. Each issued eight parallel browser
`fetch` reads mixing `terminal_get`, `runtime_get` and `config_get`.

All 24 returned HTTP 200 / `ok=true`; request latency was 18.9–100.1 ms, median
43.1 ms. Burst elapsed times were 46.4, 111.6 and 54.0 ms. All 400 markers appeared
once in live Terminal output; the same socket stayed open. No interception,
fabricated failure, input replay or automatic takeover was introduced.

| Observation | Local bytes | Local ACK / host bytes | Original chunks / host ranges |
| --- | ---: | ---: | ---: |
| Ordinary catch-up | 29,014 | 29,014 / 29,014 | 294 / 7 |
| Core down, before delayed output | 29,175 | 29,175 / 29,175 | 297 / 9 |
| Core down, after delayed output | 30,593 | 29,175 / 29,175 | 319 / 9 |
| Restart catch-up | 30,593 | 30,593 / 30,593 | 319 / 10 |
| Normal Bash final drain | 30,614 | 30,614 / 30,614 | 323 / 11 |

The ordinary ledger exceeded 129 chunks. Its seven host ranges contained
`2, 1, 2, 2, 128, 128, 31` whole originals. The size distribution includes
175 original 72-byte chunks, 105 of 144 bytes and five of 216 bytes; the complete
distribution and ledger are saved. Capture framing/coordinates were unchanged.
Two ranges reached the 128-original limit; the largest was 13,259 bytes, so this
workload did not exercise the 256 KiB limit.

Passive samples showed an outstanding tail at 2.853 seconds and catch-up at
2.878 seconds after the final write. The explicit read-only check verified
catch-up, no retained payload and no pending intent at 2.895 seconds. These are
point-in-time, sequential store observations, not atomic cross-store sampling
or recorder final-drain proof. The original 30-second gate was never raised.

The separate producer ran once, waited 10.000 seconds, then wrote exactly 20
unique short markers at mean 10.130 ms spacing. Core stopped normally before
those writes; connection refusal was verified while the exact Bash/recorder/tmux
fingerprints remained live. The outage added 1,418 local bytes in 22 original
chunks without host/ACK advancement. Identical core restart caught up through
normal observers within 1.934 seconds of the restart command. Terminal/spec/
source/epoch/process identities and the prior human hold remained unchanged.

Real-form sign-in created a different view-only viewer. Its explicit epoch-2
takeover was acknowledged before `exit`. All 11 input frames (309 bytes), two
resize frames and both control actions matched actual receipts, sequences,
epochs, viewers and submitted byte counts. Their exact input bytes equal the
two producer commands plus `exit`, with one launch of each producer and no replay.

Every one of the 323 original chunk coordinates/hashes matches the actual
reconstructed host bytes. All 11 host ranges are contiguous and contain whole
original chunks within D50 bounds. One original source/epoch/run/link remains,
with precise original recording/actor bindings and no native owner or Chat entry.
All ordinary/outage markers occur exactly once. Both saved databases passed
`integrity_check`.

At 20 ms sampling, 10,496 offset observations retained 20 intent versions for
ten ranges, including **ten actual persisted ArchiveReceipt values**. Every
manifest, aggregate hash, original ledger entry, binding/recovery identity and
receipt end matched. The initial 18-byte host range's receipt was not sampled;
this is not a complete returned-receipt trace. No receipt was manufactured or
inferred from a watermark, and the harness performed no ACK/reclamation.

Desktop Terminal and 390px Chat/details screenshots were directly inspected:
output, controls and wrapping were readable, with normal inspector scrolling
and no horizontal overflow or visible error banner. Browser evidence preserves
883 parsed schema/operation responses, all HTTP 200, and no page or WebSocket
domain error. Separate records retain four outage connection refusals, two old
session `events_list` 401s after restart, two fresh sign-in discovery 401s, one
favicon 404, three read aborts from the restart viewer, and two response-body
observation failures when old tabs closed. The aborts concern conversation/
runtime/terminal reads; their cause was not instrumented. None was a burst
request or an input effect.

The original core log also retains one `terminal_archive_transfer_failed`
warning and one `ERROR: Core server diagnostic (details omitted)`. The latter
appears during initial core shutdown. Logging provides neither the warning's
precise phase nor either root cause; successful byte/gate checks do not erase
these unresolved diagnostics. No rerun or product/configuration tuning followed.

Bash exited 0 and reported `drain_complete=1`, `finalized_offset=30614` before
cleanup; local/ACK/host matched, with no pending intent or payload and Terminal
stopped. This proves only this fixture's observed normal exit/drain. Final bytes
have SHA-256
`e3b9add4ad036779ba3ca921d228f38cf58f0bac14d3344d46cef2a864360776`.

Normal Chrome/core closure and exact-terminal cleanup left all six recorded
fingerprints absent, no TCP/Unix listener, no Chrome profile and no namespace
process. No additional forced signals were needed; the launcher exited 0.
All 11 **original immutable host objects**, original bytes, expected markers,
producer timings, manifests/receipts, snapshots, databases and UI/error logs
were preserved before deleting disposable data/HOMEs/credentials. Reconstruction
from preserved objects and both databases passed again after deletion.

Only this report is tracked on `codex/verify-responsiveness-integration-r2`.
`evidence-manifest.json` indexes private evidence; `submission.json` records the
submitted commit, exact remote SHA and clean state after commit/push. No product,
tests, dependencies or global plan changed; no PR, merge or deployment occurred.
This bounded Bash/UI result does not accept initial native Chat, Claude MCP,
general shutdown/child/attachment completeness, VM/browser integration or G0–G6.
