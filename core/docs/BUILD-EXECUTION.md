# Managed implementation and review

Accepted and integrated: D46 [host dispatch](evidence/host-message-dispatch-manager-acceptance.md)
and D48 [continued native collection](evidence/native-collection-continuity-manager-acceptance.md).
The [actual core/MCP/file-card flow](https://github.com/qorqutbot/qorqutbot-ui/blob/main/docs/evidence/file-reference-integration-manager-acceptance.md)
also passed bounded acceptance. Next, two parallel [connected Chat checks](CONNECTED-CHAT-ACCEPTANCE.md)
exercise manually started Claude and Codex through real MCP calls, replies,
automatic dated history and core restart. Manager reviews/tests directly;
workers use Astra MAX with self-review off. Core has schema 9, 17 public
operations and five handlers. These component results do not yet establish
native Chat dispatch or complete transcript coverage. Shutdown drain,
child/attachment support, browser/VM integration and full system gates remain open.

Latest owner direction (2026-10-08): maximize useful parallel implementation;
Manager reviews and tests completed work directly. New separate review actors
are optional, not a gate. Keep Astra MAX builders and self-review disabled.
[D39](NATIVE-CONNECTION.md) assigns native hook admission, capture/transfer, and
conversation parsing/publication to three independent lanes while two builders
finish core/UI terminal geometry. Merge only after Manager acceptance, then run
the combined workflow. Earlier evidence below retains its actual review history.

## D40 composition progress

The [native collector service](evidence/native-collector-r1.md) and
[standard-config loading evidence](evidence/manual-native-autoload-r1.md) have
passed Manager direct review. Collector owning checks passed independently.
Codex standard paths were observed; Claude loading is qualified to the explicit
fixture-only post-onboarding continuation, with the fresh-home failure preserved.
Managed setup is also accepted. Manager composed both services in the normal
server and twelve focused checks passed. Two parallel provider assignments now
verify [manual CLI startup](NATIVE-WORKFLOW-ACCEPTANCE.md) through hooks/MCP,
native raw capture, Chat and automatic dated files. A third facts-only assignment
checks the remaining native input and terminal archive connections for Manager
design. No native workflow acceptance follows from these
component results; Chat dispatch, browser, VM and full system gates remain open.

## Current work after owner scope agreement

D36 is accepted: [Terminal-owned sessions](TERMINAL-OWNED-SESSIONS.md). Core and
UI manual-creation implementations have passed scoped review, including the
core test's synchronization correction and fresh Astra MAX review. The
[combined core/UI check](MANUAL-CREATION-INTEGRATION.md) at
`5a50666` with UI `6a4841d` has passed fresh Astra MAX review and
[Manager acceptance](evidence/manual-creation-integration-manager-acceptance.md).
The accepted pair is integrated with the separately reviewed shell and adapter
changes. Native rehearsal
corrections passed [Manager acceptance](evidence/native-rehearsal-manager-acceptance.md).
Its product observations are not a completed runtime.

D37's independent [inbox ownership](INBOX-OWNERSHIP.md),
[recorded shell](RECORDED-SHELL.md) and
[adapter corrections](ADAPTER-REHEARSAL-FIXES.md) increments are accepted. Adapter
revision `9737e2a` passed fresh Astra MAX review and
[Manager checks](evidence/adapter-rehearsal-manager-acceptance.md), including the
final manifest-write race correction. The accepted paired creation check includes
actual browser/core sessions, automatic dated files and normal restart.
These completed interfaces feed
one explicitly owned runtime integration, followed by the actual Terminal UI
and onbash flow check; the existing ten-actor cap remains.
No automatic CLI orchestration or context-packet work is authorized by old plans.
The VM decision remains separate; common integration starts with onbash.

The parallel startup-hook and native-conversation source explorations have both
passed separate Astra MAX reviews and
[Manager verification](evidence/manual-runtime-seams-manager-acceptance.md).
They identify the remaining connection points without adding runtime behavior
or delegating the following implementation design.

D38's two implementation lanes have passed separate fresh Astra MAX reviews.
The core candidate `003c5bad` has
[Manager acceptance](evidence/terminal-server-manager-acceptance.md); UI candidate
`2faabdf` is accepted and merged at UI main `e380b4c`. The four core corrections
from the initial review are resolved. The next gate is an actual compiled-browser/
installed-core [flow](TERMINAL-INTEGRATION.md) against this accepted pair and the frozen
[Terminal protocol](TERMINAL-SERVER.md). Native startup/inbox association,
Chat dispatch and native history follow this usable-terminal step.

Combined testing exposed finding 217: a narrow read-only emulator clips a wider
pane, and mirrors need explicit native dimensions. Manager's
[geometry contract](TERMINAL-GEOMETRY.md) freezes protocol 2 and assigns core
dimension/mirror synchronization and UI scrolling/render ordering in parallel.
Both need fresh reviews before the original integration actor resumes with the
new accepted pair. The notice relocation is a separate bounded packaging fix.

The local onbash ownership increment is now accepted at `1af5f85`, with fresh
Astra MAX review and [Manager checks](evidence/inbox-ownership-manager-acceptance.md).
It adds schema 6 and `runtime_get`, preserving five handlers. Hook association,
recorded-shell composition, delivery and native capture remain integration work.

The recorded shell is accepted at `395d831`, with fresh Astra MAX review and
[Manager checks](evidence/recorded-shell-manager-acceptance.md). It provides local
recorded Bash execution and bounded human attachments. D38 now composes it in
the normal host server and supplies the Terminal UI; their combined running
flow is awaiting acceptance. Native startup hooks remain separate work.

Manager decision, 2026-10-07. The owner authorized implementation through ACLA,
up to ten concurrent actors, no native self-review, and a separate reviewer after
each completed assignment. Initial assignments used the owner's Astra MAX
override. After a temporary Sonnet launch policy on 2026-10-08, the owner's
latest explicit correction is "only ASTRA MAX PLEASE". All new builders and
reviewers use Codex gpt-6-astra/max with reviewLoop=false. The unaccepted Sonnet
manual-creation review is stopped and superseded by a fresh Astra MAX review;
no actor is converted in place. Historical accepted reviews retain their actual
model provenance. The owner previously granted standing authorization on
2026-10-07 to merge and push anything related to this project across all three
repositories. Manager can land reviewed work without asking again. Deployment
and the open VM choice are unchanged; individual worker handoffs still control
which actor may integrate shared files or merge branches.

Run: `649fed24-a7af-40d7-9d41-382ab248483a`. Manager owns plans, decisions,
integration scope and final acceptance. Builders execute a bounded plan;
reviewers report evidence and defects. Neither role changes the design or
delegates further. Treat every report as an unverified claim until checked.

## Connected chat assignments

Manager's next work packages are D34's message/conversation/daily-file domain,
D35's server worker/session/static integration, and the minimal UI in
`qorqutbot-ui/docs/SHELL-IMPLEMENTATION.md`. The first core builder alone owns
host migration 5 and application assembly. The server builder owns transport
and job-runner helpers without editing that schema; UI owns its repository.
All three use the frozen operation/session contracts and receive separate
fresh reviews. Native prefix and console hosting have passed their bounded
reviews and Manager checks. The corrected chat domain has also passed independent
review and Manager checks. Their combination has now passed
[running-server verification](CHAT-SERVER-INTEGRATION.md) and independent review;
[Manager acceptance](evidence/chat-server-manager-acceptance.md) records the
automatic publication and restart evidence for main integration.
UI's uncertainty-retention correction passed fresh review at `cbff84d`.
Manager's real compiled-UI/core checks also passed against `faf311d`: actual
cookie sessions, creation/rename/icons, messaging/onbash, automatic dated files,
normal restart and desktop/mobile layouts. Evidence lives in the UI repository's
`docs/evidence/ui-shell-manager-acceptance.md`; native execution remains separate.
None of these assignments declares VM/native/browser control complete.

## Accepted bounded results

| Assignment | Accepted revision | Independent review and Manager evidence | Remaining work |
| --- | --- | --- | --- |
| Manual creation with actual compiled UI and schema-6 core | Core `5a50666`, UI `6a4841d` | Fresh Astra MAX review passed historical migration/replay, installed HTTP/MCP creation and both actual Chrome/core modes. Manager verified package/UI/artifact bytes, passed historical and installed creation checks, and inspected desktop/mobile screenshots. [Acceptance](evidence/manual-creation-integration-manager-acceptance.md). | User-started CLI association, shell activation, Terminal transport/UI, actual Chat delivery and native assistant ingestion. No deployment or whole-system acceptance. |
| Native config preservation and captured composers | `9737e2a` | Fresh Astra MAX review passed 32 source and five installed checks plus generated-mode race and original-fixture diagnostics. Manager matched 56 package files, passed two installed preservation/composer checks and reproduced rejection of the original final-write race. [Acceptance](evidence/adapter-rehearsal-manager-acceptance.md). | Offline compatibility only; `native_verified` and `permits_paste` remain false. Startup association, live input and native ingestion still require integration. |
| Persistent recorded shell and human attachment | `395d831` | Fresh Astra MAX review passed seven source and two installed checks, including actual recorder import provenance. Manager inspected the implementation, matched 59 package files and passed two installed process-restart/capture and uncertain-input/recorder-failure checks. [Acceptance](evidence/recorded-shell-manager-acceptance.md). | Host activation, startup/native association, Terminal transport/UI, Chat delivery, native ingestion, VM/browser and service-lifetime integration. |
| Identity-only creation and immutable historical replay | `8d2039f` | Fresh Astra MAX review of the corrected flow found no defects. Manager verified 56 installed package files and passed the formerly failing timing scenario against the real server worker. [Scoped acceptance](evidence/manual-creation-manager-acceptance.md). | Combined schema-6 core/UI acceptance is recorded above; native Terminal/Chat integration remains separate. |
| Local onbash inbox ownership | `1af5f85` | Fresh Astra MAX review found no in-scope defects; twelve runtime and three inventory/schema checks plus an installed real-child flow passed. Manager verified 61 package files and passed four installed ownership/recovery checks. [Acceptance](evidence/inbox-ownership-manager-acceptance.md). | Native hook/process association, shell composition, delivery, assistant capture, Terminal UI and VM/browser integration. `input_ready` remains false. |
| Pinned native CLI rehearsal | `13227a8` | Fresh Sonnet 5.5 xhigh review and [Manager evidence acceptance](evidence/native-rehearsal-manager-acceptance.md); actual loopback-fake-provider fresh/resume observations, with fixture boundaries and timing corrections. | Manual startup/inbox/Chat integration, composer/config fixes, native reader/normalization, complete source coverage, actual provider and VM acceptance. |
| First connected UI and actual core integration | UI [`cbff84d`](https://github.com/qorqutbot/qorqutbot-ui/blob/cbff84d1e92066d13f9ff5322b49a32fc7f0203a/docs/evidence/ui-shell-r1.md), core `faf311d` | Fresh Sonnet 5.5 medium review passed locked build and six grouped Chrome checks. Manager matched 40 UI and 56 installed core files, reproduced build hashes and passed two actual Chrome/core flows with no mock API: metadata, three messages, both dated files, restart, sessions and responsive layouts. [Manager acceptance](https://github.com/qorqutbot/qorqutbot-ui/blob/main/docs/evidence/ui-shell-manager-acceptance.md). | Native assistant replies/delivery, browser/file cards, VM/browser execution, continuity/export/restore and full system gates remain pending. |
| Running-server chat publication | [`4e969f9`](https://github.com/qorqutbot/qorqut-core/blob/4e969f944cd5736de7a42ead0ebc60592804b5c3/docs/evidence/chat-server-integration-r1.md) | Fresh Sonnet 5.5 xhigh review passed 30 source transport tests, source messaging, five installed checks and child-provenance checks without blocking findings. Manager inspected all six changed paths, verified unchanged product bytes, 158 tracked files, 56 installed files and 31 pins, then passed both installed publication/restart flows. [Manager acceptance](evidence/chat-server-manager-acceptance.md). | Real UI/backend browser testing; native assistant capture/delivery, VM/browser lifecycle, continuity/export/restore and full system gates remain pending. |
| Durable messages and dated conversation files | [`5a79f48`](https://github.com/qorqutbot/qorqut-core/blob/5a79f480b448c1eb3d4172e6e83947146015283d/docs/evidence/chat-domain-r1.md) | Fresh Astra MAX review passed 22 source and four installed checks without remaining findings. Manager reviewed the production/schema change, matched 142 tracked and 50 installed Python files plus 29 runtime pins, passed two installed message/publication checks and replayed both corrected failure diagnostics. [Manager acceptance](evidence/chat-domain-manager-acceptance.md). | Combined running-server publication and real UI checks; native assistant capture/delivery, browser/VM lifecycle, export/restore and system gates remain pending. |
| Headless jobs, console sessions and optional static UI | [`b51f309`](https://github.com/qorqutbot/qorqut-core/blob/b51f3099c2cb4ed6690667882cd1bcad63cf1675/docs/evidence/console-server-r1.md) | Fresh Astra MAX review found no defects; 28 final-source tests, five installed checks and a real file-effect/mutation cancellation diagnostic passed. Manager inspected the complete production change, verified 133 tracked files, 46 installed Python files, 31 pins and the clean/pushed SHA, then passed two installed static-serving/startup-cancellation checks. [Manager acceptance](evidence/console-server-manager-acceptance.md). | Actual chat publisher and UI integration, native collection/delivery/replies, browser/VM runtime and system gates remain pending. No deployment or whole G0–G6 acceptance. |
| Native receipt and file-prefix verification | [`c4ac16d`](https://github.com/qorqutbot/qorqut-core/blob/c4ac16d6fe1963774026f43a74b5fa491b5c5f89/docs/evidence/native-prefix-r1.md) | Fresh Astra MAX review passed nine prefix and nine existing spool source tests plus nine installed prefix tests. Manager checked the complete production diff, exact clean/pushed scope, unchanged prior spool AST and independent wheel bytes, then passed three separate reclaim/concurrency/mutation checks. [Manager acceptance](evidence/native-prefix-manager-acceptance.md). | Read-only comparison only. Native ownership, reader/checkpoints, collection, normalization and runtime/system gates remain pending. |
| Core durable application foundation | [`ef18e59`](https://github.com/qorqutbot/qorqut-core/blob/ef18e59e2df372963f5fba222156fe6b6e50195f/docs/evidence/foundation-r1.md) | Fresh Astra MAX review found no in-scope defects; its 26-test suite passed from source and the installed wheel. Manager inspected application, transaction, event, job and file boundaries; independently passed six focused durability/concurrency checks and both installed-package diagnostics, including a newer schema present only in WAL. Clean/pushed SHA and all four upstream source hashes verified. | Domain operations, transports, managed resources, restore/import integration and real archive acceptance remain pending. Local process/fault checks are not hardware power-loss certification. No whole-module or G0–G6 acceptance. |
| Environments readiness exploration | [`4432880`](https://github.com/qorqutbot/qorqut-core/blob/44328807c84db01c3351bef74ada285171117d15/docs/evidence/environments-p0.md) | Separate Astra MAX reviewer reported no in-scope defects. Manager checked the exact committed diff and clean worktrees, reproduced unavailable direct KVM access/missing VM tools, verified all seven source hashes and spot-checked external-writer locking/process ownership. | D02 substrate discussion, host preparation and every real two-VM/mount/network/desktop/persistence/restore test remain pending. No G0–G6 acceptance. |
| Browser local effect foundation | [`a94e537`](https://github.com/qorqutbot/qorqut-browser/blob/a94e537519ba19d484375a7f8fb986339149285a/docs/evidence/browser-r1.md) | Separate Astra MAX reviewer found no in-scope defects and ran all 20 tests plus packaging checks. Manager inspected control/store/helper/process paths, independently passed six focused tests and three fault diagnostics, verified all six source hashes and the remote branch SHA. | Independent MCP/HTTP bridge is the next increment. Real helper, Chrome/profile, VNC/RFB, service-cgroup, core and VM/onbash acceptance remain pending. No G0–G6 acceptance. |
| Provider adapters: local preparation and source discovery | [`2db2be6`](https://github.com/qorqutbot/qorqut-core/blob/2db2be6d051e83b383e240aed41424a212153107/docs/evidence/adapters-r1.md) | Fresh Astra MAX review found no remaining in-scope defects across the full implementation and three corrections; all 28 tests and isolated native version/help probes passed. Manager inspected final source/proof/path validation, passed four focused regressions and eight independent rewrite/append scenarios, and verified clean worktrees and the exact pushed SHA. Earlier Manager checks confirmed bounded preparation metadata, aliased-root locking and mixed-primary/child preservation. | Generated configuration loading, native MCP/authentication, process attestation, recorder interaction, live resume and complete child/attachment coverage remain pending. Source descriptors are checked observations; ingestion must revalidate identity/provenance and apply epochs. No M04 or G0–G6 acceptance. |
| Local terminal input/control primitives | [`1704124`](https://github.com/qorqutbot/qorqut-core/blob/170412438596a98845e2fee8ed3f48ce984b899b/docs/evidence/terminal-r1.md) | Fresh Astra MAX review found no remaining in-scope defects across the original implementation and D23/D24 corrections; all 25 tests passed and a negative control detected the prior readiness defect. Manager inspected the final effect, receipt, lock and binding paths; passed five focused real-tmux checks and independently repeated both capture/hold loss diagnostics with no Enter and inert retry. Clean/pushed SHA, whitespace and tmux binary hash verified. | Native composer compatibility, recorder/adapters, managed lifecycle, service/cgroup/VM/onbash and higher-level holds remain pending. Ordered admission observations do not claim atomicity against asynchronous death. Combined package must still validate the foundation dependency lock. No M03 or G0–G6 acceptance. |
| Local transcript spool and nested-PTY recorder | [`08a0273`](https://github.com/qorqutbot/qorqut-core/blob/08a0273cccf172d8f5a3c0d98fdff93a36531521/docs/evidence/recording-r1.md) | Fresh Astra MAX review found no remaining in-scope defects across the original implementation and both D27 corrections; all 22 owning tests passed. Manager inspected final session/startup/restoration and spool/ACK boundaries, passed seven focused checks and independently repeated both former leader-exit diagnostics with every owned member exited, direct child reaped, bytes retained and restoration intact. Clean/pushed SHA, whitespace and all three source hashes verified. | Cooperative session observation is not cgroup quiescence or detached-session supervision. Saved health is historical. Host archive/fsync-before-ACK, native/provider, runner/tmux, systemd/cgroup/VM survival, backups and combined-package integration remain pending. No M07 or G0–G6 acceptance. |
| Core HTTP/MCP/operator transport | [`d2e251c`](https://github.com/qorqutbot/qorqut-core/blob/d2e251c4cd02be91b95c392d225cf52d25c2958a/docs/evidence/core-transports-r1.md) | Fresh Astra MAX review found no remaining in-scope defects; 15 source transport and 41 installed foundation/transport tests passed. Its negative controls detected removal of either response-size guard. Manager inspected final operator, dispatcher/lifetime and MCP boundaries, independently passed four installed HTTP/SDK/CLI/logging/limit flows and reran package/source/pin/NOTICE verification. Clean/pushed SHA and both source hashes verified. | Only four existing operations and serve/call. Native MCP discovery, domain/runtime/browser handoff, streams, UI/VM and combined package integration remain pending. Preserve the historical foundation busy-timeout observation and OS/storage drainage limits. No M10 or G0–G6 acceptance. |
| Combined core package | [`9701c66`](https://github.com/qorqutbot/qorqut-core/blob/9701c661104f7042d0c001796d517957697b5599/docs/evidence/core-integration-r1.md) | Fresh Astra MAX review passed all 117 tests from source and the installed wheel, independently checked child import origins, pins, installed contents and every accepted merge tree. Manager verified ancestry, unchanged production/metadata and all eight authored paths; independently passed the installed shared runner/capture scenario, passive-import check and 36-file/31-pin verification. | Packaging and local table coexistence only. Agents, managed runtime, host archive, native providers, browser/UI and real VM/system gates remain pending. Historical foundation busy-timeout sensitivity remains recorded. |
| Runtime composition exploration | [`39c0f7e`](https://github.com/qorqutbot/qorqut-core/blob/39c0f7e9b261bf38357ca91bc824dba5d0514850/docs/evidence/runtime-composition-r1.md) | Fresh Astra MAX review traced all five assigned questions without findings. Manager checked the report, exact clean/pushed evidence-only diff, 18-module AST inventory, and launch/native ownership, recorder liveness, persisted health and input-admission seams directly in source. | Static exploration only. Managed launch composition, verified native ownership, externally observed recorder readiness, native composer/paste acceptance and source ingestion remain absent. No runtime or system gate advanced. |
| Agent registration, authored workspace and status | [`1c983de`](https://github.com/qorqutbot/qorqut-core/blob/1c983de4c84c9cabded4a870c0d3422c11f640dc/docs/evidence/agents-r1.md) | Fresh Astra MAX review found no in-scope defects; 19 agent, 26 foundation and 15 transport source tests plus installed HTTP/MCP and passive-import checks passed. Manager reviewed the full production change, independently verified all 40 installed package files, test staging and 31 pins against the candidate, and passed four installed checks for concurrent registration, authored-file preservation, projection races and read-only recovery holds. Exact clean/pushed 16-path scope verified. | Five shared agent operations and real directory/projection effects only. Workspace initialization retains provisioning/start intent and a non-automatic dependency hold. Presence, runtime/VM launch, browser/messaging, general file APIs and archive/restore remain pending; no whole M01/M09 or G0–G6 acceptance. |
| Live local recorder observation | [`7a678ec`](https://github.com/qorqutbot/qorqut-core/blob/7a678ec62d8df0e062f8858ba480c762892cca72/docs/evidence/recorder-probe-r1.md) | Fresh Astra MAX review found no in-scope defects; all 33 final-source transcript tests and 11 installed probe tests passed, including the unsigned credential correction. Manager inspected all seven changed files, independently verified the installed package/test bytes and 31 pins, and passed four installed checks for paused-loop timeout, observer failure, credential/reply bounds and a different live responder. No fixture descendants, sockets or data remained. | Fresh terminal-loop observation only. Native ownership/readiness/ingestion, managed launch/input admission, host archive/backlog policy and cgroup/VM/system acceptance remain pending. Timeout is not proof of death; no lease or G0–G6 acceptance. |
| Native source and checkpoint exploration | [`a05e67a`](https://github.com/qorqutbot/qorqut-core/blob/a05e67a147c5b368bfac6090f65a1b1beb6a3fe1/docs/evidence/native-capture-p0.md) | Fresh Astra MAX review found no remaining in-scope defects after the initial root-exception statement was corrected. Manager read the full report and discovery/spool source, verified the exact clean/pushed report-only diff, all 20 recorded fingerprints and the AST exception boundaries. Static checks only; no product tests or runtime changes. | Discovery returns observations without a retained descriptor or capture mapping. Durable native checkpoints, full-prefix verification after reclamation, source-change handling and native finalization remain unimplemented. The report informs Manager design; no runtime or G0–G6 acceptance. |
| Durable host transcript intake | [`70ff636`](https://github.com/qorqutbot/qorqut-core/blob/70ff636ea1bc45fac6a92077f43f481b27c34e39/docs/evidence/host-archive-r1.md) | Fresh Astra MAX review found no in-scope defects; 21 archive, 26 foundation and 19 agent final-source tests plus five installed checks passed. Manager reviewed the full production change and verified the exact independent wheel, 42 Python files plus NOTICE, 31 pins and clean/pushed 12-path scope. Four separate installed checks passed for orphan/lost-response replay with real spool reclamation, original-run attribution/conflict retention, restore holds and damaged/aliased objects. | Four passive internal callables and host migration 4; nine public operations/three handlers remain unchanged. Authenticated network collection, native ingestion/checkpoints, full integrity/recovery audit, hold release, backup/import and G0–G6 acceptance remain pending. File/SQL fault tests do not certify hardware power loss. |

Approval confirms bounded evidence, not installed capability. Core baseline
[PR #1](https://github.com/qorqutbot/qorqut-core/pull/1) merged on 2026-10-08 as
`f168369d553f8378df1ba8bc2470a4e1105bf25c`; local `main` is fast-forwarded to it.
That merge's source, tests and package metadata equal the reviewed `9701c66`
candidate; only Manager tracking differs. Subsequent agent and recorder-probe
increments are integrated unchanged from `1c983de` and `7a678ec`, respectively,
with newer Manager plans preserved. The native exploration report is integrated
unchanged from `a05e67a`; it adds no production capability. Nothing was deployed.
The internal host archive is integrated unchanged from `70ff636`, preserving
the newer Manager documents and native exploration report.
Volatile capacity/port observations
retain their timestamps and are not a deployment budget or reservation.

## Reviewed foundation interfaces

Use `ef18e59` as the concrete core baseline for dependent assignments. Its
immutable [interface/evidence report](https://github.com/qorqutbot/qorqut-core/blob/ef18e59e2df372963f5fba222156fe6b6e50195f/docs/evidence/foundation-r1.md)
defines the accepted signatures. `Application.invoke` is the shared public path;
`Operation`/`Change` and `Application.execute` assemble fixed internal handlers.
The sixteen current public operations are `config_get`, `config_update`,
`operations_get`, `events_list`, `agents_create`, `agents_get`, `agents_list`,
`agents_update`, `agents_status_set`, `messages_send`, `messages_list`,
`messages_acknowledge`, `deliveries_get`, `conversation_list`, `runtime_get`
and `terminal_get`. Terminal observation is specified in [D38](TERMINAL-SERVER.md).
The ownership read is specified in [INBOX-OWNERSHIP.md](INBOX-OWNERSHIP.md). Chat contracts
are specified in [CHAT-IMPLEMENTATION.md](CHAT-IMPLEMENTATION.md). The agent contracts are specified in
[AGENT-REGISTRY.md](AGENT-REGISTRY.md); all use the same HTTP/MCP/operator
registry. Further domain operations require an explicit assignment and concrete
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

- Independent browser MCP at `c341377`: the D28 logging correction is committed
  and pushed. The fresh reviewer stopped on a provider safety restriction before
  delivering its ACLA report; that review is incomplete and has not been credited.
  The prior review/Manager reproduction found raw malformed request/notification
  payloads in root diagnostics despite clean HTTP errors. Current sanitation and
  wire evidence remain unapproved.
  Synthetic browser effects are not real Chrome/VNC/core/VM acceptance.

## Next implementation round

Manager's next milestone is a headless create → run → capture → message flow.
The immediate evidence assignment is [NATIVE-REHEARSAL.md](NATIVE-REHEARSAL.md),
closing the actual CLI configuration/session/composer observations missing from
the earlier static composition report. It does not authorize VM setup or change
capability flags. Manager then freezes managed launch and native-reader bindings
from those observations; builders do not design those interfaces.
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

[AGENT-REGISTRY.md](AGENT-REGISTRY.md) (`agents-r1`) is accepted: identity/onbash,
authored workspace/persona, presentation/status history and five shared
operations, including host migration 3. Resource/runtime integration stays
explicitly unavailable; the owner has not selected or authorized host preparation
for D02. Subsequent changes to the host schema, registry and assembly still need
one assigned owner and a concrete Manager design.

Runtime exploration is accepted at `39c0f7e`. Its first resulting implementation,
[RECORDER-PROBE.md](RECORDER-PROBE.md), is accepted at `7a678ec`: an optional
fresh process-bound query in the existing recorder loop, without host schema or
registry changes. Native ownership/composer/source integration, authenticated
host collection and managed launch still need explicit designs and acceptance. The probe
alone cannot enable automated terminal input or mark an agent ready.

The host archive assignment is accepted at `70ff636` under
[HOST-ARCHIVE.md](HOST-ARCHIVE.md), including host migration 4 and passive
application assembly. Raw bytes become ACK-eligible only after file and
inventory durability; there is still no authenticated network collector.
The separate `native-capture-p0` exploration is accepted at `a05e67a`:
[its report](evidence/native-capture-p0.md) records native discovery,
descriptor/provenance checks, spool receipts and the absent native checkpoint
path. Each completed implementation or revision receives a fresh independent
review.

The next native implementation is `native-prefix-r1`, following
[NATIVE-PREFIX.md](NATIVE-PREFIX.md) and D32 on this accepted combined base.
It owns only bounded read-only spool receipt pagination, held-file prefix/tail
verification, its focused tests and evidence. It adds no migration or service.
This establishes a comparison primitive after local payload reclamation;
source attestation, ingestion and persistent native-reader checkpoints remain
the subsequent Manager design. It cannot enable automated input or provider
readiness. The host archive and browser lanes retain their separate boundaries.

D33 adds the owner's explicit requirement for automatic dated chat copies under
each agent's `transcripts/`. Core/M07 owns publishing both sides from durable
conversation entries, including restart catch-up and provider switches; M09
preserves files/progress in full exports. A07.10 is required for completed chat
acceptance. This is planned work for conversation publication, not implemented
by native-prefix or the accepted host archive. It does not change the completed
native-prefix candidate's assigned scope or independent review baseline.

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
   New reviewers follow the current owner launch policy above, with
   `reviewLoop=false` and no nested delegation.
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

## D40 parallel continuation — 2026-10-08

The real core/UI recorded Terminal flow has passed Manager acceptance. Native
admission, capture and projection callables are accepted.
[The next concrete plan](NATIVE-COMPOSITION.md) assigns three disjoint lanes:

| Lane | Scope | Acceptance after completion |
| --- | --- | --- |
| Native setup | Managed credential, hooks/MCP/instructions, monitor/server integration | Manager source review and focused setup checks |
| Native collection | Admitted source polling, archive transfer, projection, publication scheduling | Manager review and focused durable pipeline checks |
| Native autoload | Actual pinned CLI discovery of proposed standard config paths | Manager review of factual isolated evidence |

All three use Astra MAX with self-review disabled. Manager integrates the
reviewed collector into normal server composition and then checks actual manual
CLI/user/assistant history through the real UI and dated files. No automatic
CLI launch, paid calls, browser/VM installation or live owner fixtures.

## Native workflow finding 257

The actual Codex UI check exposed slow sequential keyboard frames while entering
a long command. At the saved cutoff, Manager verified exact bytes/receipts and
no Enter, loss or transport error. The [queued-input clarification](NATIVE-WORKFLOW-ACCEPTANCE.md)
lets the existing command drain and keeps the native workflow moving. Keyboard
responsiveness remains a separate open usability issue; the facts-only actor
will trace its accepted code path for Manager's correction.

## D41 parallel Terminal corrections

Manager verified exploration completion 264 against the accepted source and all
59 referenced artifact hashes. Messaging still has no dispatch caller; terminal
spools have a compatible producer binding but no host transfer caller; UI input
currently pays one receipt round trip per key. The static 36 tmux calls per
input frame are not a measured per-call cost profile.

Two independently owned implementation lanes now follow: the core
[Terminal archive connection](TERMINAL-ARCHIVE.md) and UI queued-input batching.
Both receive direct Manager review and focused actual-flow checks. The concurrent
Claude/Codex native workflow verifiers keep core a2a162ce and UI 5a9090c fixed.
Chat dispatch/readiness, browser/VM integration and full system acceptance remain
unfinished. This allocation is not implementation acceptance.

## Qualified Claude workflow accepted

[Manager acceptance](evidence/manual-native-claude-manager-acceptance.md) verifies
actual manual start, positive exit/resume, live-CLI core restart, native Chat and
automatic UTC-dated files at the frozen core/UI pins. The canned provider and
fixture-only post-onboarding qualification remain explicit. This does not accept
Chat dispatch, full source coverage or final shutdown drain. Codex verification
and the two D41 corrections continue independently.

## Both manual native history workflows accepted within their qualifications

[Codex Manager acceptance](evidence/manual-native-codex-manager-acceptance.md)
now joins [Claude acceptance](evidence/manual-native-claude-manager-acceptance.md).
Manual start, exit/resume, live-native core restart and exact primary Chat/daily
history are verified for both frozen versions with local canned providers.
Fresh Claude onboarding and complete source coverage remain outside these gates.

The two D41 implementation lanes continue. A parallel facts-only
[native input assignment](CHAT-DELIVERY-EVIDENCE.md) now checks the actual remaining
composer/paste observations needed before Manager connects Chat delivery. It owns
no production changes and may use only completed fixtures copied privately.
