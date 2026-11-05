# Inbox nudges (engine)

`qorqut_operations.inbox_nudges` keeps the durable state of the advisory that tells a running agent it has pending inbox
events. It starts nothing, owns no thread and has no clock or native I/O; the trusted host supplies the exact verified native
binding and the current event authorization, sends through its own input arbiter, and settles each attempt with what the
transport reported.

A nudge changes no acknowledgement, Task, Action, Signoff or routine state. `submitted` is the native transport's report
that the notice was sent; it is never evidence that the agent read its inbox or handled anything. Only an exact
acknowledgement is. Reading is not acknowledgement either.

## Storage (`storage_contribution()`)

Owner `inbox-nudges`; it reads the host tables `inbox_events` and `inbox_deliveries` (declared `required_tables`) and owns:

| Table | Role |
| --- | --- |
| `inbox_nudge_needs` | One row per registered agent: whether it has pending authorized events (`active`), its activation count (`cycle`, a diagnostic), why no notice is due yet, the next attempt sequence, and `scan_after`, where the next bounded scan of its pending deliveries resumes. Never deleted; `cycle` and `next_sequence` only move forward. Cycle 0 exists only while the row is inactive (an agent whose scan has not yet found an authorized event); the first activation is cycle one. |
| `inbox_nudge_attempts` | One immutable attempt per reservation: kind, exact binding, its session key, the covered event ids, the host's observation when it was made (a diagnostic), state and reason. Only `prepared` (and `uncertain` to `submitted`) may change; never deleted. At most one `prepared` attempt per agent. |
| `inbox_nudge_evidence` | Append-only exact evidence (hashed outcome) for every state change. |

No table stores an event summary, a body, the typed text or a screen. The schema is unchanged from the earlier cycle- and
progress-based rule; its rows are read as they are (see Coverage).

## States

`prepared` (reserved, nothing typed) -> `submitted` | `uncertain` | `deferred` | `failed` | `unavailable` | `stale`.
`deferred`, `failed`, `unavailable` and `stale` are definitely not typed. `uncertain` may have reached the session and is
never sent again to that session; exact later evidence may turn it into `submitted`.

## Ports and calls

* `Visibility.authorized(uow, agent_id, events) -> set[int]`: which event rows the agent may currently see. The engine never
  trusts a directory, tag or display name. It calls the port inside a read unit of work, never under the writer lock.
* `InboxNudges(database, visibility, policy=Policy(), clock=...)`:
  `candidates(agent_ids)`, `prepare(agent_id, binding_or_None, observation)` -> `Reservation | Waiting`,
  `validate(attempt_id, binding)` (the fresh check immediately before provider input; cancels a stale reservation),
  `settle(attempt_id, Outcome)`, `cancel(attempt_id, reason)`, `unsettled(agent_id=None)`,
  `status(agent_id, *, limit=5, binding=None)`.
* `Binding` is the host's exact verified session (binding id, full agent id, provider, conversation, generation, transport,
  process id and start). No length or alphabet is assumed for agent ids.
* `Observation(work, user, working)` is recorded with each attempt for diagnostics only. It decides nothing; a host may pass
  the neutral `Observation()`. There is no old/new mode.

## Eligibility rule

An event warrants a notice to one native conversation when it is delivered to the agent, unacknowledged, currently authorized,
and not already announced (or possibly announced) to that conversation. A conversation's identity is the agent, the provider
and the native conversation id; the exact binding (generation, process, transport, binding id) still decides where one attempt
may be typed, and a receipt of another binding never settles it.

* An unchanged unacknowledged backlog is never announced again to the same conversation, whatever Chat inputs, replies, work
  or time pass, or whatever the agent says about deferring it. Chat neither rearms nor holds back a notice.
* A newly eligible event is announced independently of older pending ones: a notice covers only events not yet covered.
* A new native conversation receives a batch of the outstanding eligible events (`new_session`). Reattaching or restarting the
  same conversation (a new exact binding, a backend restart) replays nothing.
* Offline (no binding) keeps the need pending (`Waiting("offline")`); nothing here starts or resumes an agent.

Kinds keep their stored names for receipt compatibility, with event-based meanings: `initial` is the agent's first notice that
may have been typed, `new_session` the first to a conversation after another conversation of the agent had one, and
`follow_up` a notice of newly eligible events to a conversation that already had one.

