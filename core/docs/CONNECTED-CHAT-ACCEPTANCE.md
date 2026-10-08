# Connected Chat dispatch acceptance

Manager verification assignment, 2026-10-09. Two independent Astra MAX lanes
exercise the merged D46/D48 implementation: actual Claude 2.1.287 and actual
Codex 0.160.0, each with its own disposable installation and canned loopback
provider. The handoff pins the exact core/UI commits. This is verification only;
no product changes, schema, dependency upgrades or redesign. Report defects to
Manager with the original observations. Keep tests/harnesses private and preserve
failed attempts. Only the provider-specific evidence report is tracked.

## Common setup

Build/install the pinned core wheel outside the source checkout. Build the pinned
UI using its exact lockfile (or verify and reuse an identical compiled artifact).
Reuse copied, completed manual-native/input-observation helpers and pinned native
executables; never operate on their original fixtures. Record package/build and
native executable identities. Run normal `qorqut-core serve` with its real
workers/monitor, actual compiled UI and official installed hooks/MCP configuration.
No direct job pumping, fabricated owner/idle event, SQL mutation, manual native
capture/projection call or intercepted core HTTP/WebSocket/MCP response.

Use fresh user/network/PID/mount namespaces, private HOME/tmp/data/native stores,
Chrome profile and generated fixture credentials; bring up loopback only. Choose
ports independently of the other lane. No real provider credentials or inference,
external routes, owner sessions, production data, browser takeover or VM setup.
Keep credentials out of logs, URLs, screenshots, traces and Git. Use only explicit
fixture environment variables and fingerprinted cleanup. Do not inspect another
actor's unfinished worktree or suppressed browser-review material.

The recorded shell is provisioned by core. Manually start the CLI through the
actual UI Terminal, with explicit takeover and native keyboard input. Creation
must not start it. Native provider/model/effort settings stay in Terminal. Claude
may retain the already authorized fixture-only `hasCompletedOnboarding: true`
qualification; do not write trust hashes or future-MCP trust. Answer displayed
workspace/MCP/key dialogs deliberately. Codex keeps its accepted --no-daemon and
loopback provider configuration, with actual hook consent. Use the exact accepted
manual launch procedure; if it no longer works, preserve the failure and ask.
If initial Codex has no primary source/idle hook, submit one short manual seed
prompt, as already qualified by D46; label that prerequisite.

## Fixed connected flow

1. Sign in through the real session form. Show initially owner-free recorded Bash,
   then the manually started CLI's admitted owner, managed hook setup, primary
   native capture and terminal recorder. Native records and public API/UI must
   agree on the session. Read-only diagnostics may inspect private stores; they
   must not create state. Do not equate the public input_ready=false with failure:
   D46 proves readiness for each dispatch and does not publish a reusable grant.
2. Retain explicit human Terminal control while entering an owner Chat message
   through the UI composer. Use a unique marker, Unicode, LF and TAB in its body.
   The message must be durable and dated automatically, with no terminal attempt
   or native user record for that message while human control is held. Observe
   its deferred/pending reason through deliveries_get. Release control through
   the real UI, leaving the native composer empty.
3. Let the ordinary background worker deliver the message. Exactly the canonical
   51-byte `[Qorqut inbox <message UUID>]` notification may reach native input;
   the complete body stays in core. Verify one submitted attempt and matching
   runner receipt, exact native echoed notification bytes/identity/raw span, and
   the persisted target's owner/run/terminal/session. No manual input, direct
   RuntimeInput/TerminalRunner call or synthetic receipt may stand in for this.
4. Extend only the copied canned provider's response scripting, using that
   provider's actual tool schemas, so the real CLI performs its registered core
   MCP messages_list call for the notification UUID. Save sanitized evidence of
   the actual MCP response containing the exact body, including LF/TAB/Unicode.
   Then the canned provider returns a short fixed assistant marker and asks the
   native CLI to call messages_acknowledge for that UUID through MCP. Preserve
   actual order/tool results: if the provider needs tool-call/result phases before
   final text, follow its protocol. This is deterministic protocol verification,
   not real model reasoning. An out-of-band SDK call cannot satisfy this step.
5. During a bounded five-second canned response delay, enqueue a second distinct
   message through Chat. Verify no second notification is pasted while the first
   turn is working. After the next accepted idle hook, ordinary dispatch must
   submit it once through the same read/ack/reply flow. Distinguish submitted
   delivery, explicit handling and native assistant text. Do not infer handling
   from paste, hook or assistant prose. Bodies must remain exactly unchanged.
6. Compare both stable core messages and actual native assistant entries with
   HTTP conversation_list, the visible Chat and automatic UTC-dated JSONL. Match
   recorded content, identity, source provenance and raw span hashes. The current
   projection may also expose native notification echoes; record their exact
   association facts separately. Echo deduplication is a following increment,
   not permission to edit native records or suppress an acceptance failure.
7. While the CLI stays live and idle, stop/restart this temporary core normally.
   Reauthenticate through the real form. Verify saved message/attempt/handling
   identities, entries and dated content remain unchanged and neither submitted
   message is pasted again. Send one fresh short Chat message and verify the
   reconnected native session handles it normally through the same actual MCP
   path. This checks D48 reopen plus D46 receipt/input composition, not merely
   record persistence. If native startup consent/configuration changes, report it
   and do not seed a success state.
8. Capture and inspect a desktop Chat, Terminal and 390px Chat screenshot. Observe
   wrapping, scroll, focus, network/domain/page errors and any stale status. Exit
   the CLI normally through explicit human Terminal control; verify its owner
   closes while Bash persists. Close only owned Chrome/server/provider/namespace
   processes and prove their exit. Preserve all private original evidence.

Do not rerun the existing broad component suites or earlier three-turn manual
resume matrix. No forced-fault matrix, onboarding redesign, second-session race,
provider switch or raw completeness claim is needed here. A failure after possible
input remains uncertain: inspect original receipts and ask Manager; never force
retry, erase state, auto-clear a human draft or fabricate a new idle event.

## Report

Only `docs/evidence/connected-chat-<provider>-r1.md` is tracked. Record exact pins,
commands/results, clean/pushed report commit, original harness/log/screenshot and
native evidence paths, real MCP attribution and response bodies (synthetic data
only), message/attempt/entry IDs, source hashes and cleanup. Preserve all failed
commands and distinguish harness errors from product defects. Report the
fixture-only Claude onboarding and canned-provider limitations; acceptance does
not establish real login/inference, complete child/attachment/shutdown history,
browser takeover, ordinary VM execution, backup/restore or deployment. Manager
owns review, interpretation, subsequent design and merging.
