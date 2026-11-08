# Integration Envelope

A scoped, trusted-service credential class for automated cold-outreach callers
(e.g. the Paper outreach service), layered on the existing send ledger, capacity
reservations, and mailbox/shared-sender model. It never replaces the read/send/
admin tokens or their routes; it adds a fourth credential kind with its own,
narrower route surface. Installing this code enables no live sending: an
integration client must be explicitly configured (`EMAIL_INTEGRATIONS_FILE`)
before any `/integrations/*` route accepts a request.

## Credentials

`EMAIL_INTEGRATIONS_FILE` optionally names an owner-only JSON file, a list of:

```json
[
  {
    "client_id": "outreach",
    "secret": "<random, at least 32 ASCII characters>",
    "workflow_owner": "outreach",
    "allowed_mailboxes": ["biz-a"],
    "business_ids": ["<registered business UUID>"],
    "handoff_destinations": ["qorqut"]
  }
]
```

`client_id` and `workflow_owner` match `^[A-Za-z0-9_-]{1,60}$`. `secret` is
32-200 ASCII characters, unique across every read/send/admin token and every
other integration client. `allowed_mailboxes` must be non-empty and every ref
must already be a registered mailbox; `business_ids` is optional and, when
set, must be a subset of the businesses those mailboxes belong to.
`handoff_destinations` is optional (default `[]`), a list of other
`workflow_owner`-shaped strings this client is allowed to hand a thread off
to via `POST /integrations/threads/{id}/handoff`; see that route below. File
mode 0600, outside source repositories, same as `EMAIL_SHARED_SENDERS_FILE`.
Loading is validated at `Settings` construction; an invalid file refuses
startup.

Authentication is `Authorization: Bearer <secret>`, constant-time compared
against every configured client (never the read/send/admin pool, so a legacy
token cannot call an `/integrations/*` route, and an integration secret cannot
call a legacy route). As with every other credential in this service, `token`
in the query string is refused outright and no token is ever logged. A client
can act only through its own `allowed_mailboxes`/`business_ids` scope; the
service enforces this on every route, not just at submission.

## Routes

| Method | Path | Purpose |
|---|---|---|
| POST | `/integrations/intents` | Submit a cold-outreach send intent (idempotent). |
| GET | `/integrations/intents/{intent_id}` | Fetch the current receipt. |
| POST | `/integrations/intents/{intent_id}/cancel` | Cancel if still queued. |
| POST | `/integrations/threads/{thread_or_message_id}/handoff` | Idempotent ownership handoff. |
| GET | `/mailboxes/{mailbox}/messages?after=&limit=` | Scoped reply-cache view (shared with the legacy read credential; see below). |

All bodies are JSON, capped like other strict routes (`MAX_HTTP_BYTES` for the
intent submit, 64 KiB for handoff). Unknown fields are rejected
(`extra="forbid"`, `strict=True`) everywhere.

## POST /integrations/intents

```json
{
  "intent_id": "camp-1-enr-1-step-0",
  "message": {
    "request_id": "<ignored; the server derives the ledger key>",
    "business_id": "business-a", "mailbox": "biz-a",
    "from_address": "hello@biz-a.example", "to": ["prospect@example.test"],
    "subject": "...", "body": "...", "expires_at": "2026-10-01T00:00:00+00:00"
  },
  "correlation": {
    "workflow_owner": "outreach", "account_id": "acct-1", "business_id": "business-a",
    "campaign_id": "camp-1", "enrollment_id": "enr-1", "step_index": 0,
    "manifest_digest": "sha256:...",
    "stop_key": {"contact_key": "prospect@example.test", "business_id": "business-a"}
  },
  "category": "cold_outreach",
  "expires_at": "2026-10-01T00:00:00+00:00"
}
```

`message` is otherwise an `Outgoing`-compatible strict payload (same fields,
same validators); its own `request_id`, `category`, `workflow_owner` and
`stop_key` are ignored and overwritten by the server (`request_id` is derived
from `(client_id, intent_id)`; `category` is always the envelope's
`"cold_outreach"`; `workflow_owner`/`stop_key` come from `correlation`). Its
`expires_at` must equal the envelope's `expires_at` exactly — one deadline,
not two independently editable ones. `expires_at` is a hard deadline for the
worker to stop trying, never a scheduled send time; submission always queues
immediately.