Pacing only bounds when a due notice is typed: one `prepared` reservation per agent (`Waiting("reserved")`);
`Policy.minimum_repeat_seconds` (60) between two effective notices of the agent (`Waiting("minimum_spacing", not_before)`),
during which new events stay pending and are announced together when it expires; and, after a notice that was definitely not
typed (deferred, failed or unavailable), its own bounded exponential backoff for the same exact session (5 s, 30 s, 60 s
base; 300 s cap; `Waiting("retry_later", not_before)`). The first eligible notice has no delay of its own beyond the host's pass.
When nothing is eligible because every pending authorized event is already covered, `prepare` answers `Waiting("coalesced")`.
A need's `cycle` counts its activations and gates nothing; the earlier progress allowance and cycle rearm are gone, and the
`awaiting_progress` and `uncertain_unreconciled` verdicts are no longer produced.

## Coverage

The coverage of a conversation is every event id in `event_ids_json` of the agent's `submitted` and `uncertain` attempts whose
`binding_json` names the same provider and conversation id, across all of the agent's history and every cycle: nothing is
capped, cleared, reseeded or migrated, so rows left by the earlier rule count as they are. `deferred`, `failed`, `unavailable`,
`stale` and `prepared` attempts cover nothing. An `uncertain` attempt suppresses only its own events for its own conversation:
it is never replayed and never holds back a newer event. Membership is decided in SQL (`json_each` over the stored lists,
restricted to the agent and conversation, evaluated once per page), so a scan never materializes the history in Python and
covered rows cost no page budget or authorization.

A reservation records the bounded batch (at most 100 ids) eligible when it was made; an event that arrives later is left
eligible for the next batch. Immediately before input, `validate` requires the exact binding and that every covered event
still pending is still authorized and not covered for the conversation, with at least one left: an acknowledged event simply
drops out, but a pending covered event whose access was revoked makes the reservation `stale` (`coverage_handled`), so a
delivered notice never records coverage for an event the agent could not see and the next pass reserves what is eligible now.
Inside the write that makes a reservation, the batch is filtered again against current coverage, so callers that read the
same facts concurrently never announce an event twice.

After a restart the host reconciles `prepared` attempts from its native-input record before sending anything new: no input
claim may be cancelled, a submitted input settles, uncertain evidence stays `uncertain`. Transport uncertainty may make input
unsafe at the host; it is never a conversation-wide coverage ban here.

## Status

`status(agent_id, binding=...)` reads coverage for the conversation of the binding the host supplies (its current live
binding, which must belong to `agent_id`; another agent's is refused with `invalid_binding`). Without a binding it reads the
conversation of the agent's latest attempt, as existing callers expect; offline diagnostics are the host's. It reports, with the
earlier vocabulary (`idle`, `pending`, `deferred`, `submitted`, `uncertain`, `stale`, `unavailable`):

* `pending` (or the latest undelivered attempt's `deferred`, `unavailable` or `stale`) while some pending authorized event is
  not yet announced to that conversation, or a scan could not prove there is none; never `submitted` just because the last
  notice was. A fresh live conversation that waits for the spacing is `pending` with its `unannounced` events;
* `uncertain` when some pending authorized event is only possibly announced to it;
* `submitted` only when the bounded scan of covered events read all of them and every one is announced. When that scan stopped
  early and none it read is only possibly announced, unseen ones could be, so the state is `pending` with reason
  `scan_incomplete` instead;
* `idle` when nothing authorized is pending.

`pending` counts the authorized pending events seen and stays distinct from the coverage counts `unannounced`, `announced` and
`possibly_announced`; `more` says a scan stopped early (a full sample or the row budget), so the counts are lower bounds. Both
scans are bounded (no full count). These extra keys and the detailed reasons are backend diagnostics; reasons reuse the existing
codes.

## Bounded scan continuation

An agent's pending deliveries are read in bounded windows (200 rows per page, 2,000 rows per pass), each event authorized again
as it is read; with a binding, events covered for its conversation are excluded in SQL. `scan_after` is the durable
continuation: a pass resumes after it and saves the next position, which is right after the last event of a full sample (so no
authorized event past a page or sample boundary is skipped), the last row inspected when the row budget ran out, or 0 once the
last pending row was read, so a long run of events the agent may not see is crossed over several passes and every earlier entry
is read (and authorized) again on the next wrap. While a found batch has to wait (offline, reserved, spacing, backoff) the
position stays where its window began, so those events are the ones reserved once a notice is due. The position is saved in the
same transaction as the rest of `prepare`, survives a restart, and is only a position: it is not an acknowledgement or
coverage, caches no authorization and announces nothing. A row is created for an agent that has none (inactive, cycle 0) when
the first window finds nothing authorized yet.

What a window may prove: emptiness only from "no such pending delivery rows at all" or "a window that began at the start and
read to the last pending row found no eligible event". A partial window, or a tail window that began after the start, that finds
nothing proves nothing about the rows it did not read: it never deactivates an outstanding need (`prepare` answers
`Waiting("scan_incomplete")` and `status` stays `pending`).
