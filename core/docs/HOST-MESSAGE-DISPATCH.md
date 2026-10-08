# D46 — Connect stored messages to the manually started onbash session

Manager implementation decision, 2026-10-09. Base is accepted core
`c13eb8b2dbf023a69e84cde2eda0368058485e6f`. One builder owns the complete host
composition, durable attempt ledger and normal job connection. Reuse D43's
51-byte notification and D44's recorded-terminal input gate/receipts. No CLI
launch, provider selection, new service, public input endpoint or VM work.

## Private runtime boundary

Implement `RuntimeInput(application)` in `runtime/input_dispatch.py`, with a
small frozen `InputTarget` model in that module or `runtime/input_models.py`:
installation_id, conversation_id, terminal_id, native_session_id (UUIDs),
provider, cli_version, idle_event_seq (positive counter), control_epoch
(nonnegative counter), and the existing runner RunBinding as `binding`.
The complete canonical target is saved before dispatch. It contains no secrets
or caller-selected executable/database path. Run ID is the incumbent owner ID.

Methods are `prepare(agent_id) -> InputTarget`,
`submit(target, attempt_id, message_id, *, still_current) -> AttemptReceipt`,
and `inspect(target, attempt_id) -> AttemptReceipt | None`. The callback
still_current is mandatory, pure, and must return exactly True. It checks the
host claim and saved attempt before every input readiness check. All external
work runs outside SQL write transactions under existing installation use/effect
guards. Constructors do not start any service, shell or CLI.

prepare only observes: no input, owner replacement, registration or attempt.
Support only configured reserved onbash with an initialized owned workspace.
Inspect the existing recorded shell, current native owner and exact managed
hook configuration. Require the current installation/recovery epoch,
environment generation, terminal, native-session/provider/version and process
fingerprints to agree. Recheck the live native executable against its configured
program and its bounded parent chain to the exact recorded Bash; reuse/factor
the existing native hook ancestry primitives, with no discovery by cwd/name.
Unknown/closed/stale bindings cannot authorize input. A fresh process check,
not cached runtime_get.status, provides current liveness.

Use the latest accepted native.hook event for this exact owner/run generation,
terminal, provider/version, native session, process, recovery/environment binding.
Accepted outcomes are connected/reconnected/observed. Require actor attribution
to the registered agent. Its event must be SessionStart, Stop or Interrupt with
activity idle. Working/waiting-user/SessionEnd/absent/mismatched observations
refuse input. A hook's age alone is not a lease: a long-idle still-live CLI is
valid after fresh process/capture/composer checks. An unrelated rejected second
session must not replace the incumbent's accepted observation.

Capture requires a fresh verified active recorder probe for the exact recorded
terminal, healthy saved capture with no terminal gap, and enabled native
collection reporting collecting/active with a non-stale observation at or after
this owner's connected_at. Also require an admitted primary native source link
in host archive_run_sources for this exact run/native session/installation and
epoch, with no conflict. This intentionally waits for initial source discovery;
for Codex the owner may need the already-observed first manual Terminal prompt.
Do not read the collector's thread-owned NativeCapture object or pump it from
this worker. No zero-backlog or complete-source-coverage claim follows.

The terminal must be unclaimed, and a fresh Tmux snapshot must yield empty from
D43 classify_input. Do not use ComposerVerdict.permits_paste (the pure adapter
intentionally cannot grant whole-runtime authority). Refuse unknown versions,
geometry, drafts, modal/busy states or missing/true input_off/synchronized flags.

submit reconstructs only the deterministic owned runner database from the saved
target. It rechecks target, managed configuration, latest exact idle event and
capture under the existing use/effects/workspace guards. Register the RunBinding
with the saved terminal_id. Construct TerminalRunner with the configured tmux
executable, paste_settle_timeout=0.5, and explicit fresh callbacks for ownership,
idle hook, capture and still_current. Callback observations run under D44's
terminal gate; use the existing ShellManager internal inspection seam there,
never recursively call a public method that acquires that same flock. Small
shared observation extraction is allowed; do not copy the paste state machine.

The classifier delegates to D43. The pure verifier requires the supplied digest
to equal notification_text(message_id)'s ASCII hash and matches_input to match
that exact notification in the fresh snapshot. Only that notification reaches
TerminalRunner.attempt. No body, clearing, extra keys, fallback Enter or second
paste. Human control remains ShellManager-only. No separate machine surface or
hold is implemented yet; explicitly supply no machine hold for this local-only
boundary and document that a future machine takeover must join this gate.

An internal InputUnavailable(CoreError) may carry a fixed reason for prepare's
known-no-input refusal: runtime_unavailable, unsupported_target,
session_unavailable, hook_unavailable, native_busy, native_waiting_user,
human_control, capture_unavailable, composer_busy, composer_blocked,
composer_unknown or target_changed. No provider/OS error text reaches the API.
Do not make error prose the protocol. Failures after submit begins are reconciled
from receipts; a convenient exception alone is never proof of no input.

inspect performs no paste/registration/launch or live-session adoption. It reads
and reconciles the original existing runner store under D44's gate, validating
saved mapping/binding and the original installation epoch. Refuse a missing or
replaced store; never create a fresh empty database and call it receipt absence.
A closed original native process must not prevent reading its old receipt.
Preserve uncertainty if original evidence cannot be obtained.

## Host attempt storage and public observation

