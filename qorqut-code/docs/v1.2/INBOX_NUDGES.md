# Backend inbox nudges (P5)

An agent with pending, authorized inbox events and a running native session gets one short advisory typed into that exact
session by the application backend. Nothing runs inside the agent's session: no poller, no lease, no agent-side watcher.

The engine is `qorqut_operations.inbox_nudges` (Operations `docs/INBOX_NUDGES.md`): the durable need, the immutable attempts and
the event coverage. This host side (`app/bazarlyk/native_chat/nudges.py`) supplies the authorization port, the verified native
binding, the one input arbiter, and the lifetime. Chat inputs, replies and the native work/user counters are not inputs to the
decision.

A nudge acknowledges nothing and changes no Task, Action, Signoff, routine, message or subscription. `submitted` is the native
transport's report that the notice was typed; it is never evidence that the agent read its inbox. Only an exact
`inbox_acknowledge` is.

## Producers, recipients, deduplication

* **Producers** are unchanged. Every writer of `inbox_events`/`inbox_deliveries` (Story, Task, document, Signoff and attention,
  message, owner conversation, email, focus card, incident, operational assignment and content session
  pointer events) keeps its own event shape and its own recipient rule; none of them knows about nudges. A source that is
  later added needs only `Inbox.event_visible` to answer for it.
* **Recipients** are the agents that have a not-yet-acknowledged delivery row, in business and Global scope alike, each judged
  with that agent's own invocation context and `inbox_list` policy (`HostVisibility`). One agent never sees another's items.
