# Manual native connection and history

Manager decision D39, 2026-10-08. D36 still governs the product: creation starts
Bash, and the user starts/configures the CLI in Terminal. This work connects that
execution to the existing inbox and history. It does not add a launcher, a second
agent lock, automatic provider selection, or a new service.

The owner has requested faster parallel implementation and direct Manager review
and testing. From this decision forward, Manager reviews completed changes;
another reviewer actor is not mandatory. Astra MAX remains the builder policy,
with self-review disabled. Existing accepted evidence retains its actual origin.
Use focused owning checks and one combined flow, rather than repeating full
build/test/provenance exercises at every stage.

## Parallel ownership

While core/UI terminal geometry is being corrected, build three independent
parts against the accepted core. None waits for a viewer change:

1. Native JSONL parsing and conversation insertion: adapters/native_records.py,
   conversation native types/writer, and host migration 7.
2. Native file capture and local archive transfer: transcripts/native_capture.py
   and its small local catalog, using the existing runner capture database.
3. Manual hook admission and hook client: runtime/native_hooks.py,
   adapters/manual_hooks.py, and the internal HTTP hook endpoint. This lane
   owns no host migration and does not edit terminal stream/geometry code.

These lanes have callable seams below. The integration step supplies the small
composition linking them, installs hook configuration for the managed shell,
and periodically drains admitted sources. That composition then gets a normal
running-core/manual-CLI check. A unit test of a seam is not that connected check.

## Hook admission

Implement a local `NativeHooks` service assembled on Application. Its trusted
`handle(context, request, *, terminal, program)` entry receives a verified live
ShellView and an explicitly configured NativeProgram(provider, cli_version,
absolute native executable). It returns connected/reconnected, conflict,
unavailable, or observed, with a fixed explanation and optional owner identity.
The request contains agent/terminal IDs, hook-process fingerprint, and the
native hook JSON. Bound request size to 1 MiB. Never trust platform IDs inside
the native JSON.

Walk at most 32 /proc parents from that exact live hook process to the supplied
shell. Recheck start fingerprints and the shell binding. Select the configured
native executable in that chain, not Node or the hook helper. Reject another
root native executable between the selected process and shell: a child agent
must not connect independently. Check provider compatibility against the pinned
profiles. This prevents accidental association; it is not hostile-user security.

SessionStart takes its UUID and transcript path from the native hook, derives
the OwnerCandidate from verified shell/process facts, and calls the existing
SessionOwners.connect. A missing fresh transcript is allowed. An absolute
transcript path must be within that provider's managed HOME store; cwd is not
ownership evidence. The existing incumbent-wins rule is unchanged. Stop means
idle, never release. SessionEnd cannot prove exit. Other events require an
already matching owner; they cannot allocate ownership. Process reconciliation
remains the authority for exit. Persist accepted hook metadata/path/process and
fixed conflict/unavailable diagnostics as attributed events; do not persist
credentials or arbitrary tool/prompt bodies in those events. Repeated startup
delivery can add an observation but cannot allocate another owner.

`adapters.manual_hooks.prepare_manual_hooks(roots, binding, core, programs,
hook_sink_argv)` writes only owned provider hook configuration and its private
binding document; no CLI runs, no preallocated native run is needed. Binding
contains agent/conversation/terminal/recovery/environment IDs. Preserve existing
authored/native settings. Reuse the accepted managed-file machinery and known
provider hook shapes. Claude uses workspace .claude/settings.local.json hooks;
Codex uses managed HOME .codex/hooks.json. Merge only the owned Qorqut hook entries,
preserving unrelated hooks/settings; record generated ownership so a conflicting
edit produces a setup error. Core/browser MCP preparation remains separate.

The sink is `python -m qorqut_core.runtime.hook_client --binding <absolute-file>`.
It reads bounded JSON stdin, captures its own process fingerprint, and POSTs to
`/api/v1/native/hook` using the explicitly configured core headers. Use a bounded
timeout and fixed diagnostics. For conflict print the exact incumbent instruction
to stderr and native hook additional context. Backend/network failure says
could not connect, never another-session conflict. Success does not claim input
readiness or transcript completeness. No retry with a different identity.

The HTTP route is internal, not an MCP/model operation. It uses existing bearer
authentication and the dispatcher/lifetime boundary. It resolves the current
shell and configured program from the managed binding, not arbitrary request
paths, before calling the same service. No extra database/service. Add one
read-only `Terminals.native_context(agent_id, terminal_id)` seam returning a fresh
verified ShellView; do not change shell startup, viewer, control or resize logic.
The installation/composition step supplies the owned binding configuration;
missing configuration returns unavailable. No changes to global native stores.

## Native capture

Implement `NativeCapture(database, roots)` over the existing runner SQLite file.
Keep connections thread-owned. Its `register(observation, capture_run)` accepts
the existing adapter NativeSource and capture RunBinding after admission. Persist
a source mapping keyed by installation/agent/provider/native-session/relative
path, with stable source UUID and current epoch/file identity. Persist intent
before idempotent existing spool registration so reopen can finish registration.
Link a resumed run without duplicating the unchanged prefix.

`poll(source_id, *, max_bytes=1048576)` opens the exact managed regular file,
rechecks adapter ownership spans/device/inode, verifies the already captured
prefix using verify_file_prefix, then appends bounded bytes through CaptureSpool.
Use bounded memory and the existing 256 KiB chunks. A partial JSONL line is raw
data and must be captured immediately. No provider parsing in this lane. A
missing or changed file reports an explicit gap; never silently reinterpret old
offsets. A new verified observation can begin a distinct divergence epoch,
preserving the previous bytes and recording the reason. Retain an already open
descriptor long enough to drain an unlinked old file where possible.