Response is `202` with `state: "queued"` and `transmission_began: false` on a
first-time accept; deferred to the worker to actually reserve capacity and
dispatch. There is no synchronous dispatch path for this route, specifically
so the reply-stop guard's pre-send check always has a real window to act in
before any provider call. Validation failures are `400 invalid_intent` /
`invalid_message` (with a `fields` list) / `invalid_intent_request` depending
on which layer rejected it (envelope schema, wrapped-message schema, or
cross-field business rule such as an out-of-scope mailbox or a mismatched
`expires_at`).

### Idempotency and conflicts

Idempotent on `(client_id, intent_id)`. A replay with byte-identical resulting
`Outgoing` content (via `Outgoing.digest()`) returns the exact same receipt,
including its current state — never resubmitted or re-reserved. A replay
whose resulting content differs (any digest-participating field, including
`category` when set explicitly) returns `409 intent_conflict` and leaves the
original untouched. An `intent_id` this client has cancelled without ever
submitting it (see "Cancellation tombstone" below) returns
`409 intent_cancelled` and queues nothing, however many times it is retried.

## GET /integrations/intents/{intent_id}

Returns the current receipt, or `404 intent_not_found`. A tombstoned
`intent_id` (see below) is `200`, not `404`. Receipt shape:

```json
{
  "intent_id": "camp-1-enr-1-step-0", "client_id": "outreach",
  "request_id": "itg-<sha256 prefix>", "category": "cold_outreach",
  "workflow_owner": "outreach", "state": "queued",
  "transmission_began": false, "updated_at": "...",
  "result": {}
}
```

### State machine

| `state` | Meaning |
|---|---|
| `queued` | Accepted, not yet claimed by the worker. |
| `capacity_deferred` | Worker tried to reserve capacity and the shared 150 ceiling or the sender's own rolling limit was exhausted; `result.retry_at` carries when to look again (possibly `null`, meaning an unresolved reservation is holding the budget). |
| `sending` | Past the reply-stop guard's cancellation boundary; draft created and/or submission in flight. Not further cancellable. |
| `confirmed` | Provider accepted the submission (`sends.state == "sent"`). |
| `failed` | Terminal failure, e.g. `approval_expired` before dispatch, or a non-transient provider error. |
| `cancelled` | Either the client cancelled it while still `queued`, or the reply-stop guard cancelled it at the worker's final send claim. `result.reason` distinguishes `integration_canceled` from `stop_guard`. |
| `uncertain` | Dispatch outcome unknown (e.g. a transient/connection failure after the provider call). Never auto-resubmitted; a later lookup or background pass reconciles it from the saved evidence, exactly like the legacy send ledger. |

`transmission_began` is `true` only once the row has actually left `queued`
toward the provider (i.e. reached `creating`/`draft`/`sending`/`accepted`/
`sent`, or a terminal `failed`/`uncertain` reached *after* that point) — it is
`false` for every outcome the ledger's own `result.not_sent` flag marks as
never dispatched (owner-cancel, guard-cancel, or expiry caught before the
provider call), even though the ledger `state` itself has already moved to a
terminal value like `canceled` or `failed`.

## POST /integrations/intents/{intent_id}/cancel

Cancels only if still `queued`; otherwise returns the current (unchanged)
receipt. The response always reports whether transmission had already begun
(`transmission_began`), so a caller cannot mistake "too late to cancel" for
"cancelled".

### Cancellation tombstone

`POST .../cancel` for an `intent_id` this client has never submitted no
longer returns `404`. Instead it durably records a per-client cancellation
tombstone (`client_id`, `intent_id`, `created_at`) and answers `200` with the
normal receipt shape plus `"tombstone": true`:

```json
{
  "intent_id": "camp-1-enr-1-step-0", "client_id": "outreach",
  "request_id": null, "category": null, "workflow_owner": null,
  "state": "cancelled", "cancelled": true, "transmission_began": false,
  "tombstone": true, "updated_at": "...",
  "result": {"reason": "integration_canceled", "not_sent": true}
}
```

This closes a race where `POST /integrations/intents` waits on the same
internal lock the tombstone check runs under: a submission that was already
queued behind a slow request sees the tombstone the instant it acquires that
lock and is rejected `409 intent_cancelled` (see above) instead of quietly
queuing a send the caller had already given up on. `GET` for a tombstoned
`intent_id` returns this same `200` receipt rather than `404`. Cancelling an
already-tombstoned id again is idempotent (the original `created_at` is
kept). Cancelling an intent that *was* actually submitted is unchanged by
this addition — that path never touches the tombstone table.

