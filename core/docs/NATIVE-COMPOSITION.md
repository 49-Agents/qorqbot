# Manual CLI setup and continuous history

Manager decision D40, 2026-10-08. The real core/UI Terminal flow is
[accepted](evidence/terminal-integration-r1.md). Compose D39's accepted hook
admission, raw capture and projection into the normal server. Creation and
observation still never start a native CLI; the user manages it in Terminal.

## Parallel ownership and fixed interfaces

1. Setup builder: native-program configuration, agent credentials, managed
   hook/MCP/instruction preparation, normal server/monitor composition and
   runtime observations. It does not implement the reader.
2. Collection builder: `runtime/native_collector.py` and focused tests. Compose
   discovery, capture, archive and projection, without changing their admission.
3. Native autoload explorer: execute the bounded actual-CLI loading checks
   below. Report facts only; no production code or design changes.

`runtime/native_models.py` freezes observation models before dispatch.
`NativeSetup(application)` is passive; `configure_credentials(register)` sets
its internal credential callback. `tick()` attempts setup, `view(agent_id)` is
thread-safe and memory-only, and `close()` clears the cache. `NativeCollector(application)`
is also passive, with `tick()`, `view(agent_id)` and `close()`. Its SQLite I/O
and close run on the same monitor thread. Views become unknown/observation_stale
after five seconds without observation; ordinary agents report unsupported_target.

Setup adds `Application.native_setup` and reserves `Application.native_collector
= None`. `SessionMonitor` accepts explicit optional native_setup/native_collector
objects, ticks them after ownership reconciliation, and closes them in its own
thread's finally block. Bounded operational failures must not stop unrelated
reconciliation; an unexpected fatal failure affects existing monitor readiness.
No new thread, service, scheduler or generic extension system. The normal server
configures setup's callback and passes both objects into the monitor.

After acceptance, Manager replaces the None reservation with NativeCollector(self)
and verifies the combined flow. Public operations remain sixteen, handlers five,
host schema seven. RuntimeView gains nullable native_setup/native_capture fields
using the frozen models. runtime_get copies their cached views only, without
external I/O in its transaction. input_ready stays false until actual dispatch
acceptance; connection and capture are not permission to paste.

## Managed setup

LocalTerminalSettings gains `native_programs: tuple[NativeProgram, ...] = ()`,
at most two distinct providers. Validate pinned preparation versions without
running executables. Preserve old installations, size preflight and no automatic
executable discovery. These are exact executable/version bindings, not model,
effort or selected-provider preferences. Mechanically fix the known legacy test
fixture to omit fields unsupported by its pinned old InstallationSettings,
preserving its receipt replay/no-launch assertions.

Enable setup only for configured onbash after its owned workspace and recorded
shell are available. Use Terminals.native_context and existing lifetime/effects
and workspace guards. Confirm installation, recovery, agent and terminal binding.
A recovery hold leaves files unchanged and reports recovery_review. Cache a
successfully installed binding, avoiding rewrites every second; recheck on
restart or changed binding/configuration. Never start, stop, resume or input a CLI.

Persist a random bearer with at least 32 bytes of entropy in private
`runtime/core-credential.json`, bound to installation_id and agent_id. Reuse
managed atomic write/fsync. Create only when absent; malformed, differently bound
or insecure existing files block setup, never silently rotate/adopt. Never read
owner/provider tokens or ambient credentials. No secrets in repr/logs/events,
API responses, Terminal output or Git.

Credentials gets an internal, thread-safe local digest-to-actor overlay and
`register_local(actor_id, token)`, refusing a digest attributed differently.
Give each ASGI application its own overlay and clear it on lifespan shutdown;
preserve configured credentials/readiness. Register under the actual agent actor,
never the owner. This is attribution for allow_all, not a permission subsystem.
Core MCP URL is the explicit installation loopback `/mcp`; this bearer also
authenticates hook POSTs.

Reuse prepare_manual_hooks with verified ManualBinding, managed HOME/workspace,
configured programs and installed Python `-I -m qorqut_core.runtime.hook_client`.
No preallocated run, fake browser endpoint or legacy run-based preparation.

Add `adapters/manual_core.py::prepare_manual_core` using existing ManagedTree
locks/identity. Its inputs are (roots, binding, core, programs), as for manual
hooks without the sink argv; it returns the tuple of configured paths.
Claude workspace `.mcp.json` gets the qorqut-core HTTP entry;
Codex managed HOME `.codex/config.toml` gets the core server table with
required=false and explicit bearer header. Preserve unrelated servers, hooks,
native trust, model/effort settings and authored text. Claim only that entry/table.
An unowned existing core entry conflicts unless identical. For Codex append the
initial table if absent; on retry compare parsed owned semantics while keeping
all other bytes, including native-added trust. Conflicting owned edits or an
unsupported replacement block instead of rewriting the whole TOML document.
Do not set native model/effort/authentication/permission preferences.