* **Authorization** is checked per event at every use: when a notice is reserved, and again immediately before the first input
  (`InboxNudges.validate`, run inside the arbiter's checkpoint). An event that is not provably visible discloses nothing. The
  retained private-message and Signoff restrictions are the inbox's own `event_visible` rules; the nudge text names no event,
  source, count or summary, so it cannot disclose an unavailable item.
* **Bounded scan**: the engine reads at most a bounded window of an agent's pending deliveries per pass and remembers where it
  stopped (`inbox_nudge_needs.scan_after`, saved with the prepared-state transaction, surviving a restart). A partial or tail window
  can neither prove the inbox empty nor clear an outstanding need; the scan resumes on the next tick, so an authorized event beyond
  the first window is eventually delivered and a later visibility change on an earlier entry is revisited when the scan wraps. It
  is a continuation cursor only: not coverage, an acknowledgement or a permission cache.
  Fresh covered-event authorization is still checked before any input. Details: Operations `docs/INBOX_NUDGES.md`
  ("Bounded scan continuation").
* **Deduplication**: one durable need per agent (`inbox_nudge_needs`), not per event or per source. Events that are visible and
  pending at the same moment share one attempt (coalescing); a source whose item is missing contributes nothing.

## What is typed

`NUDGE_TEXT` (engine `contracts.py`): it keeps priority on any direct user request, points at the existing MCP inbox tools, asks
to acknowledge only the exact events handled, and says it is advisory and needs no reply. The arbiter wraps it in the system
marker `<qorqut-system-input id=… type="inbox_nudge">`. The agent hook recognizes the leading marker
(`native_chat.projection.leading_marker`), not the text, so a nudge is never owner input for Dev review cadence; owner text that
merely contains the words, or quotes a marker mid-text, is ordinary owner input.

## Delivery

One pass of `NudgeBackend.run_once` (thread `inbox-nudge-backend`, 3 s tick, woken on demand):

1. optional `reconcile` hook (see Lifetime), then restart reconciliation of open reservations;
2. for each agent with a due need: skip if a direct Chat input is waiting (user work first);
3. observe the exact live session (`probe`, `registry.reconcile`);
4. `engine.prepare(agent, binding, Observation(working=…))` returns a `Reservation` (exact binding, covered event ids, kind) or a
   `Waiting` reason; the observation carries no counter and is diagnostic only;
5. `arbiter.admit_nudge(agent, source_key, text, expected_binding=<reserved session>, on_checkpoint=<fresh validate>)`;
6. `engine.settle` with exactly what the transport reported, or `cancel` when the session or authorization changed first.

Both adapters submit while the agent is busy (Claude terminal paste, Codex `codex queue`); the arbiter types only into a
verified empty composer. An occupied or unknown composer, a modal, an unreadable screen or no verified session defers and
touches nothing. Processing is never inferred from submission. A session that changed between reservation and input aborts the
old attempt (`stale`); the exact new binding gets its own attempt, never a replay of an old receipt.

### Event coverage (inbox reliability repair, 2026-10-14; replaces the frozen P5 rearm)

An event warrants a notice when it is delivered to the agent, unacknowledged, currently authorized and not already announced (or
possibly announced) to the same native conversation (agent, provider, conversation id). The engine keeps that coverage in the
existing attempts (`event_ids_json`, `binding_json`, states): submitted and uncertain attempts cover their own event ids for that
conversation, across all earlier cycles; deferred, failed, unavailable and stale attempts cover nothing.

* A new eligible event gets its own notice whatever older event is still pending; an unchanged unacknowledged backlog never gets
  another, after Chat inputs, replies, elapsed time or an agent saying it will look later.
* Eligible events are combined into one bounded notice per agent. 60 s between effective notices is kept: events arriving
  meanwhile stay pending and go out together when it expires. Deferred/failed/unavailable attempts retry on bounded backoff.
* The same conversation reattached by a new process, or seen again after a backend restart, is not told again; a genuinely new
  conversation gets a notice for the outstanding eligible events.
* An uncertain notice is never replayed and suppresses only its own events; a newer event is still announced.

The host passes the engine no work or user counter: `Observation(working=…)` is diagnostic only (busy evidence recorded with the
attempt). The native archive projection keeps `native_agent_counters.work_sequence`/`user_sequence` and turn attribution for Chat
and other readers; they no longer gate or rearm a notice. The backend's per-agent memo that skips an unchanged agent between passes
is keyed on the live binding and the pending set only.

**Turn origin and turn work are two facts.** `native_replies.origin` (`user`, `nudge`, `system`, `unknown`) is who started the
turn a reply completed: the first input of the turn names it, and a later steered input never changes it. `native_replies.user_work`
is whether any genuine, non-notice native user input took part in the turn, wherever it came in the order: any such input sets it,
and a notice that comes after cannot clear it. Only `user_work` advances `work_sequence`, so a turn that a nudge started and the
owner then steered is work (nudge, owner input, end of turn and owner input, nudge, end of turn alike), while a reply to a nudge or
a recovery wake alone never is. Neither kind of turn makes a notice due. The flag is evidence that some user input was in the turn: it is not proof that
the owner wrote it and changes no Chat authorship label or permission (Chat still says nothing about who typed a native user turn).
Both facts are persisted with the projector state, so a restart between the input and the completion keeps them.

*What counts as a notice.* Whether a leading marker was typed by the application is not decided by the parser. The capture/index
boundary hands the projector a typed marker resolver built from the exact registered conversation's input receipts
(`store.conversation_markers` with the session's transcript source, `reader.proven_type`: same agent, provider and conversation,
the declared type equals the stored type, and the input reached delivery). Text that merely looks like a marker, a marker id of
another conversation or a declared type that differs from the stored one is the owner's own input: it still *displays* as an
(unverified) notice, but it is work. A projector with no resolver proves nothing. Display provenance (`entry.system`) remains the
separate reader check.

### Offline, restart, uncertainty

* No live session: the need stays pending (`offline`); nothing starts or resumes a session. It is delivered only after someone
  starts a session by hand and the native backend binds it.
* `uncertain` (the transport may have typed it) is stored and never resent to that conversation, across restarts; it holds back
  only its own events.
* A reservation left by a crash is settled from the native input record only: no input ⇒ cancelled (`unclaimed`), never typed
  later; input ⇒ its final state; a still-`submitting` claim waits for the native backend's own recovery.

### Bootstrap recovery wake

`POST /api/agent/inbox/recovery` (credential-bound registration, episode UUID) calls
`NudgeBackend.recovery_notice(agent, episode, consumer_id=…)` types the wake as its own system input type,
`bootstrap_recovery` (`arbiter.admit_recovery`, the same schema, marker and arbiter path, text behind
`<qorqut-system-input … type="bootstrap_recovery">`, metadata `{episode}`), never as `inbox_nudge`. It is idempotent per episode
and requires the exact conversation at the producer boundary: `consumer_id` is mandatory and the arbiter rejects a recovery
request without `expected_binding`; the wake is pinned to the live session whose conversation equals `consumer_id` (otherwise
`deferred`/`session_not_current`, nothing typed or stored). It is not an inbox event, covers none, acknowledges nothing and is
unrelated to inbox coverage: it neither announces an event nor makes an announced one due again, and its own reply is no work. It starts no
session and adds no transport. Chat shows it as the unverified-or-verified notice `system.input_type="bootstrap_recovery"`, labelled
"Session recovery" by the UI. It is kept distinct from provider-native roles. The supervisor retries only on `deferred`.

## State and DTO contracts

| Surface | Contract |
| --- | --- |
| Tables (Operations contribution `inbox-nudges`) | `inbox_nudge_needs`, `inbox_nudge_attempts` (immutable, exact binding + session key, covered event ids = the durable coverage, diagnostic progress), `inbox_nudge_evidence` (append-only). No text, summary or screen. Unchanged schema. |
| Host tables extended | `native_agent_counters.work_sequence/user_sequence`, `native_replies.origin` and `native_replies.user_work` (native_chat schema, forward-only triggers); `native_inputs.input_type` adds `bootstrap_recovery` (system origin). |
| `NudgeBackend.status(agent_id)` | `{state, reason, attempts[…]}`; `state` ∈ idle, pending, deferred, submitted, uncertain, stale, unavailable. Coverage is read for the live conversation (`engine.status(agent, binding=binding_of(live))`): `submitted`/`uncertain` only when every pending authorized event is announced or possibly announced to it, so a fresh conversation or a newer event is `pending`. Without a live session anything authorized pending is `pending` with `reason: "offline"`, whatever an earlier conversation was told; nothing pending is `idle`. The `pending` count is distinct from coverage. Never "read"; detailed reasons stay backend diagnostics. |
| Activity DTO `agents[].inbox` | `{state, reason, last_attempt_at}` with the state above, or `unknown` (`nudge_backend_not_running`, `nudge_status_unavailable`). Replaces `{state, last_check_at, lease_until}`. |
| Owner inbox read `GET /api/me/agents/<id>/inbox` | the unchanged `items`, `next_cursor` and `subscriptions`, plus `delivery: {state, reason, last_attempt_at}`: the same definition and meaning as `agents[].inbox` (`native_chat.nudges.delivery_view`), read only after the owner/recipient authorization, `unknown` with a reason when diagnostics are unavailable. No watcher, lease or poll field. |
| Content-session notice | `ContentSessions._notification[sid].state`: pending, queued (deferred), sent (acknowledged by the creator), uncertain, stale, unavailable. `dispatch` sends nothing. |
| Chat DTO | the empty `actions` field is removed; the P7-review corrections (current vs selected conversation, `session.binding`, per-input `binding`/`idempotency_key`, pinned `POST chat/messages`) are specified in `NATIVE_CHAT_DTO.md`. |
| Ownership record | `native_session_owners` is the exact-session owner (agent, consumer, verified native binding, generation, desired state). It has no poll, check or lease column, no generation default and refuses an empty binding or a zero generation; `Inbox.session_owner` replaces the time-expiring `lease` and never expires. A session is a new owner only through the verified native binding. |

## Lifetime

`Runtime.inbox_nudges` starts after `native_chat` and before activity, independent of any Chat view or browser. It is stopped
after `content_sessions`, before `native_chat` and so before the database close: `stop(timeout)` sets the stop flag, wakes, joins
within the bound and raises `TimeoutError` while the thread survives, keeping the reference so a later stop proves the drain.
Stopping twice is harmless; start after stop starts a fresh thread. The backend only sends through the arbiter.

`NudgeBackend(…, reconcile=callable)` is the single hook for the accepted Task snooze reconciliation: it runs first in every
pass in its own write transaction (`inbox_nudges.reconcile`) and a failure never blocks nudges. This tree does not contain the
P3-only method; the combined host passes it as `NudgeBackend.build(runtime, inbox, reconcile=…)`.

## Removed

Agent-side: the Codex/Claude watcher process and its setup (`inbox/watcher.py`, `ensure_watcher`, `run_watcher`, `deliver`),
the watcher interval in registrations/preparation, the inbox-check hook receipts and notification routes
(`/api/agent/inbox/{watch,release,notification,notified}`), poll-lease expiry (`lease`, `release`, `notification`,
`deliver_notice`, `LEASE_SECONDS`), the `service_notice` provider bypass, supervisor legacy watcher registration and candidates,
and the poller-only health advice in diagnostics, activity and resolver. Emitted guidance (`inbox/defaults.py`, bootstrap
setup note `INBOX_SESSION_NOTE`) now states the final semantics: nothing to start, poll or keep alive; Qorqut may type one
advisory notice; review with MCP tools at a checkpoint; acknowledge exact events.

The supervisor keeps binding recovery, queued follow-ups (bootstrap receipt, recovery wake), context-recovery seeding,
reference probes, and a per-session `--watch` host child that only runs the delegated Codex runtime
(`durable_delegation_enabled` and runtime `codex`); other sessions get no process. Delegation authority formerly implied by a
live poll lease is now the generation-fenced ownership row plus that host child's liveness.

## Deliberately retained (not poller behavior)

Incident taxonomy names `inbox.watcher.*` (the persisted incident vocabulary) and `sandbox/tmux.queue_notice` (broker transport for
non-host targets, pinned by `test_native_chat_producers.py`). The pre-fencing helper recognition, generation-0 owner handling, the
native-binding legacy probes, the release worker's poll continuity, `inbox_notification_receipts`, the watcher lease/poll columns and
`inbox_deliveries.notified_*` were removed by the runtime-composition piece: the table is `native_session_owners` and release
continuity compares the session owner and generation, never a poll. Role/template source text that still mentions watching is P8.

## Evidence

Operations at the pinned `ed85af5`: `tests/test_inbox_nudges.py` (engine, including the scan continuation and event
coverage). Independent additional acceptance evidence, not part of the pinned engine tree: `tests/test_inbox_event_coverage.py`
(event coverage through the public engine API) on the separate Operations test branch `v1.2/inbox-reliability-regressions`,
commit `63cbb46`. Host: `tests/test_inbox_nudges.py` (synthetic fake provider panes and a fake
Codex queue driven through its steering path; no browser, server, real session or model; Claude and Codex adapters) covers
coalescing; stale full or partial acknowledgement and revoked access before input; immediate busy delivery; occupied composer
deferral; chat input first; an unchanged backlog never announced again after the running work finishes, a direct Chat input is
worked through, a nudge reply or deferral, either steering order (nudge, owner input, end of turn and owner input, nudge, end of
turn), a restart between the input and the completion, or elapsed time; the engine receiving no work/user counter and the memo
fingerprint ignoring them; offline then manual binding; restart with an uncertain receipt, with a crash-left unclaimed
reservation and with a reservation typed before the crash; two concurrent passes typing one notice; a new conversation getting the
outstanding batch; unchanged Task, Signoff, message and acknowledgement state; a retained private message; several business and
Global recipients; recovery wake (its own type `bootstrap_recovery`, idempotent, required exact conversation/binding, pinned to the
asking conversation, no inbox event announced or repeated, "notice" provenance in Chat); a lookalike marker that is the owner's
work; the owner inbox read's `delivery`; lifetime drain and restart. Its "event coverage" section needs the event-coverage engine of the pin:
a new event announced beside an announced backlog with the counters unchanged, events arriving during the 60 s spacing delivered
together afterwards, status not `submitted` while a newer event is unannounced, an uncertain notice holding back only its own
events, the same conversation in a new process or after a backend restart not told again, and (with the engine's binding-aware
status) a fresh conversation `pending` before its first notice and an agent offline after an old notice `pending`/`offline`.
`test_native_chat_projection.py`
(resolver-proven origin and work, both orders, Claude and Codex restarts), `test_native_chat_chat.py`,
`test_native_chat_producers.py`, `test_dev_review_cadence.py` cover the counters, DTO, the retained-producer inventory and the
marker-recognizing hook.

Legacy test files that exercised the poller were deleted or converted (`tests/inbox_support.py`: `pending`, `pending_count`,
`owner`, `claim`). Most of the legacy `app/tests` tree does not collect in this checkout (`bazarlyk.contracts` is gone); those
edits were checked with pyflakes and import/target audits only and are not executed.

## Integration notes

* **Dependency**: the parent depends on the P5 Operations feature wheel `qorqut-operations==1.2.0.dev0` built from Operations
  branch `v1.2/inbox-nudges` (commits `ddc1167`, `313349b`, `828b689` on foundation `138f40c`). The parent's `qorqut-operations` gitlink and
  the retired private foundation receipt still name the foundation commit `138f40c`; they were not moved. The host-foundation
  receipt check compares the installed wheel with that receipt, so it is expected to differ until integration records the
  accepted engine commit and wheel.
* **Inbox reliability repair (2026-10-14)**: the `qorqut-operations` gitlink pins the approved engine `ed85af5` (Operations
  branch `v1.2/inbox-reliability-engine`: event-based eligibility `ff69439` plus `InboxNudges.status(agent_id, binding=…)`,
  which `NudgeBackend.status` calls). Verified by focused synthetic tests only; not deployed. The running installation keeps its
  previous host and engine until a coordinated host and engine release.
* **P3**: Story-to-Task subscription/membership replacement edits `inbox/service.py` subscription code and calls this backend's
  `reconcile` hook with the Task snooze reconciliation; nothing here duplicates its records.
* **P4/P6**: assembly and lifetime declarations (`inbox-nudges` contribution, `inbox_nudges` lifetime step) combine with the
  Context lifetime and storage declarations at integration; the committed `composition/schema_ddl.py` is the host DDL.