This builder alone owns migration 9 and Application/Jobs assembly. Preserve
migrations 1–8 and all historical operation JSON. Add delivery_attempts with
immutable identity: attempt_id, message_id, per-message attempt_seq,
previous_attempt_id (nullable), job_id, installation_id/recovery_epoch,
canonical target_json and target_sha256, payload_sha256, payload_bytes=51,
created_at. Mutable observation is state, reason, runner_receipt_json|null and
updated_at. States: prepared, dispatching, deferred, submitted, uncertain,
failed. Add appropriate FKs, unique message/sequence, state/range checks,
identity-update and delete/replacement refusal. Persist each material transition
as an attributed event; repeat identical observations must not append events.
Submitted/failed results cannot regress. Uncertainty may resolve only from a
matching original runner receipt, never from elapsed time, hook text or handling.

Expose typed DeliveryAttempt items through existing deliveries_get, ordered by
attempt_seq: attempt_id, message_id, attempt_seq, previous_attempt_id,
run_id/run_generation, terminal_id, provider/cli_version, idle_event_seq,
control_epoch, payload_sha256/payload_bytes, state/reason, created_at/updated_at.
Keep process fingerprints, filesystem paths and complete runner receipt in the
host's saved target/receipt instead of expanding routine API output. No new
public operation, job kind, package or dependency: 17 operations, 5 handlers,
schema 9. Existing DeliveryView and immutable messages/handling stay unchanged.

## Dispatch and recovery sequence

Compose RuntimeInput and the dispatcher in Application. Reuse message_delivery's
execute/inspect handlers and the normal single background worker. Do not use a
new dispatch thread. Under the existing effects gate:

1. Validate exact current job/claim, actor, recipient and recovery epoch. Prior
   submitted/failed/cancelled deliveries finish without input. Recovery-held
   work stays held, including unfinished restored attempts; a new CLI does not
   release it. Already-uncertain work enters inspection, never new submission.
2. Service pending/deferred messages in message_seq order per recipient. Exclude
   historical/recovery-held jobs from current-epoch FIFO. A later independent
   message may proceed after an older uncertain delivery only if the new live
   idle event and composer checks permit it; never retry the older message.
3. Inspect any existing unresolved attempt before preparing another. A host
   prepared row (no dispatching intent) can safely become deferred. A dispatching
   or uncertain row needs the original receipt. No receipt, mismatch, or lost
   original store leaves it uncertain, even if a current session looks idle.
4. With no possibly performed attempt, prepare the current target. Disabled or
   unsupported runtime retains the existing pending/runtime_unavailable result
   and waits dependency_unavailable for 60 seconds. Other known preflight
   refusals defer with their fixed reason and retry_delay of 2 seconds, without
   creating attempts or flooding unchanged delivery events.
5. Before recording an attempt, require target.idle_event_seq to exceed every
   dispatching/submitted/uncertain attempt's idle_event_seq for that same run,
   across all its messages. This is the one-notification-per-idle-hook barrier.
   It prevents another queued message from racing ahead of UserPromptSubmit.
   A trustworthy deferred/no-input receipt does not consume the idle event.
   If the barrier is not met, defer with awaiting_native_turn, retry after 2 s.
6. Commit the new prepared attempt with complete target/hash and attributed
   attempt event. Commit dispatching intent and delivery state before calling
   submit. Recheck the claim/epoch/attempt in every still_current callback; do
   not hold host write transactions during runner operations. Use fault seams
   around host intent, runner return, and host receipt commit.
7. Validate the returned receipt's exact attempt ID, RunBinding/digest, payload
   hash/length and control epoch before saving it. submitted means one verified
   paste plus normal Enter, not model processing/handling. deferred means known
   no input and permits a later newly linked attempt after fresh preparation.
   uncertain never permits automatic new input. A lost response/exception is
   inspected using the same attempt ID; preserve uncertain if unresolved.
8. Commit receipt, attempt state, delivery state and attributed events together,
   then return the job resolution. Crash after host result but before job finish
   recovers from that saved result without another input. Expired leases alone
   never authorize new effects. Runner saved-attempt recovery remains authoritative.

Normal job claim may additionally select due current-epoch message_delivery
jobs waiting dependency_unavailable; do not wake other dependency jobs or any
recovery_review job. This allows pre-D46 offline messages to begin after startup.
Add message_delivery to the bounded original-epoch uncertain receipt inspection
in reconcile_pending. Inspection may move a proven no-input outcome to waiting
retry_delay but must never itself paste or manufacture a new attempt. Keep
unresolved input uncertain and rate-limited. No explicit uncertainty-override
or retry-after-uncertainty operation is added in this increment.

## Scope and checks

Own runtime input composition plus the minimal ancestry/terminal observation
seams, messaging attempts/dispatch/models, Application/Jobs wiring, migration 9,
affected inventory expectations, focused tests and evidence. Do not change
UI, native parsing/projection, raw bytes, provider setup, browser or VM behavior.
Runtime_get.input_ready remains the conservative literal false: no reusable
permission is advertised; readiness is proved only for a specific dispatch.
Native notification-echo association remains a following projection increment.

Meaningful checks: exact ready target and notification delegation; each missing
owner/hook/capture/human/composer proof refuses without input; callbacks stay
under the shared gate and outside SQL; target/epoch or final proof loss; receipt
inspection after native exit; only one notification per idle event; queued
messages with distinct bodies/control characters remain exact; one new idle
hook admits the next message; saved submitted/uncertain receipt never replays;
crash before dispatch, after possible input, after runner result, after host
commit; missing/mismatched receipt stays uncertain; deferred attempts can retry
under new IDs; old offline jobs wake; restore holds and old receipts survive
schema-8 upgrade; shared HTTP/MCP deliveries_get returns the same typed attempts.
Reuse existing owning fixtures. Keep synthetic readiness/provider evidence
explicit; do not claim real native acceptance from it. One real isolated tmux
serialization/receipt integration is sufficient; no duplicated full runner
suite or paid/native-model workflow here. Manager will merge and run the actual
pinned Claude/Codex connected Chat flows separately.
