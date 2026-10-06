# Managed implementation and review

Manager decision, 2026-10-07. The owner authorized implementation through ACLA,
up to ten concurrent Astra MAX actors, no native self-review, and a separate
Astra MAX reviewer after each completed assignment. This supersedes local Sonnet
defaults for this run. It does not authorize merges or deployment.

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

Approval of this report confirms its bounded evidence, not installed capability.
The branch is pushed and remains unmerged. Volatile capacity/port observations
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

Core's reviewed runtime pin is Pydantic 2.12.3. Earlier isolated adapter/runner
checks used 2.13.5; their eventual combined package must be verified against the
chosen core lock before integration is accepted. Shared migrations, registry,
package metadata and assembly retain one assigned writer at a time. Later lanes
must not invent competing implementations of these accepted boundaries.

The next foundation-dependent assignment is `core-transports-r1`, implementing
the [first core HTTP/MCP contract](CORE-API.md) and a small HTTP operator client
on `ef18e59`. It owns transport/client/entry-point code and required dependency
metadata, not domain rules or migrations. Only the four existing operations are
advertised. Agent/workspace, messaging and later integration handoffs remain
separate Manager assignments; no worker may fill those gaps independently.

## Work awaiting acceptance

- Independent browser MCP at `d46fba1`: the separate Astra MAX review passed all
  32 tests and package checks, but found raw malformed MCP payloads in root-logger
  diagnostics. Manager independently reproduced both request/notification paths.
  D28 and the first browser MCP revision require complete SDK log sanitation;
  the same evidence clarifies core's existing transport logging requirement.
  Implementation remains unapproved and needs fresh review after correction.
  Synthetic browser effects are not real Chrome/VNC/core/VM acceptance.
- Recorder at `a8576ec`: the D27 correction is committed and pushed; a fresh
  Astra MAX reviewer is assigned to that immutable SHA with both cleanup and
  scoped-membership amendments. The earlier review/Manager reproductions found
  cleanup omitting the direct child, surviving original-group members and lost
  startup TERM. The new bounded-cleanup, signal-mask and regression claims await
  independent and Manager verification. Implementation remains unapproved;
  host archive/G3 acceptance is pending.

## Repository baseline

The private `qorqut-core` and `qorqut-browser` repositories now exist. Their initial
`main` commits contain documentation only. Core owns global plans/contracts and
core module plans; browser owns its browser work package; UI retains its UI plan.
The UI document-relocation branch is based on the existing unmerged planning
branch. Original plans remain available at UI commit `e7d2b56`.

Implementation uses isolated `worktrees/<assignment>` branches with `codex/`
names. No implementation is written directly on `main`. One actor owns package
assembly and shared files in each repository. File moves/feature-branch rebases
for integration require Manager direction; actors never pull another lane's
unfinished changes or merge branches themselves. PRs remain draft and unmerged.

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
   use. Merge and deployment remain separate unperformed actions.

Use notification-driven ACLA handoffs. Manager ends its turn after dispatch and
resumes for completion/blocker messages; it does not poll or inspect unfinished
work. At most ten builders/reviewers execute concurrently across this run.
