# Managed implementation and review

Manager decision, 2026-10-07. The owner authorized implementation through ACLA,
up to ten concurrent Astra MAX actors, no native self-review, and a separate
Astra MAX reviewer after each completed assignment. This supersedes local Sonnet
defaults for this run. The owner subsequently granted standing authorization on
2026-10-07 to merge and push anything related to this project across all three
repositories. Manager can land reviewed work without asking again. Deployment
and the open VM choice are unchanged; individual worker handoffs still control
which actor may integrate shared files or merge branches.

Run: `649fed24-a7af-40d7-9d41-382ab248483a`. Manager owns plans, decisions,
integration scope and final acceptance. Builders execute a bounded plan;
reviewers report evidence and defects. Neither role changes the design or
delegates further. Treat every report as an unverified claim until checked.

## Accepted bounded results

| Assignment | Accepted revision | Independent review and Manager evidence | Remaining work |
| --- | --- | --- | --- |
| Core durable application foundation | [`ef18e59`](https://github.com/qorqutbot/qorqut-core/blob/ef18e59e2df372963f5fba222156fe6b6e50195f/docs/evidence/foundation-r1.md) | Fresh Astra MAX review found no in-scope defects; its 26-test suite passed from source and the installed wheel. Manager inspected application, transaction, event, job and file boundaries; independently passed six focused durability/concurrency checks and both installed-package diagnostics, including a newer schema present only in WAL. Clean/pushed SHA and all four upstream source hashes verified. | Domain operations, transports, managed resources, restore/import integration and real archive acceptance remain pending. Local process/fault checks are not hardware power-loss certification. No whole-module or G0–G6 acceptance. |
| Environments readiness exploration | [`4432880`](https://github.com/qorqutbot/qorqut-core/blob/44328807c84db01c3351bef74ada285171117d15/docs/evidence/environments-p0.md) | Separate Astra MAX reviewer reported no in-scope defects. Manager checked the exact committed diff and clean worktrees, reproduced unavailable direct KVM access/missing VM tools, verified all seven source hashes and spot-checked external-writer locking/process ownership. | D02 substrate discussion, host preparation and every real two-VM/mount/network/desktop/persistence/restore test remain pending. No G0–G6 acceptance. |
| Browser local effect foundation | [`a94e537`](https://github.com/qorqutbot/qorqut-browser/blob/a94e537519ba19d484375a7f8fb986339149285a/docs/evidence/browser-r1.md) | Separate Astra MAX reviewer found no in-scope defects and ran all 20 tests plus packaging checks. Manager inspected control/store/helper/process paths, independently passed six focused tests and three fault diagnostics, verified all six source hashes and the remote branch SHA. | Independent MCP/HTTP bridge is the next increment. Real helper, Chrome/profile, VNC/RFB, service-cgroup, core and VM/onbash acceptance remain pending. No G0–G6 acceptance. |
| Provider adapters: local preparation and source discovery | [`2db2be6`](https://github.com/qorqutbot/qorqut-core/blob/2db2be6d051e83b383e240aed41424a212153107/docs/evidence/adapters-r1.md) | Fresh Astra MAX review found no remaining in-scope defects across the full implementation and three corrections; all 28 tests and isolated native version/help probes passed. Manager inspected final source/proof/path validation, passed four focused regressions and eight independent rewrite/append scenarios, and verified clean worktrees and the exact pushed SHA. Earlier Manager checks confirmed bounded preparation metadata, aliased-root locking and mixed-primary/child preservation. | Generated configuration loading, native MCP/authentication, process attestation, recorder interaction, live resume and complete child/attachment coverage remain pending. Source descriptors are checked observations; ingestion must revalidate identity/provenance and apply epochs. No M04 or G0–G6 acceptance. |
| Local terminal input/control primitives | [`1704124`](https://github.com/qorqutbot/qorqut-core/blob/170412438596a98845e2fee8ed3f48ce984b899b/docs/evidence/terminal-r1.md) | Fresh Astra MAX review found no remaining in-scope defects across the original implementation and D23/D24 corrections; all 25 tests passed and a negative control detected the prior readiness defect. Manager inspected the final effect, receipt, lock and binding paths; passed five focused real-tmux checks and independently repeated both capture/hold loss diagnostics with no Enter and inert retry. Clean/pushed SHA, whitespace and tmux binary hash verified. | Native composer compatibility, recorder/adapters, managed lifecycle, service/cgroup/VM/onbash and higher-level holds remain pending. Ordered admission observations do not claim atomicity against asynchronous death. Combined package must still validate the foundation dependency lock. No M03 or G0–G6 acceptance. |
| Local transcript spool and nested-PTY recorder | [`08a0273`](https://github.com/qorqutbot/qorqut-core/blob/08a0273cccf172d8f5a3c0d98fdff93a36531521/docs/evidence/recording-r1.md) | Fresh Astra MAX review found no remaining in-scope defects across the original implementation and both D27 corrections; all 22 owning tests passed. Manager inspected final session/startup/restoration and spool/ACK boundaries, passed seven focused checks and independently repeated both former leader-exit diagnostics with every owned member exited, direct child reaped, bytes retained and restoration intact. Clean/pushed SHA, whitespace and all three source hashes verified. | Cooperative session observation is not cgroup quiescence or detached-session supervision. Saved health is historical. Host archive/fsync-before-ACK, native/provider, runner/tmux, systemd/cgroup/VM survival, backups and combined-package integration remain pending. No M07 or G0–G6 acceptance. |
| Core HTTP/MCP/operator transport | [`d2e251c`](https://github.com/qorqutbot/qorqut-core/blob/d2e251c4cd02be91b95c392d225cf52d25c2958a/docs/evidence/core-transports-r1.md) | Fresh Astra MAX review found no remaining in-scope defects; 15 source transport and 41 installed foundation/transport tests passed. Its negative controls detected removal of either response-size guard. Manager inspected final operator, dispatcher/lifetime and MCP boundaries, independently passed four installed HTTP/SDK/CLI/logging/limit flows and reran package/source/pin/NOTICE verification. Clean/pushed SHA and both source hashes verified. | Only four existing operations and serve/call. Native MCP discovery, domain/runtime/browser handoff, streams, UI/VM and combined package integration remain pending. Preserve the historical foundation busy-timeout observation and OS/storage drainage limits. No M10 or G0–G6 acceptance. |
| Combined core package | [`9701c66`](https://github.com/qorqutbot/qorqut-core/blob/9701c661104f7042d0c001796d517957697b5599/docs/evidence/core-integration-r1.md) | Fresh Astra MAX review passed all 117 tests from source and the installed wheel, independently checked child import origins, pins, installed contents and every accepted merge tree. Manager verified ancestry, unchanged production/metadata and all eight authored paths; independently passed the installed shared runner/capture scenario, passive-import check and 36-file/31-pin verification. | Packaging and local table coexistence only. Agents, managed runtime, host archive, native providers, browser/UI and real VM/system gates remain pending. Historical foundation busy-timeout sensitivity remains recorded. |
| Runtime composition exploration | [`39c0f7e`](https://github.com/qorqutbot/qorqut-core/blob/39c0f7e9b261bf38357ca91bc824dba5d0514850/docs/evidence/runtime-composition-r1.md) | Fresh Astra MAX review traced all five assigned questions without findings. Manager checked the report, exact clean/pushed evidence-only diff, 18-module AST inventory, and launch/native ownership, recorder liveness, persisted health and input-admission seams directly in source. | Static exploration only. Managed launch composition, verified native ownership, externally observed recorder readiness, native composer/paste acceptance and source ingestion remain absent. No runtime or system gate advanced. |

Approval confirms bounded evidence, not installed capability. Core baseline
[PR #1](https://github.com/qorqutbot/qorqut-core/pull/1) merged on 2026-10-08 as
`f168369d553f8378df1ba8bc2470a4e1105bf25c`; local `main` is fast-forwarded to it.
Its source, tests and package metadata equal the reviewed `9701c66` candidate;
only Manager tracking differs. Nothing was deployed.
Volatile capacity/port observations
retain their timestamps and are not a deployment budget or reservation.

## Reviewed foundation interfaces

Use `ef18e59` as the concrete core baseline for dependent assignments. Its
immutable [interface/evidence report](https://github.com/qorqutbot/qorqut-core/blob/ef18e59e2df372963f5fba222156fe6b6e50195f/docs/evidence/foundation-r1.md)
defines the accepted signatures. `Application.invoke` is the shared public path;
`Operation`/`Change` and `Application.execute` assemble fixed internal handlers.
Only `config_get`, `config_update`, `operations_get` and `events_list` are currently
public. New domain operations require an explicit assignment and concrete
handlers, not placeholder registrations.

Domain mutations use the supplied `Transaction`, `register_actor`, `append_event`
and fixed `Jobs` handlers. External effects stay outside SQL under installation
ownership; expired external claims require inspection. File callers use
`Files.stage` outside SQL, `attach` within their mutation, verified `install`
outside SQL and `finalize` alongside their domain update. Recovery activation is
explicit maintenance only; historical results and held effects stay preserved.
These are local Python interfaces, not new network services or public tools.

Core's reviewed runtime pin is Pydantic 2.12.3. Combined integration verified the
adapter/runner code against that retained lock in source and installed suites.
Shared migrations, registry,
package metadata and assembly retain one assigned writer at a time. Later lanes
must not invent competing implementations of these accepted boundaries.

The first foundation-dependent assignment is `core-transports-r1`, implementing
the [first core HTTP/MCP contract](CORE-API.md) and a small HTTP operator client
on `ef18e59`. It owns transport/client/entry-point code and required dependency
metadata, not domain rules or migrations. Only the four existing operations are
advertised. Agent/workspace, messaging and later integration handoffs remain
separate Manager assignments; no worker may fill those gaps independently.

## Work awaiting acceptance

- Agent management at `1c983de`: the M01.01–03/workspace implementation is
  committed and pushed, with a fresh Astra MAX review assigned against
  [AGENT-REGISTRY.md](AGENT-REGISTRY.md). Manager verified the clean exact SHA,
  16-path scope and unchanged unrelated production/package files. Builder
  reports 19 owning, 26 foundation and 15 transport source checks plus installed
  agent HTTP/MCP and passive-import checks; these remain unapproved pending
  independent review and Manager verification.
- Independent browser MCP at `c341377`: the D28 logging correction is committed
  and pushed. The fresh reviewer stopped on a provider safety restriction before
  delivering its ACLA report; that review is incomplete and has not been credited.
  The prior review/Manager reproduction found raw malformed request/notification
  payloads in root diagnostics despite clean HTTP errors. Current sanitation and
  wire evidence remain unapproved.
  Synthetic browser effects are not real Chrome/VNC/core/VM acceptance.

## Next implementation round

Manager's next milestone is a headless create → run → capture → message flow.
The following sequencing uses existing module plans; every assignment still
needs a concrete Manager handoff with exact interfaces, owned files and checks.
It does not delegate architecture decisions to builders.

1. The accepted core foundation, transports, adapters, terminal and recorder are
   combined and merged in PR #1 after fresh integration review. Use that reviewed
   baseline for dependent work. Browser's repository integration remains
   separate; never include its unaccepted MCP candidate to complete a bundle.
2. Open the independent builder assignments below on named, reviewed bases.
   Manager freezes their concrete integration interfaces first. One assigned
   writer owns shared migrations, registry, package metadata and application
   assembly; other assignments contribute only their owned modules.
3. Add durable messaging once agent identity/status and input/capture interfaces
   are fixed. Freeze agent/message/conversation/file/browser-request API shapes
   before starting UI work. The UI can then use small synthetic API fixtures;
   actual integrated browser/runtime acceptance remains a separate requirement.
4. Verify the headless flow and browser user-handoff flow, then provider switching
   and recovery. Host/onbash work can progress while D02 is open; it does not
   substitute for the required isolated VM acceptance.

| Builder assignment | Next concrete outcome | Opening dependency |
| --- | --- | --- |
| Integration and application assembly | One compatible core package, concrete assembly, reviewed merges and a focused cross-module flow | Accepted component SHAs; transport candidate only after acceptance |
| Agents and authored files | Stable IDs/onbash, folders, editable persona, status history, icon/display-name metadata and workspace operations | Reviewed foundation and one assigned domain-schema owner |
| Runtime integration | Connect tmux, recorder and provider adapters with verified run identity, readiness and preserved receipts | Reviewed combined package and frozen launch/capture interfaces |
| Durable host archive | Immutable host chunks, fsync-before-ACK, resumable collection and source identity; parsing follows raw durability | Accepted spool and Manager-defined host inventory/transfer interfaces |
| Browser and remote control | Persistent synthetic Chrome profile, dedicated TigerVNC, real control-tool/hold verification and setup | Reviewed browser primitives; MCP/handoff integration waits for accepted transport and core request contract |
| Messaging and handoff | Durable direct/offline messages, guarded delivery and atomic browser request/Done/Cancel notifications | Reviewed agent operations, capture/input contracts and browser protocol |

The next concrete implementation assignment is [AGENT-REGISTRY.md](AGENT-REGISTRY.md)
(`agents-r1`): identity/onbash, authored workspace/persona, presentation/status
history and five shared operations. It alone owns host migration 3, registry and
assembly. Resource/runtime integration stays explicitly unavailable; the owner
has not selected or authorized host preparation for D02. A parallel runtime
exploration may trace the accepted adapter/recorder/tmux interfaces and return
facts, but cannot design or implement new launch protocols/schema. Manager owns
that subsequent design. Both outputs require fresh independent review.

Runtime exploration is now accepted at `39c0f7e`. Its first resulting
implementation assignment is [RECORDER-PROBE.md](RECORDER-PROBE.md): an optional
fresh process-bound query in the existing recorder loop. It owns only local
transcript/probe code and tests; M01 retains exclusive host schema/registry
ownership. Native ownership/composer/source integration and managed launch
remain later explicit designs. This is not a declaration that those capabilities
are implemented or ready.

UI becomes a builder assignment as the domain API stabilizes, taking a released
slot. VM installation/lifecycle waits for the pending substrate decision. Later
backup/restore and continuity work likewise follows its actual dependencies.

Expect 6–8 active ACLA workers normally, with a hard maximum of ten across
builders and reviewers: usually 4–6 builders plus 2–4 fresh independent reviewers.
Start fewer during shared-base integration and whenever prerequisites are open.
Completed builders stop edits during review; an open assignment is not evidence
of active computation. All use Astra MAX with native self-review disabled.
These are expected allocations, not claims that new actors have been launched.

## Repository baseline

The private `qorqut-core` and `qorqut-browser` repositories now exist. Core `main`
contains the reviewed primitive package from PR #1; browser's initial `main`
still contains documentation only. Core owns global plans/contracts and
core module plans; browser owns its browser work package; UI retains its UI plan.
The UI document-relocation branch is based on the existing unmerged planning
branch. Original plans remain available at UI commit `e7d2b56`.

Implementation uses isolated `worktrees/<assignment>` branches with `codex/`
names. No implementation is written directly on `main`. One actor owns package
assembly and shared files in each repository. File moves/feature-branch rebases
for integration require Manager direction; actors never pull another lane's
unfinished changes or merge branches unless their Manager handoff assigns that
action. Core PR #1 is merged; other planning/candidate branches retain their
individual recorded state. Standing authorization allows Manager to land
reviewed changes as their integration checks pass.

## First round

| Assignment | Role and bounded outcome | Owned paths | Acceptance boundary |
| --- | --- | --- | --- |
| foundation | Implement M00 package, typed config/errors, SQLite migrations, idempotent operations/configuration, events/jobs and crash-safe file receipts | Core root package metadata; `config.py`, `contracts.py`, `app.py`, `storage/`; matching tests/evidence | Durable storage and shared callable operations, no advertised stub tools or live services |
| environments | Explore M02 P0 host readiness and pinned source facts; return factual prerequisite evidence | `docs/evidence/environments-p0.md` only | No VM choice, package installation, libvirt/network changes, VM boot or readiness claims without evidence |
| terminal | Implement the M03 local process/tmux, persistent guarded-input and control primitives | `runner/`; matching tests/evidence | Actual isolated tmux and crash receipts; service/cgroup/VM acceptance remains pending |
| adapters | Implement M04 bounded local preparation, version/capability probes and source discovery from managed homes | `adapters/`; matching synthetic fixtures/tests/evidence | Preserve authored files, register two MCP endpoints, no model turns or personal native stores |
| recording | Implement M07 P0 recorder and atomic local source/chunk spool | `transcripts/`; matching tests/evidence | Durable-before-display, real PTY, source lineage, ACK validation; host archive/G3 remains pending |
| browser | Implement M06 local configuration/binding, command/control receipts and fixed-profile helper primitives; inspect prerequisites | Browser package metadata and local `contracts.py`, `state.py`, `control.py`, `supervisor.py`, `tools.py`; matching tests/evidence | One local input gate and conservative uncertain effects; full MCP/relay/core integration follows its reviewed contract |

These are six bounded assignments, not declarations that six modules are done.
Remaining capacity is available for independent reviewers. Later rounds open
agents/files, messaging/transports, UI, and operations/recovery as their concrete
dependencies pass review. A completed exploration assignment receives the same
independent evidence review as code. An unavailable prerequisite is reported,
not silently simulated into a passing integration result.

The accepted browser increment unlocks `browser-mcp-r2`: an official standalone
MCP server, local identity/command/control routes and optional application
handoff client, all using the reviewed gate. Browser owns its concrete v1 wire
contract in `docs/PROTOCOL.md` on `codex/browser-mcp-plan`. Core's future handoff
operation implements that endpoint and retains request/message transaction
authority. Relay, desktop provisioning and a substitute core implementation are
outside this bounded server assignment.

## Initial implementation decisions

Use Python 3.12, Pydantic v2 and stdlib SQLite. Foundation owns the core root
`pyproject.toml`, lock selection, package initializer, host assembly and shared
models. Other core lanes create only their named subpackages and focused tests;
they may run them with `src` on the import path and an isolated virtualenv until
package integration. Browser owns its independent package metadata. No sibling
Qorqut runtime import is allowed.

Do not add a speculative service or interface framework. At this first boundary,
local runner, adapter, recorder and browser primitives accept explicit immutable
bindings/configuration and structured arguments. They have no dependency on a
live host application. Concrete Python signatures stay module-local until
Manager reviews and freezes integration; they are not independently invented
network contracts. Cross-module gaps go to Manager before dependent work.

Runner and recorder ultimately share one local SQLite file. For this round their
local tables use `runner_` and `capture_` prefixes, respectively, with independent
idempotent `ensure_schema(connection)` initialization. Neither writes competing
global `user_version` metadata or creates a second spool database by default.
Every connection enforces WAL, foreign keys, FULL synchronization and bounded
busy waits. Their APIs accept the configured database path/connection rather
than guessing a global data root. Foundation's host database is separate.

Terminal primitives require exact agent/recovery/environment/boot/run bindings
and process fingerprints, never names or PIDs alone. Automated input requires
both capture readiness and an explicit adapter composer verdict. Until integrated,
these are injected narrow callables and the production default is not ready.
Receipt retry never repastes an uncertain attempt. No terminal helper starts a
model as a side effect of inspect or import.

Recorder owns the child PTY and output spool; runner owns tmux/process transport.
The recorder entry is `python -m qorqut_core.transcripts.recorder`, with structured
argv after `--` and explicit database/source/run identity options. It does not
wrap the host collector, parse conversations, or own agent lifecycle. Producer
pause/cgroup survival requires a later integrated rehearsal; ordinary process
tests cannot certify it.

Adapters prepare only explicitly supplied managed roots. Generated provider
entrypoints refer to `persona.md`; neither replaces it nor copies credentials.
Both MCP endpoints are independent. Native source discovery is confined to the
managed home and verified run/native identities; a matching cwd or newest file
is not ownership. Unknown versions/capabilities remain unsupported. Archive
parsers belong to recording, not a second adapter history store.

Browser remains an independent MCP service in the final product. The first
package increment implements its local durable effect boundary before its MCP
transport: one gate/receipt path must later serve direct tools and proxied
commands alike. It accepts fixed loopback CDP and owned profile/display settings,
does not spawn a default profile, and rejects wrong binding/control epochs.
Startup after a crash conservatively reconciles saved ownership and uncertain
commands. Only explicit configured application reconciliation can reopen
managed automation. This increment implements no competing request/message
database, handoff HTTP schema, VNC input filter or core integration. No stub MCP
tools are advertised. The following browser assignment adds the official SDK
server and handoff/control wire contract after Manager freezes those interfaces.

The VM choice remains open. D02 stays a planning default, not installation
authorization. Environment exploration can inspect executable/device/access,
memory/disk/network availability and source recipes, without changing host
configuration. No paid provider calls, owner logins, live Qorqut sessions or
production agent data are test fixtures.

## Review and integration gate

1. Builder commits its owned implementation, minimal meaningful tests and exact
   evidence. It reports base/final SHAs, changed paths, commands/results,
   unresolved limits and scope completion once through ACLA. No interim reports.
2. Manager freezes that completed SHA and launches a fresh ACLA review actor on
   a separate worktree at that SHA, with the full original handoff/amendments.
   Reviewer uses Astra MAX, `reviewLoop=false`, and no nested delegation.
3. Reviewer checks requirements against actual code and evidence. It may run
   the focused checks against temporary data, without editing tracked files,
   source worktrees, global configuration, deployments or live services. Report
   concrete defects with file/line/trigger, test evidence and uncertainty. Do not
   redesign, expand scope, or equate omitted future phases with current defects.
4. Manager independently verifies findings and important durability/identity
   guarantees. Confirmed defects go back to the original builder through ACLA;
   a revision is independently reviewed at its new completed SHA. Unsupported
   findings are rejected with evidence. A reviewer vote is not acceptance.
5. Approve each completed actor explicitly through ACLA. Record accepted SHAs and
   evidence before assigning integration. A passing primitive/fixture test does
   not satisfy G0–G6's real-system requirements. G3 still gates unattended fleet
   use. Manager may merge and push accepted integration under the owner's standing
   authorization. Deployment remains a separate unperformed action.

Use notification-driven ACLA handoffs. Manager ends its turn after dispatch and
resumes for completion/blocker messages; it does not poll or inspect unfinished
work. At most ten builders/reviewers execute concurrently across this run.