A consumer that cannot confirm whether a `POST /integrations/intents` call
reached the server (e.g. a client-side timeout) must cancel-before-closing:
call `POST .../cancel` for that `intent_id` rather than treating a `GET`
`404` as proof the intent will never arrive. A `GET` `404` only means "not
recorded yet", not "will never be recorded".

## Reply-stop guard

Keyed on `(stop_key.contact_key, stop_key.business_id)` — the contact/business
scope, deliberately not the workflow owner, so a reply to either producer's
cold message silences the other producer's pending cold sends to that contact
for that business. `workflow_owner` is still recorded on the stop for
attribution and on the reply annotation. A stop record is written in the same
transaction as a correlated inbound reply (`EmailService.sync` ->
`Store.correlate_replies`, run once per mailbox at the end of every completed
sync pass).

`correlate_replies` matches a reply by RFC Message-ID (`inReplyTo`/
`references`) against a `sent` ledger row's own Internet Message-ID. The
matched row is labelled for the guard either by its `integration_intents`
entry (envelope intents) or, for a legacy `/sends` row, by the label in that
row's own stored `Outgoing` payload (see "Legacy `/sends` cold-outreach
label"); a match is recorded only when the label's category is
`"cold_outreach"`. Each inbound message is keyed once, by
`(mailbox, message_id)`, in `message_correlations`; a later label-only
revision of the same message (e.g. a folder-move update) is skipped, so a
guard-triggering reply is never processed twice and a later edit cannot
un-trigger it.

The worker's final send claim (`EmailService.start_send`, before any provider
call, before capacity reservation) checks the guard for every send whose
ledger row is still `queued`; the check keys off the *same*
`(contact_key, business_id)` scope, so it cancels every other pending
`cold_outreach` send that shares that scope — envelope intent or labelled
legacy `/sends` row alike, whatever their workflow owners — not just the send
whose reply triggered the guard. Ordinary/conversational sends
(`Outgoing.category` unset or `"conversational"`, and any legacy `/sends`
submission without the label) are never touched by the guard. The underlying
`integration_stops` table is unique per `(contact_key, business_id)`; the
write is idempotent (`INSERT OR IGNORE`) and keeps the owner that triggered
the first stop.

## Legacy `/sends` cold-outreach label

A legacy caller that books cold outreach through `POST /sends` labels each
message with three additional `Outgoing` fields:

```json
{
  "category": "cold_outreach",
  "workflow_owner": "qorqut",
  "stop_key": {"contact_key": "prospect@example.test", "business_id": "business-a"}
}
```

