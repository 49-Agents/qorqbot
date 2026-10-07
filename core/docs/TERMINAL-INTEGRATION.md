# Compiled UI and recorded Terminal acceptance

Manager assignment, 2026-10-08. This verifies the accepted D38 implementations
together. It changes no runtime contract and is not native CLI acceptance.

## Immutable inputs and ownership

Core candidate `003c5badc3aa7e6d3f958b461e8e7a98dda49758` is accepted with the
four corrections documented in [D38](TERMINAL-SERVER.md). The handoff pins the
exact merged core commit containing identical production/test bytes and these
Manager documents. UI main is
`e380b4c053a2bbc602e775cb2e167f6b3e30c32c`, containing accepted UI candidate
`2faabdfc3558a5b50396032274d2caf622c6e9a8` unchanged.

The integration actor owns only `docs/evidence/terminal-integration-r1.md` in
its assigned core worktree. Put executable harnesses, source exports, package
environments, captures, screenshots and complete evidence in its private run
directory. No production, existing test, dependency or UI changes. If actual
behavior fails this contract, provide the reproducible failure and ask Manager;
do not patch the product, weaken assertions or change the plan. A separate fresh
Astra MAX reviewer examines the completed evidence and independently checks
the material integrated behavior before Manager accepts it.

## Real system setup

Build/install the exact core export from its unchanged lock in a private venv.
Export the exact UI commit, use its unchanged npm lock and build compiled assets.
Verify installed package and served UI bytes against those exports. Use the
normal generated `qorqut-core serve` entrypoint with the real application worker,
observation loop, authentication and static hosting. Record process argv/import
provenance so a source-import/debug server cannot stand in for this installation.

Use an isolated loopback port, disposable agent data/HOME and a short private
runtime/socket root. Configure the accepted local Terminal settings explicitly.
Reuse the positively owned test fixture setup/cleanup if helpful, with no manual
job pumping, fabricated ready state, source monkeypatch or manually injected
WebSocket result. Request/frame observation and read-only evidence queries are
allowed; no API or WebSocket interception/mocking. Keep actual recorder/tmux/
Bash identities so cleanup never targets an unrelated process.

The owner chose direct UI testing. Use installed Chrome through the UI's locked
Playwright package, fresh browser profiles/contexts and synthetic task data.
No external QA service, owner browser/profile, provider login/call, native CLI,
VM/browser-MCP work, global service/configuration change or deployment.

## One connected flow and restart continuation

1. Sign in through the actual cookie-session UI. Observe discovery of sixteen
   operations and the reserved onbash's real Terminal becoming available through
   normal folder provisioning and observation. Verify its managed workspace,
   home and recorded Bash binding. Initial setup must have started only the
   accepted Bash/recorder/tmux path, with no native CLI. Ordinary agents report
   `unsupported_target` and acquire no host terminal.
2. Open Terminal through the UI. Verify a real authenticated WebSocket, visible
   output, initial view-only state and explicit Take control. The acknowledged
   control enables input. Use real Playwright keyboard input, control keys and
   Unicode insertion to execute a few harmless commands entirely inside the
   fixture workspace. Compare exact resulting bytes with observed frames and
   recorded output; seeing an input receipt alone is insufficient.
3. Exercise one multiline/bracketed paste through the implemented UI path and
   verify its actual shell effect. A DOM-dispatched clipboard event is allowed
   only with an explicit synthetic-input label; it must still traverse the real
   UI, WebSocket, core and shell. Do not use/read the owner's clipboard. Resize
   the viewport/inspector while controlling and verify actual pane dimensions
   within the protocol bounds. Output-only terminal protocol replies must not
   become pane input; retain a compact observed device-query check.
4. Open a second real browser viewer and explicitly take control. The previous
   viewer must stop admitting input when it observes the new owner. Return to
   Chat and another agent, then back: drafts and stored messages remain, the
   shell identity persists and a newly opened viewer starts view-only. Releasing
   or taking a hold is explicit; a closed viewer alone never releases it.
5. Arrange a short, positively owned Bash child that emits a unique marker after
   the UI and normal console are closed. Record terminal/tmux/Bash/recorder/child
   fingerprints and durable control first. Stop only that console, verify the
   recorded shell and child survive, and restart the same normal console with
   the identical configuration. Sign in again; confirm the same terminal and
   durable hold, output captured during the outage, view-only reconnect and no
   replayed input or automatic takeover. Preserve truthful disconnected native
   runtime/queued Chat states throughout.
6. In the restarted UI, inspect desktop and narrow mobile Chat/Terminal layouts,
   navigation and inspector. Verify no clipping, uncaught application errors or
   unexpected CSP failures; inspect saved screenshots directly. Expected socket
   closure during explicit shutdown is recorded separately from a defect. Check
   logout disconnects the viewer while preserving shell/hold. End with explicit
   owned fixture cleanup and confirm process/path removal.

This is one bounded integration workflow, not a rerun of every accepted unit
suite or a new protocol test matrix. Existing source/browser checks already
cover queue limits, malformed framing and synthetic effect/cancellation cases.
Add targeted private diagnostics only for concrete integration concerns.

## Completion and limits

Commit/push concise evidence on the assigned branch: exact core/UI commits,
build/start/browser commands, assertions and observed results, source/wheel/
served-asset provenance, screenshot locations, real versus synthetic input,
cleanup and any limitations. Keep private tokens, profiles, runtime stores,
transcripts and fixture paths out of Git. Report a failure accurately; do not
declare acceptance by documenting a workaround.

The proof is a usable recorded onbash Terminal through the real UI. It does not
connect manually started native providers, claim inbox ownership through a
startup hook, deliver Chat to a CLI, capture native assistant replies, prove host
archive transfer, or complete browser/VM/reboot/backup/restore gates. Those
remain required subsequent work under D36 and the global acceptance plan.
