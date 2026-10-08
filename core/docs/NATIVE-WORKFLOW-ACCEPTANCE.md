# Manual native workflow acceptance

Manager acceptance assignment under D40, 2026-10-08. The setup and collection
services are separately reviewed and composed in the normal server. This gate
checks actual manually started native CLIs against that composition and the
accepted UI. It does not enable Chat dispatch or change product contracts.

## Parallel assignments

Two independent verification actors exercise the same flow below: one Claude
2.1.287, one Codex 0.160.0. Each uses its own fresh private installation, network
namespace, ports, HOME, workspace, terminal, Chrome profile and evidence. No
shared fixture mutation. Each owns only its provider's evidence document and
private harness; no product, plan, dependency or existing test changes. Manager
reviews the completed facts directly. Ask Manager about an actual failure or
missing decision; do not patch the product, weaken a check or silently substitute
a different code/configuration pair.

A third exploration actor reads accepted code/evidence for the remaining Chat
input and terminal raw archive connections. It reports exact existing callables,
bindings, known failures and missing inputs. It does not design a solution or
modify production. Manager retains all subsequent design and scope decisions.

## Fixed inputs and containment

The handoff pins the exact composed core commit and UI
`5a9090c7566edc7df1b3afa8facb7134dd420eca`. Export those commits, install core
in a private environment and build the UI with the existing locks. Verify the
actual imported package and served distribution against those inputs. Reuse
accepted Terminal integration helpers and native autoload fixtures. No new
framework, broad suite rerun or package upgrade.

Run the normal `qorqut-core serve` entrypoint, real workers and installed hook
client. Configure explicit NativeProgram executable/version binding for the
assigned provider and the accepted local Terminal settings. Creation/setup must
produce recorded Bash, managed configuration and credentials without starting
a native CLI. No fabricated owner, SQL mutation, manual job pumping, debug
server, fake hook sink or interception of core HTTP/WebSocket results. Read-only
queries and request/frame observation are permitted for evidence.

Use a disposable loopback-only network namespace with no external route, fresh
synthetic provider keys and the accepted local canned provider. Core, provider,
CLI and Chrome must share that test namespace. Do not read personal CLI homes,
credentials, live agent sessions, browser profiles or production Qorqut data.
Preserve owned fingerprints/pidfds and stop only positively owned processes.
The owner chose direct browser testing: use installed Chrome and the UI's locked
Playwright package with a fresh profile, not the external QA service.

Claude's actual fresh onboarding failure is already preserved in
[autoload evidence](evidence/manual-native-autoload-r1.md). This flow may use
only the same explicitly authorized fixture-only post-onboarding seed
`{"hasCompletedOnboarding": true}` in the fresh managed HOME .claude/.claude.json.
Label it throughout. Product setup must never write that seed. Do not seed
workspace/MCP/key trust, native hook hashes, authentication or permissions.
Answer actual native trust dialogs explicitly in the disposable fixture.
Codex uses the accepted manually supplied --no-daemon flag and local fake
provider overrides; standard managed configuration still autoloads. No native
UUID is supplied on first start.

## Bounded connected flow

1. Sign in through the actual compiled UI cookie-session flow. Wait for normal
   provisioning and Terminal/setup observations. Confirm no native owner/process
   exists, setup is installed and collection is waiting_session. Open Terminal,
   explicitly take control and manually enter the assigned CLI command using
   the implemented keyboard/input path. Synthetic provider environment values
   may be entered in that fresh shell; preserve generated hooks/MCP settings.
2. Answer actual native trust/consent dialogs. Verify native MCP tool discovery
   from the real core configuration (for example core tool names in the canned
   provider's observed tool list). Separately issue one harmless status mutation
   through the official MCP SDK using the generated managed credential and
   confirm the real agent actor on the persisted status/operation. Label this as
   SDK attribution, not a semantic tool call by the native model. No new model
   tool-call emulator is needed in this gate.
3. Submit one short distinct text prompt in Terminal and receive a canned reply.
   Verify production SessionStart admission: one exact native process/UUID and
   terminal binding, runtime owner, native source ownership and host archive
   receipts. Codex's accepted SessionStart occurs only on first input; do not
   demand pre-input ownership. Merely installed config is not an inbox receipt.
4. Switch to Chat. Verify exact native user/assistant text is rendered by the
   real UI and returned by conversation_list with native provenance, and the
   same entries appear automatically in agents/<id>/transcripts/YYYY-MM-DD.jsonl
   by durable UTC date. Record native raw archived bytes/offsets, entry IDs and
   daily file hashes. Preserve declared parser/attachment gaps; a partial Chat
   projection must not be described as a complete raw archive. Save and inspect
   a desktop Chat screenshot and a narrow screenshot for this native content.
5. Close only the native CLI using its normal terminal exit. Observe positive
   process exit and ownership release; Bash persists. Manually resume the saved
   native UUID using the provider's supported resume command and enter a second
   distinct prompt. Confirm a new admitted owner/run with the same conversation,
   old entries unchanged, no duplicate native entry IDs, and automatic new
   publication. An unsupported resume result is a reported failure, not a
   reason to fabricate session identity or edit source data.
6. With that CLI idle and still alive, stop only the owned normal console and
   restart it with the identical configuration. Confirm the recorded shell and
   native process survived, credentials/config and inbox ownership reconnect
   without replacement, old Chat/daily entries remain, and a third short Terminal
   prompt/reply is collected and published by normal workers. No Chat dispatch
   is expected yet: input_ready stays false and stored outgoing Chat messages
   remain queued. Do not reinterpret terminal keyboard input as Chat delivery.
7. Log out/close viewers and clean up only the owned test processes, namespace
   and sockets. Record remaining process/port checks. Keep private raw evidence
   and concise committed results, with actual fixture limitations.

These three short canned turns per provider are one connected flow, not a new
compatibility matrix. Existing ownership conflict and recorder-outage unit/real
checks are not repeated. This gate does not accept real provider login/inference,
Chat-to-CLI dispatch, terminal raw host transfer, browser/VM integration, backup/
restore, deployment or complete G0–G6.

## Clarification 257: queued keyboard input and timing

The Codex fixture's 536-byte launcher was entered with per-character
Playwright keyboard.type. Its copied helper's 45-second settlement assertion
expired, while the real UI continued draining its queue. Manager independently
checked the saved cutoff: 347 bytes matched the exact requested prefix, 346
submitted receipts matched their frames, and no Enter or newline was sent.
The observed per-character receipt delay is a real responsiveness concern and
remains open; it is not hidden by this harness correction.

Preserve the in-flight input. Observe its remaining queue for up to another
180 seconds without replay or clearing. Only after exact complete input bytes,
matching receipts, unchanged viewer/control and no errors are verified may the
fixture send one separate Enter and continue. If those conditions do not hold,
report the retained state and ask Manager; do not resend an uncertain command.
For subsequent long text, Playwright keyboard.insertText through the actual
Terminal input handler, followed by a separately acknowledged Enter, is allowed.
The native flow gate measures correct input effects and history continuity;
passing it does not accept per-character responsiveness. Product code, protocol,
input ownership and all history assertions stay unchanged.

The parallel facts-only connection investigation also traces this accepted UI's
serial frame queue and the core's per-frame verification/effect path, reporting
evidence for latency contributors. It does not implement batching, relax
verification or redesign the protocol. Manager owns that correction separately.