`workflow_owner` follows the envelope's pattern; `stop_key` is the envelope's
`StopKey` shape, and its `business_id` must equal the message's own
`business_id`. `category: "cold_outreach"` without both fields is rejected as
`400 invalid_message` (the legacy route's existing validation shape); the two
label fields are likewise rejected when `category` is unset or
`"conversational"`, so a producer cannot half-label a send and silently
bypass the guard. All three fields are optional and are omitted from the wire
payload and from `digest()` when unset, so a caller that sends nothing new is
unaffected and a labelled row carries the unchanged `Store.receipt` shape.

The Qorqut cold-outbound-marketing batch producer is the implemented caller:
in `qorqut/background/fastmail_batch.py::_specification` every message the
batch books gets `workflow_owner: "qorqut"` and a `stop_key` built from the
recipient's normalized address plus the business ID. Conversational and human
sends through the same `EmailDelivery` path stay unlabelled. Engine-email must
accept these fields before the Qorqut producer emits them, so the engine
deploys first.

## Correlation and reply visibility

`GET /mailboxes/{mailbox}/messages?after=&limit=` is the existing legacy
route, reused rather than duplicated (per email-outreach's own plan: "use the
existing ... route with the new scoped caller identity"). It now accepts
either a legacy read token or a configured integration secret:
- **Legacy read token**: unchanged behavior, sees every cached message.
- **Integration secret**: `mailbox` must be in the client's
  `allowed_mailboxes` (`403 mailbox_not_allowed_for_client` otherwise); the
  page excludes sent copies (`is_sent_copy: true`, i.e. the engine's own
  outgoing mail cached back from the provider) so a reply-polling loop only
  ever sees actual inbound traffic.

Every item gets an additive `is_sent_copy` boolean and, when correlated to a
sent cold-outreach message (envelope intent or labelled legacy `/sends` row),
a `correlation` object:
`{workflow_owner, intent_id, request_id, matched_internet_message_id,
sent_request_id}`. Uncorrelated items simply omit `correlation`. The `cursor`
field always advances past every row in the underlying page, including rows
filtered out of `items` for an integration caller, so paging cannot stall on
a run of sent-copy rows.

A reply's own mailbox-cache page can be committed (and delivered by a poll)
before `correlate_replies` has run for it; that poll sees the item without a
`correlation`. When `correlate_replies` later records the correlation, it
also bumps that message's revision, so the *same message id* is delivered
again at a higher revision, this time with `correlation` populated. A handoff
that changes a thread's owner re-points an existing correlation's
`workflow_owner` the same way. Consumers must therefore deduplicate by
message id (last write wins), not assume each id is delivered at most once.

## POST /integrations/threads/{thread_or_message_id}/handoff

```json
{"idempotency_key": "handoff-1", "workflow_owner": "qorqut"}
```

`workflow_owner` names the DESTINATION owner, not the caller: it must equal
either the client's own `workflow_owner` (a no-op handoff back to itself) or
one of its configured `handoff_destinations`; any other value is
`400 invalid_handoff_request`. This lets Outreach submit intents as its own
owner and later hand the thread to Qorqut once Qorqut has acknowledged it,
without the two calls needing to agree on a single owner.

A first-time call must also own the SOURCE: `thread_or_message_id` must
already have a recorded reply correlation (in the caller's
`allowed_mailboxes`) whose current `workflow_owner` equals the caller's own
`workflow_owner`, and, when that correlation traces to an envelope intent,
that intent's `client_id` (and business, if `business_ids` is set) must be
this same client. Destination legality is not proof of owning the source
conversation; a client sharing a mailbox with another producer must not be
able to reassign that producer's correlation.

If no correlation is recorded yet for `thread_or_message_id`, the call is
rejected as `404 handoff_target_not_found`; nothing is persisted, so an
identical retry once the reply has been correlated (see `correlate_replies`
below) can succeed. If a correlation exists but is owned by a different
client or workflow owner, the call is rejected as `403
handoff_target_forbidden`. Neither rejection is ever recorded, so it never
produces a false `"ownership": "transferred"`.

### Transfer receipt reuse

A producer can legitimately need to hand the same `thread_or_message_id` off
under more than one idempotency key (e.g. Outreach records several
per-enrollment task rows for a single contact opt-out, each with its own
handoff key). One semantic transfer exists per `(thread_or_message_id,
client_id, destination_owner)`, regardless of how many idempotency keys
record it: if the correlation's current owner is no longer this client's own
`workflow_owner` but instead already equals the requested destination, and
this client has a prior `POST .../handoff` on file for that exact
`(thread_or_message_id, destination_owner)` pair (any idempotency key), the
new key is recorded pointing at that same transfer's result and the call
succeeds `200` with `"already_transferred": true` added to the normal
success shape, instead of re-running (and failing) source-ownership
verification against a correlation this client itself already moved.
Nothing about the correlation or its `c_owner` annotation changes again — it
was already re-pointed by the first transfer. A destination this client has
no prior transfer for is still `403 handoff_target_forbidden`, exactly as
before, even when some other client did transfer it there; and a *different*
destination reusing an *already-used* idempotency key is still
`409 handoff_conflict`, unaffected by this addition.

Idempotent on `(thread_or_message_id, idempotency_key)`. A first call that
passes ownership verification records the source client, the source owner
(the client's own `workflow_owner`) and the destination owner, and returns
`{"thread_or_message_id", "idempotency_key", "workflow_owner", "client_id",
"ownership": "transferred"}` (`workflow_owner` here is the destination). A
repeat call with the same key returns the identical stored result
(byte-for-byte, so a caller cannot distinguish "first time" from "replay"
from the response shape) without re-running ownership verification. A repeat
call with the same key but different resulting content (different
destination `workflow_owner`) returns `409 handoff_conflict` and leaves the
original untouched. The recorded handoff is permanent; nothing in this
engine reverses it.

When the destination differs from the source, any existing reply correlation
for that `thread_or_message_id` (in the caller's `allowed_mailboxes`) has its
`c_owner` annotation re-pointed to the destination, in the same transaction,
with the same revision-bump `GET /mailboxes/{mailbox}/messages` re-delivery
described below for a newly recorded correlation. The reply-stop guard's
`(contact_key, business_id)` scope is untouched by a handoff.

## Capacity sharing

Intent submission enqueues through the exact same `Store.begin_send`/
`EmailService.start_send`/`Store.reserve` path as every other send, so the
installation-wide 150-per-24h ceiling and each shared sender's own rolling
limit are shared, atomically, across integration callers and direct send
clients alike. As with existing shared-sender sends, this cap only applies
when the message actually resolves to a configured shared sender (own-mailbox
direct sends are uncapped, unchanged). `capacity_deferred` carries
`result.retry_at`; a `null` retry time means an unresolved reservation is
holding the budget rather than a clean window existing.

## Error codes

| Code | Route(s) | Status |
|---|---|---|
| `unauthenticated` | any `/integrations/*` | 401 |
| `invalid_intent` | POST intents | 400 |
| `invalid_message` | POST intents | 400 |
| `invalid_intent_request` | POST intents | 400 |
| `intent_conflict` | POST intents | 409 |
| `intent_cancelled` | POST intents | 409 |
| `intent_not_found` | GET intent | 404 |
| `mailbox_not_allowed_for_client` | GET messages | 403 |
| `invalid_handoff` / `invalid_handoff_request` | POST handoff | 400 |
| `handoff_target_not_found` | POST handoff | 404 |
| `handoff_target_forbidden` | POST handoff | 403 |
| `handoff_conflict` | POST handoff | 409 |
| `request_too_large` | any body route | 413 |

## What legacy consumers keep

Every existing route, credential, and receipt shape is unchanged. `Outgoing`
gained one new optional field, `category` (`"conversational"` or
`"cold_outreach"`, default unset/`None` meaning conversational), and the two
optional cold-outreach label fields `workflow_owner` and `stop_key`
documented above; like `transport`/`sender_ref`/`forward_message_id`, an
unset `category` is excluded from `digest()` and from the wire payload, so
every historical digest and every existing caller that never sets it is
unaffected. Setting any of them explicitly (including `category` to
`"conversational"`) does change the digest, matching the existing precedent
for `transport`. The legacy `/sends` and `/mailboxes/{mailbox}/send/cancel`
routes, and the read/send/admin token pool, do not gain any new power from
the envelope and cannot reach any `/integrations/*` route; the envelope's
wrapped `message` has its `category`, `workflow_owner` and `stop_key` set by
the server from the envelope correlation rather than by the caller.

## Resolved ambiguities

3. **Reply-cache route**: reused the existing `GET /mailboxes/{mailbox}/messages`
   route with dual credential support, instead of adding a new path, per the
   email-outreach plan's own explicit instruction.
4. **Submission never dispatches synchronously**: even when capacity is
   trivially available, `POST /integrations/intents` always returns `queued`
   and defers to the worker pass. This keeps the reply-stop guard's mandated
   pre-send check point deterministic and gives the guard's race window a
   fixed, testable shape rather than depending on incidental timing.
5. **Cross-caller ledger key namespace**: the integration's send-ledger
   `request_id` is deterministically derived (`itg-<sha256 prefix>` of
   `client_id:intent_id`) rather than reusing the caller-supplied
   `message.request_id`, so an Outreach-chosen ID can never collide with a
   direct send client ID in the shared `sends` table.
6. **`transmission_began` semantics**: defined as "the row reached
   `start_send`'s post-guard code", using the ledger's own `not_sent` marker
   rather than raw ledger state, since a cancelled/failed-before-dispatch row
   still shows a terminal `state` but must report `transmission_began: false`.
7. **`category` and digest**: an unset `category` is excluded from the digest
   (backward compatible); an explicitly set value participates in it, exactly
   like the existing `transport` field. A test asserting the opposite (any
   explicit `category` value producing an identical digest to an unset one)
   would contradict that existing precedent and was corrected rather than
   the design.
8. **Handoff idempotent-replay shape**: the receipt omits any
   first-call-only flag (e.g. "created"), so repeated calls with the same
   idempotency key are byte-identical, matching "repeated calls return the
   same result" literally.
9. **Guard scope**: the guard cancels every pending `cold_outreach` send
   sharing the same `(contact_key, business_id)` scope, across every workflow
   owner and both submission paths, not only the send whose reply triggered
   it — a contact who replies mid-sequence should stop the whole sequence,
   and a reply to one producer must stop the other producer's sends.
10. **Qorqut cold-producer labeling**: implemented on both sides. The engine
   accepts and guards the legacy `/sends` label described above; the Qorqut
   cold-batch producer sets it in
   `qorqut/background/fastmail_batch.py::_specification`.
11. **Sender/business validation timing**: `submit_intent` fails fast on an
   unresolvable sender/business combination (mirroring the legacy `/sends`
   validation) rather than leaving a doomed row queued for the worker to
   discover later.
12. **Handoff `workflow_owner` is the destination**: a producer submits
   intents as its own owner, then hands the thread to a different owner once
   that owner has acknowledged it out-of-band; requiring the two to match
   made every real handoff impossible. `handoff_destinations` makes the set
   of allowed destinations explicit configuration, not implicit trust of
   whatever owner a caller names.
13. **Correlation revision bump**: a message's cache revision only advances
   from `sync_page`; `correlate_replies` runs afterward and previously left
   it unchanged, so a poll between the two could never later observe the
   correlation. Reusing the same revision counter for a newly recorded (or
   handoff-retargeted) correlation makes `GET .../messages?after=` the single
   source of truth again, at the cost of a message id being deliverable more
   than once.
14. **Handoff source ownership**: destination legality alone let any client
   sharing a mailbox retarget another producer's correlation, and an
   uncorrelated target was silently recorded as `"transferred"`. A first-time
   handoff now verifies the caller currently owns every existing correlation
   row for the target (and, via the originating intent when resolvable, that
   it is this same `client_id`) before mutating anything; a missing
   correlation rejects as retryable `404 handoff_target_not_found` with
   nothing persisted, rather than a pending row, since a client can simply
   retry once `correlate_replies` records it.
15. **Cancellation tombstone (C2)**: `POST /integrations/intents` recording an
   intent and `GET .../intents/{id}` reading one were asymmetric on locking —
   the POST waits on `send_lock`, the GET does not — so a caller whose slow
   POST was still queued behind the lock could GET a `404`, wrongly conclude
   the intent would never arrive, and later have that same POST queue the
   send anyway once the lock freed up. A cancel of a never-submitted id now
   tombstones it durably and the submit path's tombstone check runs inside
   the same `send_lock` section as the write it is guarding against, closing
   the race rather than narrowing its window.
16. **Transfer receipt reuse (C3)**: source-ownership verification (#14) is
   necessarily against the correlation's *current* owner, but a legitimate
   second handoff call for an already-transferred message will always see an
   owner that is no longer this client's own `workflow_owner` — that is what
   a successful transfer means. Without an exception, every second handoff
   of an already-moved thread would permanently 403, even from the client
   that performed the original transfer. Scoping the exception to "this
   client already recorded a transfer to this exact destination" (rather
   than, say, ownership being *plausible*) keeps a genuinely foreign client
   unable to piggyback on someone else's transfer.

## Managed outreach routes

A shared sender can serve many businesses while every message stays filed in
its real business. The trusted core service (admin token only, never an
integration secret or agent) calls:

| Method | Path | Purpose |
|---|---|---|
| POST | `/integrations/routes/ensure` | Create or re-drive one client/business route (idempotent). |
| POST | `/integrations/routes/deactivate` | Stop new submissions; cancel queued ones. |
| GET | `/integrations/routes?client_id=` | List a client's routes. |
| GET | `/integrations/routes/{business_id}?client_id=` | Read one route. |

`ensure` takes `request_id`, `client_id`, `business_id`, `sender_ref`,
`generation` (monotonic per business), optional `active` (default `true`) and
optional `mailbox_ref` for an explicit verified reuse of an existing mailbox
of that business. Without `mailbox_ref` the route gets its own archive-only
mailbox `oa-<hash>` (deterministic per client and business). `deactivate`
takes `request_id`, `client_id`, `business_id`, `generation`.

Responses carry `route_id`, `revision`, `generation`, `business_id`,
`mailbox_ref`, `purpose`, `sender_ref`, `active`, `state`
(`provisioning`/`ready`/`uncertain`/`failed`/`disabled`), `ready`,
`blocker` and `receipt` (`request_id`, plus `folder_operation` when a folder
create ran). `ready` also requires the shared sender to be enabled for the
business (`blocker: sender_unavailable` otherwise). No credential is returned.

Rules:

- The client must set `managed_routes: true` in `EMAIL_INTEGRATIONS_FILE`; a
  non-empty static `business_ids` still restricts which businesses a route may
  name (`client_business_restricted`). Such a client may omit
  `allowed_mailboxes`.
- A reused request ID with a different payload, a generation lower than the
  recorded one, or a different desired state at the same generation is `409`
  (`route_request_conflict` / `stale_generation`). A route's mailbox and sender
  are immutable once bound. An exact replay returns the current route and
  re-drives unfinished provisioning.
- Archive folders are created through the existing account `folder_create`
  receipts (`<route_id>-folder-<attempt>`). The folder name carries a random
  token saved before the provider call; a lost response is reconciled by that
  exact name, and JMAP sibling-name uniqueness prevents a second folder.
  Until an exact result is known the route stays `uncertain`.
- An archive-only mailbox (`purpose: "outreach_archive"`) has no address or
  aliases, sends only through its bound shared sender, never takes Reply-To and
  is never a Fastmail From identity. `/mailboxes/configure` refuses it. Legacy
  mailboxes are never reinterpreted: purpose and bound sender are part of the
  immutable binding.
- One effective grant applies to submit, dispatch, mailbox reads, receipts,
  cancel and handoff: the client's static scope, or an active ready route
  whose exact mailbox, business and sender match. For submit and dispatch a
  managed route decides its own binding (mailbox, business and sender) even
  when a static grant names the same mailbox, so a disabled route always stops
  it; other senders on that mailbox keep the static grant. Dispatch rechecks
  it; a queued intent whose grant disappeared is cancelled with
  `result.reason: "integration_route_revoked"` and `not_sent: true`.
  Deactivation cancels the binding's queued intents the same way; a transmission that already
  began keeps its honest receipt. A revoked route stays readable only for this
  client's own correlated replies and stays valid for handoffs of them.
- All routes of a sender share its one physical account quota and the
  installation ceiling.

Fixtures: `route-ensure-ready`, `route-ensure-uncertain`, `route-status`,
`route-deactivated`, `route-stale-generation`, `mailbox-archive-entry` and
`receipt-cancelled-route-revoked` are captured by
`tests/test_managed_routes.py`; regenerate with
`ENGINE_REGENERATE_FIXTURES=1`.

## Fixtures

`tests/fixtures/integration/*.json` are small, exact examples captured from
the engine's own test doubles (`updated_at`/`created_at`/deadline-ish
timestamp fields redacted to `"<timestamp>"`, everything else verbatim):
`submit-request`, one `receipt-*` file per state in the table above
(`queued`, `confirmed`, `cancelled-before-transmission`,
`cancelled-stop-guard`, `capacity-deferred`, `uncertain`, `failed-expired`),
`messages-page-correlated` (a reply page with a populated `correlation`
object), `handoff-acceptance` (a no-op handoff back to the caller's own
owner, on a message it actually correlated), and the legacy label pair
`submit-legacy-cold-request` (a labelled `POST /sends` body) and
`receipt-legacy-cold-cancelled-stop-guard` (its guard-cancelled
`Store.receipt`). `handoff-destination-allowed` (a handoff of an owned,
correlated message to a client's configured `handoff_destinations` entry)
and `handoff-destination-disallowed` (the `400 invalid_handoff_request` body
for an unconfigured destination) cover ownership handoff to a destination
other than the caller's own owner. `handoff-not-yet-correlated` (the `404
handoff_target_not_found` body for a message with no recorded correlation)
and `handoff-foreign-owner` (the `403 handoff_target_forbidden` body for a
message correlated to a different client) cover the source-ownership checks.
`messages-page-reply-before-correlation` and
`messages-page-reply-after-correlation` are the same reply message id from
the same mailbox, at successive revisions, before and after
`correlate_replies` records its correlation. `cancel-unknown-intent-tombstone`
(the `200` tombstone receipt for cancelling a never-submitted `intent_id`)
and `intent-tombstoned-query` (the identical `200` receipt from a later `GET`
of that same id) cover the cancellation tombstone;
`submit-after-tombstone-rejected` is the `409 intent_cancelled` body for a
submission that arrives after that id was tombstoned.
`handoff-second-key-same-transfer` is the `200` body — with
`"already_transferred": true` — for a second handoff call under a new
idempotency key, for the same `(thread_or_message_id, destination_owner)` a
prior key already transferred.
