# Manual creation: combined core and UI acceptance

Manager assignment, 2026-10-08. This combines already reviewed work; it adds no
runtime capability or new public contract. D36 remains the product scope.

## Immutable inputs and ownership

Core main `b39bdda93ed70b475e5a0fb58732d4909b05f137` includes accepted local
ownership `1af5f852ccbc83cc898df11099beb4031ca0b2ca`: schema 6, fifteen operations
and five handlers. Combine it with accepted manual creation
`8d2039f8efc5572726c70aafbf12d50d16558c69`, whose correction parent is
`020879eca62526652737f5d9dde5f2276903556c`. The accepted UI candidate is
`6a4841d5830ae3970b492ebf87528de4f94f787c` in `qorqutbot-ui`.

Manager prepares a dedicated core worktree with those core branches merged.
Integration owns only `tests/agents/test_registry.py`, a mechanical operation
count in `tests/agents/test_agent_flow.py` if the merge needs it, and
`docs/evidence/manual-creation-integration-r1.md`. Preserve every accepted
production byte, lock, migration and UI source file. No other lane is consumed.
Report a production/UI defect to Manager rather than fixing it under this scope.

## Combined-version adjustment

Keep the historical creation fixture pinned to actual source at
`862d989edf0fef0bc6c5100a051e936cb5afbcde`. It must still create real explicit-true
and omitted-start receipts in a subprocess; do not replace the fixture with
fabricated rows or current production. That source creates schema 5, so the
combined binary correctly refuses `Application.open` before explicit migration.

Assert that refusal, then use the existing `Database` and `HostLock` migration
path on the same disposable installation: database `data_root/state.sqlite3`,
lock `data_root/locks/host.lock`, configured busy timeout, acquire/try-finally
close, and `Database.migrate()`. Then open normally. Do not make production open
auto-migrate or weaken version/checksum validation. Preserve all original receipt,
replay/conflict, no-launch, row-preservation, provisioning and reopen assertions.
The runtime read's inventory count is fifteen; preserve the corrected bounded
polling and full immutable receipt comparisons in the shared flow.

Run the owning agents suite and runtime suite on the combined source, including
actual old-code receipt replay with available Git history and no skip. Build a
new wheel with the unchanged lock; run the shared creation/provisioning flow and
one actual-child ownership/reopen/exit flow from an installed package with a
tests-only import path. Verify exact Git/source/wheel/installed bytes and child
entrypoint provenance. Do not duplicate unrelated backend suites.

## Real UI and core check

The owner selected direct browser testing. Use installed Chrome and the UI's
locked Playwright dependency, fresh disposable browser contexts and a loopback
test installation. No external QA service, owner profile or provider call.

Export the exact accepted UI candidate privately, install its unchanged lock and
build it. Serve those compiled assets through the normal installed core `serve`
entrypoint. Use the existing positively owned transport fixture or equivalent
normal configuration; no mocked/intercepted API responses, manually pumped jobs,
gated worker, debug server or synthetic claim of a native session. Request
observation is permitted. Record the exact source and compiled-asset hashes.

Use one compact workflow and its normal server-restart continuation:

- Sign in through the actual console cookie session. Discover configuration and
  fifteen operations; `runtime_get` for onbash is disconnected/input_ready=false.
- Create an ordinary agent using only name, one of twenty icons and the request
  key. Observe the real request: no `cli` or `start` field. Provider/model/start
  controls must be absent, and the manual Terminal explanation visible. Returned
  start_requested is false; creation alone launches no native CLI or terminal.
- Rename/change icon, verify stable identity, and preserve the truthful resource
  dependency hold. Terminal remains unavailable until its real transport exists;
  the UI must not invent a connected run from legacy configuration metadata.
- Send two identical Unicode/multiline messages with distinct keys/entry IDs and
  one onbash message. Preserve exact bodies, drafts across selection and honest
  queued/runtime-unavailable state. Reload the authenticated page.
- Close the browser, then verify the automatically published
  `agents/<id>/transcripts/YYYY-MM-DD.jsonl` for both agents against their durable
  conversation entries, using UTC recording dates and exact content/provenance.
  No UI save/export tool or open viewer may be needed for publication.
- Restart only the owned normal server. In a new browser context, sign in and
  verify the same identity, metadata, messages and unchanged transcript bytes.
  Check logout, desktop/mobile navigation/inspector usability, no horizontal
  clipping, and no uncaught page or CSP error. Inspect the saved screenshots.

The prior Manager harness at `manager-ui-core-r1/` in the recorded build run is
a reference, not current evidence. Adapt it only in a private owned directory:
replace its exact source/asset pins and old checked-start assertion with this
identity-only contract. It must not execute against moving main checkouts.
Keep runnable private commands/harnesses, checksums and screenshots for review;
commit concise evidence, never tokens, profiles, native stores or test transcripts.

## Completion and limits

Commit the narrow test adjustment and evidence on the assigned integration branch
and push it, then stop edits for fresh independent Astra MAX review. Report exact
core/UI SHAs, source/installed/UI commands and outcomes, byte/import provenance,
known failures, screenshots and cleanup. Manager decides acceptance and lands
core/UI after the combined check. No deployment is part of this assignment.

This proves combined creation, durable messaging/publication and the existing
console shell, not user-launched native CLI hookup, assistant capture, safe Chat
delivery, Terminal transport, browser/VNC or VM readiness. Those remain the next
connected-runtime and environment work; no full G0–G6 gate advances here.