Seed a concise owned instruction block in workspace CLAUDE.md/AGENTS.md for the
configured providers. Preserve authored text and persona.md, memory.md, notes/,
research/, data/. Reference those files, advertised core coordination tools and
the incumbent-conflict instruction. No unimplemented tools/browser endpoints.
Track the exact block; refuse conflicting edits. Reuse the manual-hook identity
marker, not another agent identity registry. Setup ready means configuration
installed only, never login/trust/MCP/session/input readiness. Failures leave the
shell usable and expose blocked/setup_unavailable in runtime_get.

## Continuous collection

Saved session_owners rows are the sole admission authority. Read reserved agent
and current epoch under SQL; perform external I/O outside transactions using
existing lifetime/effects/workspace guards. Roots are agent workspace and
runtime/home; private runtime/native.sqlite3 holds the native spool. No ambient
homes, newest-file selection or cwd-only ownership.

Reconstruct NativeBinding and capture RunBinding from the complete saved owner:
owner_id is run_id, with exact generation, boot, native UUID, version and recovery.
Discovery verifies source ownership spans. Attempt new owners promptly and retry
missing/delayed files. Include closed current-epoch owners, so exit before a tick
and late output remain collectible. Do not fabricate ownership for a rejected
second CLI. Live ownership is not required to drain admitted history.

Per tick: at most sixteen owner discovery attempts and sixteen serviced sources,
each with one MiB fresh polling, sixteen transfer chunks and one MiB projection.
Use round-robin continuation so backlog cannot starve newer work. Cache successful
discovery bindings rather than rescan unchanged whole files every second; retry
missing sources at most once per second/owner and rediscover on a capture request.
On restart reconstruct from durable owners/capture catalog without a new host
cursor/schema. Normal append polling must not rescan the provider store.
If needed, add only `NativeCapture.list_states() -> tuple[NativeState, ...]`, a
read-only inventory of all admitted source epochs using its existing state
reader. This is the sole authorized additive capture seam; no schema or mutation
change. Give an active/new owner priority while retaining fair historical work.

Compose NativeCapture.register/poll/transfer with the registered agent Invocation
and existing exact receipt/ACK path. Do not invent an upload protocol or copy
native files straight into Chat. Enumerate archived native epochs and their
admitted runs, including prior divergent epochs, then call NativeProjector.consume
within the bounds above. Use the earliest linked compatible admitted run as stable
parsing context; resumed ownership must not change existing provenance. Existing
proofs, uniqueness and parse checkpoints govern retries. Preserve unsupported,
malformed, child and attachment gaps; never claim full coverage or parse PTY text
as assistant history.

The conversation ledger already schedules core's daily transcript publication;
the normal job worker writes agents/<id>/transcripts/YYYY-MM-DD.jsonl without a
UI/save action. Core restart resumes the pipeline without duplicate entries or
lost partial records. Recovery, changed workspace ownership and archive conflicts
retain local bytes/intent and stop affected effects, reporting recovery_review or
collection_blocked. Other sources must still progress.

Collection views are observations, not completeness receipts. source_count counts
admitted source IDs. pending_archive_bytes sums captured-minus-ACK bytes across
their epochs; pending_parse_bytes sums archived-minus-handled-record offsets.
Gaps remain visible at zero lag. No owner means waiting_session; an owner awaiting
its file means waiting_source. Do not silently alter accepted capture/parser
contracts; report a concrete missing seam to Manager.

## Bounded checks and combined acceptance

Setup checks: disabled/legacy behavior, stable private credential and actual-agent
HTTP/MCP attribution, preserved config/restart idempotence, malformed config and
recovery holds, normal console installs hooks after Bash without starting a CLI.
Use a few grouped checks, not a new framework or duplicate transport suite.

Collection checks use positively owned temporary children and contract-exact
synthetic Claude/Codex files with existing ownership admission. Cover delayed
creation, append/partial-tail/reopen, resumed UUID under a new owner without
duplicate history, and held transfer. Check host raw receipts, entries and actual
dated bytes. Explicit job pumping is allowed in this service test only, not
claimed as normal-server acceptance. Do not duplicate parser/spool suites.

Exploration verifies standard automatic config loading with fresh isolated
homes/workspaces and existing local fake provider/MCP fixtures: Claude .mcp.json
plus settings.local.json hooks, Codex config/hooks and direct --no-daemon ancestry,
native-selected UUID and necessary native trust dialogs. Reuse the accepted
rehearsal harness; no new emulator, paid calls, owner auth or product auto-trust.
No browser MCP or access/resumption of the safety-blocked browser review.
Report concrete mismatches for Manager decisions, not a redesigned contract.

After implementation/factual acceptance run one actual combined normal-server
flow for each CLI: manual start, hook admission, MCP agent attribution, user and
assistant Chat/daily publication, exit/resume and core restart. Use accepted fake
providers and normal workers. Chat dispatch and source-verified echo association
follow separately. Browser, VM, backup/restore and complete G0–G6 remain open.