`checkpoint(source_id)` verifies the complete captured prefix and records that
finite offset/hash. It does not promise no future append. All progress survives
reopen; no current-session-only scan. Full prefix verification per poll is an
accepted simple v1 tradeoff; do not introduce a hash-tree/cache system.

`transfer(source_id, archive, context, *, max_chunks=16)` registers the saved
source/run/epoch with the existing Archive, sends exact pending chunks, and ACKs
only the returned matching durable receipt. Reclaim only acknowledged payloads;
keep receipt hashes. Interrupted transfer resumes at the saved offsets. This is
the local onbash composition; no new network protocol or VM claim.

## Native conversation

Implement pure `parse_native_record(provider, cli_version, native_session_id,
raw)` returning typed native text items or an explicit ignored/gap outcome.
Use the reviewed synthetic native rehearsal as the format evidence. Support the
pinned Claude 2.1.287 and Codex 0.160.0 profiles first. Unsupported profiles or
ambiguous structures produce a gap, not guessed text. Accept at most 4 MiB per
complete JSONL record for semantic parsing; oversized bytes remain raw archive.

Claude primary human user records and assistant text blocks use native UUIDs.
Exclude sidechain records from the root conversation. For Codex select the
observed event_msg/item_completed UserMessage and AgentMessage records with
their item IDs and matching thread identity. Ignore their response_item echoes
and task_complete summaries. Do not import instruction-role user records as
human messages. Never deduplicate by text, timestamp or adjacency. Tool/reasoning
and unknown attachment records stay in the raw archive with explicit coverage
limits; parsing never executes a tool or creates a browser/file request.

Each item carries native ID, role user/agent, exact visible text and available
native timestamp. Preserve whitespace and Unicode. Split text exceeding the
existing 64 KiB entry bound into deterministic UTF-8-safe parts carrying part
indices. Distinct equal-text native IDs remain distinct. Missing/unusable native
time uses the durable ingestion time with an explicit timestamp-origin field.

Host migration 7 extends the existing conversation ledger for native entries,
preserving old IDs, sequences, rows and immutable migration 1–6 definitions.
Native source metadata includes provider, owner/run, source/epoch, raw offset/
length/hash, parser version, native ID and part index. Native entries have no
message_id; delivery/handling are nullable. Native user entries need not invent
a platform sender actor; native agent entries use the registered agent actor.
Keep existing message entry wire data and dated file bytes unchanged.

`Conversations.append_native(tx, agent_id, item, provenance, *, operation_id=None)`
is an internal SQL-only writer. Validate saved archive source/run/agent lineage;
the collector supplies an item parsed from those verified archived coordinates.
Uniqueness is source/epoch/parser-version/native-ID/part; an exact replay returns
the existing entry, differing content/provenance at the same identity reports a
conflict. Entries use the existing permanent conversation cursor and enqueue
the same publisher. New native entries automatically appear in UTC dated files.
No UI selection or manual save is involved. A later parser version must not
automatically reinsert the entire history; rebuild/switch remains explicit.

Clarification 222: when append_native receives no operation_id, create an
internal `native_entry_import` operation receipt attributed to the registered
agent actor, in the same transaction as entry and publication job. Its stable
idempotency key hashes source/epoch/parser-version/native-ID/part; its input hash
covers the validated item and provenance (use a missing-time marker rather than
a newly sampled fallback time). Exact replay reuses the existing entry/receipt;
changed input conflicts. Store an ordinary accepted OperationResult referring
to the entry and queued publication, not a claim that publication is finished.
A supplied operation_id must identify an existing current-epoch receipt for
that agent actor. No new public operation, handler or alternative job path.

consume uses transcripts.spool.RunBinding. Verify it against archive_runs, the
saved source/epoch/run link and its session_owners row (owner_id equals run_id).
Use that row's provider/version/native UUID, checking the source native UUID.
An owner need not remain active/live to finish importing its archived history;
archive recovery holds and conflicts still block consumption. Native provenance
run_id denotes the admitted capture/import binding, not proof that a resumed
file's earlier records were originally emitted by that process. Never replace
it with whichever owner happens to be current.

This lane also adds persisted parser checkpoints/gaps keyed by source/epoch/
parser version, atomically advanced with insertion or an explicit gap. Provide
`NativeProjector(application).consume(context, binding, source_id, source_epoch,
*, max_bytes=1048576)`: read verified host chunks, frame complete JSONL records,
parse and commit bounded outcomes. Keep partial tails pending across calls;
never advance past an unhandled partial record. Oversized lines are skipped
with a durable coordinate gap after their newline, without unbounded buffering.
The raw archive remains authoritative. No filesystem work inside SQL writes.

## Checks and integration

Use a few grouped checks per lane: real local process admission/conflict/exit;
native file append/reopen/divergence and archive retry; observed-format parsing,
duplicate identity, partial lines and dated publication. Temporary fixtures only.
Run affected owning tests once after the final edit. Record concise evidence,
commit/push the task branch, and report the exact SHA to Manager. No nested
reviews, repeated whole-suite runs or elaborate file-by-file build manifests.

Manager reviews and tests completed candidates, integrates the three callables,
then runs manually started supported CLIs with disposable HOME/workspace and
the existing synthetic provider harness. Verify one inbox owner, refusal of a
second, restart/resume, raw bytes and automatic dated assistant history. Actual
Chat dispatch remains separately gated by the accepted human hold and native
composer behavior; connecting an inbox does not make pasted input safe.
