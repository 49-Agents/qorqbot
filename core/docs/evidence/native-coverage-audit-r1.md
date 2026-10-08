# Native-history coverage audit r1

Factual exploration against **`e74369c0e214570f4a6892650f3ae59e3689787c`**, branch
`codex/explore-native-coverage-r1`, 2026-10-09 UTC. **Accepted primary-stream flows
do not establish all-stream coverage.** Concrete remaining gaps include cached
discovery overlooking later files, unsupported child association/external
attachments, and no native shutdown drain/finalization boundary. No implementation
or design change is proposed here.

R10 and [M07 §§1–5](../modules/07-transcripts.md) require emitted supported
streams, child/attachment provenance, durable retry and visible incompleteness;
[CONTRACTS §8](../CONTRACTS.md#8-archive-and-history-contract) distinguishes raw
archives from Chat. [D39](../NATIVE-CONNECTION.md) and
[D40](../NATIVE-COMPOSITION.md#continuous-collection) deliberately accepted narrower
callables. Their acceptance is not evidence that the broader requirement is met.

**Evidence scope.** `$B` is
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04`;
new audit outputs are `$B/native-coverage-audit-r1` (`$A`). Original completed cases:

| Case | Original directory beneath `$B` | Data directory beneath that case |
| --- | --- | --- |
| MC | `manual-native-claude-r1/attempt-3` | `run/data` |
| MX | `manual-native-codex-r1` | `tmp/qnc-r1/data` |
| IC | `explore-native-input-r1/claude` | `run/data` |
| IX | `explore-native-input-r1/codex` | `run/data` |

These are actual Claude **2.1.287** / Codex **0.160.0** emitted artifacts with
canned loopback responses, not paid inference. Original executed core was
`a2a162ce67f0eccafee537bd75a97069c1280788`. The eight audited discovery, collector,
capture, spool, archive, parser, projector and monitor files have identical Git
blobs at that revision and this audit base (`$A/source-provenance.json`).
[Claude](manual-native-claude-manager-acceptance.md),
[Codex](manual-native-codex-manager-acceptance.md) and
[input](native-input-manager-acceptance.md) acceptance retain their original
qualifications, including fixture-only Claude onboarding and no complete G3.

Only documented native-store bases, source objects, saved observations and selected
history tables were inspected. SQLite queries used private DB/WAL copies.
`$A/inventories.json` records exact original paths, coordinates, record-type
counts and hashes; `original-file-hashes.json` verifies all 48 consumed original
files unchanged. No native CLI, model, browser, workflow rerun or unfinished actor
material was used. The two additional diagnostics below are synthetic only.

## Provider/stream comparison

Code citations below are under `src/qorqut_core` at the frozen audit base.

| Stream | Claude 2.1.287 | Codex 0.160.0 | Evidence and limit |
| --- | --- | --- | --- |
| Primary | Canonical `<session UUID>.jsonl` below `.claude/projects`; non-child `sessionId` proof | `rollout-YYYY-MM-DDTHH-MM-SS-UUID[_UUID].jsonl` below `.codex/sessions` or `.codex/archived_sessions`; matching `session_meta.payload.id` | [discovery.py:129–224](../../src/qorqut_core/adapters/discovery.py#L129). Exact bytes confirmed in all four original cases below. Neither cwd nor newest-file selection establishes ownership. |
| Resumed session | MC reused its primary source under two admitted runs | MX likewise | Both actual manual flows preserved prior bytes/entries. [native_capture.py:311–385](../../src/qorqut_core/transcripts/native_capture.py#L311) keys source by installation/agent/provider/native UUID/relative path and links unchanged resumes. Import provenance remains the earliest compatible linked run, not inferred per-byte authorship ([collector:239–251](../../src/qorqut_core/runtime/native_collector.py#L239)). |
| Child/subagent | Child-shaped paths skipped; inline `isSidechain`/`agentId` records cannot establish primary ownership, but their bytes survive within an independently owned primary | Child-shaped paths skipped; candidate metadata with parent/fork IDs or dictionary `source` rejected | [discovery:143–195](../../src/qorqut_core/adapters/discovery.py#L143). Independent child capture/linkage is unsupported. None of these four canned flows establishes actual child production or completeness. Hooks also refuse child identity/nested native ancestry ([native_hooks:87–135](../../src/qorqut_core/runtime/native_hooks.py#L87)). |
| Tool calls/results, emitted reasoning | Any such bytes in a registered file pass through raw capture; mixed/tool assistant content is outside the accepted text projection | Same byte preservation; non-message items produce parser gaps | [capture:418–441](../../src/qorqut_core/transcripts/native_capture.py#L418), [parser:93–166](../../src/qorqut_core/adapters/native_records.py#L93). No actual tool-result execution was established by the four originals. Synthetic giant-record capture does not establish live tool/child emission coverage. |
| Attachments | Each Claude original contains 14 inline `attachment` records, all host-archived; includes environment/instructions/prompt snapshots, not proof of media-file capture | The observed primary files contain no image blocks; discovery recognizes `UserMessage` image URLs | [discovery:104–126,196–219](../../src/qorqut_core/adapters/discovery.py#L104). Embedded `data:` bytes stay in raw JSONL; external URLs are references only, with `external_attachment_not_captured`. No attachment fetch/extraction/source registration occurs. Returned attachment references are not consumed by the collector. Unknown layouts remain unverified. |
| Rollover/truncation | Same-path inode/prefix/ownership changes can start a verified new epoch; a newly named source is subject to the discovery gap below | Same; archived-directory eligibility does not guarantee rediscovery after a move | [capture:263–279,331–381,393–441](../../src/qorqut_core/transcripts/native_capture.py#L263). Accepted [capture tests](native-capture-r1.md) cover synthetic divergence, >9 MiB/giant/partial records and retained descriptors. Actual provider rollover/truncation was not exercised in these originals. |
| Late append | Registered files remain pollable after exit; MC has a concrete uncollected post-stop tail | Registered files remain pollable after exit; MX/IX final primary files match host bytes | Actual inventory below; [collector tests:175–200](../../tests/runtime/test_native_collector.py#L175) separately prove synthetic late append/resume. No final-emission guarantee follows. |

An absent stream in a text-only canned exchange is **unexercised**, not proof that
the provider never emits it. Provider-internal data never emitted and killed-process
unflushed buffers are outside M07's achievable guarantee. Native debug logs,
request logs and terminal output are not interchangeable transcript sources.

## Original inventories and final boundary

Each case has exactly **one JSONL file within its provider discovery bases**, one
registered native source/epoch and six native conversation entries. MC/MX each
have two run/source links; IC/IX each have one. All 28 immutable host objects were
reconstructed from their actual receipt coordinates and checked against source
prefixes. “Original” below means the final retained native file, not a Chat copy.

| Case | Original records/bytes | Host bytes/chunks | Final original SHA-256 | Parser gaps |
| --- | ---: | ---: | --- | ---: |
| MC | 43 / 170,350 | 169,115 / 6 | `10434a9e50f604f92b9c1d6562c963917c21c44471bcd06b2d12ba5da1d2bccc` | 14 |
| MX | 36 / 74,028 | 74,028 / 8 | `b8f1f8e31dbdf0b53a265b8b74aa60d36f3cf3b9012ec4a65812053ec37001e9` | 0 |
| IC | 48 / 238,975 | 238,975 / 8 | `ba6bb8a7fb62cc0a2f7994a1e9984eb75c87a9cbf2d00233aa6f579b30c20bf3` | 14 |
| IX | 35 / 215,459 | 215,459 / 6 | `7ab55fc1388c8e29b6ca44d33ac6e2016a0a70dd1c368a7714df379105cddae9` | 0 |

Paths relative to each agent's `runtime/home` are:

- MC: `.claude/projects/<encoded-workspace>/f9fc4d0e-9b32-4d98-960e-8c313825ff11.jsonl`.
- IC: `.claude/projects/<encoded-workspace>/e661a50e-fbcb-4a6f-85f2-e768c1d0078e.jsonl`.
- MX: `.codex/sessions/2026/10/08/rollout-2026-10-08T20-04-52-01a11dfa-19d9-7453-87bf-1a98fafc921c.jsonl`.
- IX: `.codex/sessions/2026/10/08/rollout-2026-10-08T20-50-01-01a11e23-6f50-7833-b085-0e5a7970686f.jsonl`.

`<encoded-workspace>` is the corresponding absolute
`<case>/<data>/agents/<agent-id>/workspace` with every `/` and `.` replaced by `-`;
MC agent is `1644b083-85f0-4382-9c35-83c187830768`, IC
`348eb9ad-e27a-434e-b5bc-5664cbc06778`. Full literal paths are in the inventory.

MC's archived prefix hash is
`ae381ef4384b51cff92c0634e49d3863b26bc4e2a6059385520a6600559cb193`.
Its unarchived range `[169115,170350)` is **1,235 bytes**, SHA-256
`bd94c9fddea9bcbc149049540fadc21b1c22518ebdfce0241e794d33f6933de0`:
three metadata records (`cost-state`, `last-prompt`, `cost-state`). This is the
already recorded [post-collector-teardown omission](manual-native-claude-r1.md#archive-chat-and-dated-files),
not lost acknowledged bytes. It survives in the original native file only;
local capture and host ACK both stop at 169,115. The other three original/host
files match fully. These counts do not establish nonexistent additional streams.

All four native spools have zero retained payload bytes, zero pending transfer
intents, **zero native checkpoints**, null `finalized_offset` and
`recorder_finished_at`, and `drain_complete=0`. Captured/ACK/parser offsets agree
through their archived prefixes. Such agreement does not certify producer EOF.
MC's saved runtime says `gap/parser_gap`; IC says `gap/capture_gap`; MX/IX say
`collecting/active`, all with zero pending archive/parse bytes.

## Discovery cadence and visibility

The normal [monitor:35–64](../../src/qorqut_core/runtime/monitor.py#L35) reconciles
ownership, ticks setup/collection, then waits one second. This is a delay after
work, not a fixed one-second completion guarantee. The collector selects saved
onbash owners, including closed ones; new discovery requires the current recovery
epoch. Historical reobservation is limited to already admitted paths, and existing
archive/recovery holds remain effective ([collector:134–145,254–295](../../src/qorqut_core/runtime/native_collector.py#L254)).

**After any successful discovery, that owner is excluded from ordinary future
scans.** Only a requested reobservation or a newly admitted owner reopens discovery.
Restart initializes `_discovered` from durable source/run mappings
([collector:206–211](../../src/qorqut_core/runtime/native_collector.py#L206)); it does
not rescan an otherwise unchanged successful owner. Hooks record observations,
not a collector rediscovery request ([native_hooks:251–268](../../src/qorqut_core/runtime/native_hooks.py#L251)).
A later matching primary file, or a file initially lacking usable identity while
another succeeded, can therefore stay unregistered indefinitely. Later
child-shaped files are neither collected nor newly diagnosed on ordinary ticks.

Per tick, owner attempts and serviced source IDs are each capped at 16; missing
owners retry no faster than once/second. Priority occupies at most half the batch,
retaining historical round-robin work. Each source gets 1 MiB fresh capture,
16 transfer chunks, and 1 MiB projection shared across epochs
([collector:58–75,297–355](../../src/qorqut_core/runtime/native_collector.py#L297)).
This bounds counts/new-byte budgets, not total scan work: discovery recursively
enumerates the store and scans matching files, and polling verifies complete
captured prefixes. Its 1 MiB metadata-record bound is not a whole-file ceiling.

**Synthetic diagnostic 1:** existing owning helpers admitted a Codex primary,
then created a second independently eligible same-session rollout and a
child-shaped file. Three ordinary ticks plus reopen made **zero discovery calls**,
kept one source and reported `collecting/active`, zero lag. Direct read-only
discovery found both eligible sources and the child diagnostic. A new resumed
owner triggered discovery, registered the second source and exposed `capture_gap`.
This confirms the static scheduling gap, not actual native child/rollover behavior.

**Synthetic diagnostic 2:** moving the registered file into eligible
`.codex/archived_sessions` allowed its retained descriptor to capture/ACK 846 bytes.
After reopen, a 367-byte append remained uncaptured despite three ticks. Direct
discovery found the moved file, but the collector made zero scans: `source_missing`
does not set the divergence flag used for reobservation
([capture:274–279,393–441](../../src/qorqut_core/transcripts/native_capture.py#L274),
[collector:313–319](../../src/qorqut_core/runtime/native_collector.py#L313)).
Here the omission is visible as `capture_gap`, though pending archive bytes are zero.

Discovery always returns child/attachment uncertainty, but the collector excludes
those baseline codes from its gap state. Specific discovered problems set an
in-memory flag, not a durable path-level gap inventory
([collector:36–38,95,282–285,357–384](../../src/qorqut_core/runtime/native_collector.py#L357)).
Archive metadata retains “child and attachment coverage unverified”
([capture:354–358](../../src/qorqut_core/transcripts/native_capture.py#L354));
`runtime_get` exposes only aggregate state/count/lag, no attachment inventory
([native_models:16–24](../../src/qorqut_core/runtime/native_models.py#L16)).
Thus explicit stored limitations coexist with silent omission of undiscovered files.

## Exit, restart and remaining unknowns

Normal CLI exit closes ownership through process reconciliation; collection does
not require that owner to remain live. The accepted manual flows observed normal
exit/resume and a live CLI surviving normal core stop/restart. Input observations
exited both native CLIs before stopping core and retained complete primary files.
They prove these executions, not a final append deadline.

Server shutdown stops the job worker, then joins the monitor
([server:342–362](../../src/qorqut_core/transports/server.py#L342)). The monitor can
finish an in-progress tick but schedules no mandatory final drain. Collector close
only closes capture/descriptors; capture close performs no poll, transfer or
checkpoint ([collector:123–132](../../src/qorqut_core/runtime/native_collector.py#L123),
[capture:152–160,443–459](../../src/qorqut_core/transcripts/native_capture.py#L443)).
`NativeCapture.checkpoint` has no production caller and itself neither drains nor
closes a source. Ordinary restart resumes saved offsets while the named source
remains valid. An unlinked descriptor cannot survive process shutdown.

Captured bytes/cursor commit together; immutable host files and their directory
are fsynced before the host receipt transaction completes. Only its matching
returned receipt permits local ACK then reclamation
([spool:433–478](../../src/qorqut_core/transcripts/spool.py#L433),
[archive:286–368](../../src/qorqut_core/transcripts/archive.py#L286),
[capture:476–549](../../src/qorqut_core/transcripts/native_capture.py#L476)).
Unacknowledged captured bytes/intent survive locally; bytes never polled remain
solely provider-file data. One host ACK promises no independent backup.

Raw partial/oversized records survive; the projector waits for a newline and caps
semantic records at 4 MiB, recording oversized/unsupported outcomes as gaps
([projector:222–295](../../src/qorqut_core/conversation/native.py#L222)). An unfinished
suffix remains parse lag, without exit finalization. Claude's 14 attachment gaps
are explicit; Codex's zero primary parse gaps say nothing about excluded files or
external attachments. Child projection is explicitly refused
([projector:31–56](../../src/qorqut_core/conversation/native.py#L31)).

Actual child association, media dependencies, provider rollover/truncation, final
drain under concurrent append, and ordinary-agent/VM collection remain unsupported
or unverified. Only onbash is serviced; other targets report `unsupported_target`.
The [input observations](native-input-observations-r1.md) also preserve Claude's
TAB transformation and Codex's unexplained `retained_source.complete=false` on a
fully preserved large user record; neither fact proves collector byte loss.
Historical native runs predate D41 Terminal host archival; its separately
[accepted Terminal correction](terminal-archive-manager-acceptance.md) does not
close these native-history gaps.

## Audit checks

Exact commands/output are in `$A/commands.txt`, `inventory-output.txt`,
`diagnostics-output.txt` and `diagnostics-stderr.txt`. `inventory.py` passed
original/chunk/hash assertions; its first query used nonexistent `drained`, then
was corrected to `drain_complete` using fresh private copies. The failed script
and diagnostic are retained. `diagnostics.py` used existing
`tests/runtime/test_native_collector.py` helpers: **two targeted diagnostics passed**,
no stderr, owned children cleaned up and temporary fixture directory empty.
No existing suite or native workflow was rerun. `git diff --check` and document
link/word-count checks passed. Only this report is tracked; original evidence,
product code, dependencies and plans remain unchanged. Manager acceptance,
merge and deployment are separate.
