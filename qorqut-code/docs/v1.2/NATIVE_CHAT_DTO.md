# Native Chat — DTO description

Status: implemented, including the current versus selected conversation, the exact session binding on the session and on
every input, a pinned send and the optional `agent_message` field of a notice entry (below). This is the
contract the dashboard's Chat message list, composer and agent navigation read. It replaces the
retired global-chat runner shapes (`messages`, `turns`, `actions`, `runtime`, stop/restart endpoints; the empty Chat `actions`
field no longer exists). Nothing here changes Focus, the agent-pane layout or its tabs.

All routes are owner-only, under `/api/root/agents/<agent uuid>/chat`, for **any** active registered agent — global or business.
`root` is only the transport; the resource is the exact agent UUID. A business agent additionally requires an unarchived business
and the caller to be its current business owner. An agent bearer credential is refused (`forbidden`); an unregistered or retired
agent is `not_found`. Responses are the usual `{status, data | error}` envelope with `Cache-Control: private, no-store`. A retired
agent's captured conversations are read only through the separate archive reader at the end of this document.

Chat is a **projection**. Opening it, switching agents, reading state or listening to events never launches, resumes, stops or
types into an agent.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET  chat?limit=&before=&conversation=&provider=` | Current state (below). `limit` 1–100 (default 50). `before` is an entry `position` cursor (older page). `conversation` **and** `provider` together select another captured conversation of the same agent as a view-only archive; a default request omits both. |
| `GET  chat/history?before=&limit=&conversation=&provider=` | Older entries only: `{entries, older, rendered_boundary}`. `before` is required; `conversation` + `provider` select the conversation as above. |
| `GET  chat/events?cursor=` | Server-sent events (below). `Last-Event-ID` resumes. |
| `POST chat/messages` `{text, idempotency_key, expected_binding}` | Admit one owner message for the pinned session (below). All three fields are required. Returns at once. |
| `POST chat/attachments` raw bytes | Owner-only copy to the selected agent's downloads; headers `Idempotency-Key`, percent-encoded UTF-8 `X-File-Name`, and `X-Chat-Binding`. Returns `{id,name,size,path,url,reference}`. |
| `POST /api/root/thread-attachments` raw bytes | Owner-only Task-thread file copy to the configured Ordubashy's downloads, also while offline. Headers `Idempotency-Key` and percent-encoded UTF-8 `X-File-Name`; the same result shape and 25 MB bound. Saving sends no message. Normal Task/private-channel authorization still applies when posting the returned reference. |
| `POST chat/read` `{sequence}` | Move the owner's read boundary to a completed reply: `{last_read_sequence, newest_sequence}`. |

Removed (no replacement, they do not exist): `chat/stop`, `chat/actions/*`, `chat/runtime*`, any enable/start/stop of a chat
runner. Agent lifecycle stays in Terminal.

File selection stays in the browser until Send. The client first uploads each file (25 MiB maximum), then includes the
returned local-file Markdown reference in its normal `chat/messages` text. Claude/Codex receives the local path, never file
bytes. The owner sees a reference card using the existing file viewer. Files live in the recipient's
`downloads/<upload-id>/` so matching names never overwrite each other; retrying the same upload key and bytes returns the
same file. Uploads require the exact live captured binding and normal write admission. A failed upload admits no message;
a later failed message leaves its saved files available for an explicit resend. Files are not automatically deleted.
No schema, transcript format, runtime input adapter or agent lifecycle change is required.

## State (`GET chat`)

```
{
  agent_id, name,
  session: { state: "live" | "offline",
             reason: "live" | "binding_not_ready" | "capture_disabled" | "capture_not_ready" | "no_session" | "session_end"
                     | "process_gone" | "native_only" | "unsupported_runtime",
             can_send: bool,            // live AND the transcript is published AND the exact binding is recorded (reason "live")
             working: bool,             // a turn is running (hook activity of this exact session)
             provider: "claude" | "codex" | null, conversation_id: string | null,
             transport: "terminal" | "codex_queue" | null,
             binding: SessionBinding | null,  // the exact open binding a fresh observation matches; see below
             detail: string },          // owner-readable status line
  revision: int, cursor: int,           // durable visible-change counter; the same number is the event-stream position
  conversation: { conversation_id, provider, complete, finalized, error: string | null, captured_length } | null,
  conversations: [ { conversation_id, provider, updated_at, current: bool } ],  // current = the default conversation, never the selected one
  entries: [ Entry ],                   // oldest first, at most `limit`
  older: int | null,                    // pass as `before` for the next older page; null = start of the conversation
  rendered_boundary: int,               // newest completion sequence among the completed assistant entries this page returns (0 = none); acknowledge THIS
  inputs: [ Input ],                    // the agent's 30 most recent application inputs, descending by sequence (newest first)
  last_read_sequence: int,              // owner read boundary
  newest_sequence: int                  // newest completed reply of the whole agent: informational only, never an acknowledgement
}
```

`SessionBinding` is the one public shape of an exact native session: `{ binding_id, agent_id, provider, conversation_id,
generation, transport }`. `session.binding` is non-null only when a fresh observation of the live session matches the agent's
**persisted, open** binding. Reading state never registers, resumes or changes a session; the backend's normal observer records
the binding. Until it has, a live and captured session has `can_send:false`, `reason:"binding_not_ready"`, `binding:null` and a
`detail` saying Qorqut is connecting to it (the same holds for a moment after the client resumes the conversation in a new
process: a new generation). A composer is therefore enabled only when the session has both its published transcript and an exact
binding.

`conversations[].current` marks the **default** conversation (the one of the agent's newest binding, else the most recently
updated capture) by its full `{provider, conversation_id}` pair. Selecting an archive with `conversation` + `provider` changes
`conversation`, `entries`, `older` and `rendered_boundary`, but never `current`, `session` or its `binding`; a selected archive
stays view-only. A native conversation id alone is ambiguous across providers, so a selection needs both (one without the other
is `validation_error`, as is a provider other than `claude`/`codex`), and a pair this agent never captured is `not_found`; it
never falls back to another conversation.

Pages come from a small in-process window cache shared by all agents. Receipt ids repeat across agents and lineages, so a cached
page is reused only for the same agent, archive, receipt id and record hash, the same provider and conversation, and the same
`before`/`limit` window. A capture whose committed receipt names another provider or conversation is never read: its page is
empty with `conversation.error: "receipt_mismatch"`.

`session.can_send` is the composer's enabled flag. When `session.state` is `offline` the composer must say the agent is not running
in its terminal (`reason` tells why); Chat does not offer to start it. While the session is `live`, two reasons keep the composer
disabled and mean different things (both are distinct from `binding_not_ready`, which only exists once a transcript is published): `capture_disabled` — the agent's registration turns transcript capture **off**, so there is no
conversation to show and Chat cannot send (final; nothing overrides it); `capture_not_ready` — capture is **on** but this
session's client hook has not published its transcript yet (typically before its first turn), so the composer should say the
conversation is starting and enable itself when `reason` becomes `live`.

### Entry

```
{ id, kind: "message", speaker: "user" | "agent" | "notice", text, timestamp: iso | null,
  position: int,                // byte position in the conversation's archive; stable (append-only); the paging cursor
  partial: bool | null,         // agent text that is not a finished reply yet (narration before a tool call)
  identity: "native_id" | "raw_range",
  completion_sequence: int | null,  // set on the assistant entry that completed a reply: that reply's agent-wide completion sequence
  completion_id: string | null,     // the durable completion id of that reply (the id behind its `gchat:` notification)
  system: null | { input_id: string | null, input_type: string | null, verified: bool,
                   reason?: "no_matching_input" | "type_mismatch" | "not_delivered" },  // reason only when verified is false
  agent_message?: { message_ids: [string], recipients: [{ agent_id, display_name, role?: { id }, presentation?: { icon? } }] } }
                                    // only on a notice that reports successful outgoing agent messages; absent on every other entry
```

* `user` is any native user turn. A provider records every input as a user turn and gives it **no origin**, so the owner's own
  typing, a Telegram relay, a broadcast and a direct message are all `user`. Chat never labels them owner or system on its own,
  and **no relationship exists between an owner input receipt and a native user entry**: neither the provider nor the
  application records one, so equal text, time or order never link them, and a native user entry is never presented as personally
  authored by the owner. The only entry-to-receipt link is the verified system marker below.
* `notice` is a user turn that begins with the application's `<qorqut-system-input id="…" type="…">` marker (system-origin inputs
  — the inbox nudge `inbox_nudge` and the context-recovery wake `bootstrap_recovery` — are typed behind it; the UI labels the
  latter "Session recovery", never "Inbox nudge" and never as the owner's Chat). `text` is the body without the tags. `system.verified` is true only when **all** of
  these hold, checked against the durable input receipts for the markers this page actually shows (so it does not age out as later
  inputs arrive): the marker belongs to a system input admitted for this very agent, provider and native conversation (a resumed
  conversation keeps it; a marker pasted into another conversation does not match); the declared `type` is the stored input's
  type; and the input reached delivery (`submitting`, `submitted` or `uncertain` — a `waiting`, `failed` or `undelivered` input was
  never typed). Otherwise it is shown as an unverified notice (`verified:false`, `input_id:null`, plus `reason`:
  `no_matching_input` — unknown marker, lookalike or another conversation; `type_mismatch`; `not_delivered`). Equal text alone
  never verifies anything.
* A **successful outgoing agent message** is a `notice` entry with `text:""`, `system:null`, no completion fields and an
  `agent_message` object. It is derived from the agent's own archived `agents_message_send` result, never from delivery state, the
  composer or the inbox: for Codex a completed `bazarlyk` MCP tool-call item of this conversation, for Claude a `tool_result` of
  this conversation's own record, parsed only as the JSON success envelope (`status:"ok"`, `data.message` with `id`, `event_id`,
  `thread_id`, `created_at`, `recipient_id`). An attempted or failed send, an `is_error`/`isError` result, a messages-list result,
  and text that merely quotes a result are not messages; a record that also carries visible user text stays that user entry. One
  record can name several messages (unique IDs, at most 20, in result order). The receipts are checked against the stored
  messages in the same read as the page: a message counts only when its **original** sender is this Chat agent and its original
  recipient is an agent principal (a message to the owner is not an agent message), and an entry none of whose receipts qualifies
  is not returned. `message_ids` are the qualifying stored message IDs in receipt order; `recipients` are their distinct original
  recipients in the order first seen, each with `agent_id`, `display_name` (the stored profile's, else the principal's) and, when the
  stored profile (current or historical) holds them, `role.id` and `presentation.icon` — the inputs of the dashboard's
  existing agent SVG; nothing else of the profile, no SVG, no message body and no raw receipt is sent. The entry keeps the ordinary
  `id`/`position`/`timestamp`/`identity` rules of its archive record (the provider's tool-call or record id, else the raw
  position), so it is stable across window size, paging, resume and restart, and it appears on the next read after the capture
  bumps the revision. It is not a user input, reply, turn origin or user work and changes no counter. The client groups adjacent
  notices; the server returns one per record.
* Internal harness text (`<task-notification>`, local-command and system-reminder wrappers, sidechain/subagent records) is not an
  entry. The raw transcript keeps it in the archive.
* `id` is an opaque 64-hex digest of the entry's agent, provider, native conversation and immutable archive lineage plus the
  record's own identity: the provider's id when it has one (`identity:"native_id"`), otherwise the record's raw position in that
  lineage. It is therefore unique across the agent's conversations (two conversations may share a raw position or reuse a provider
  item id) and stable across resume of the same conversation, restart, window size and paging. It is never a capture id, a
  timestamp, a session generation, a page index or a hash of the text.

### Input (delivery state of application-originated inputs)

```
{ id, kind: "input", sequence: int, origin: "owner" | "system",
  input_type: "chat" | "telegram" | "broadcast" | "direct_message" | "inbox_nudge" | "bootstrap_recovery",
  state: "waiting" | "submitting" | "submitted" | "uncertain" | "failed" | "undelivered",
  reason: string | null, body: string | null, session_id,
  binding: SessionBinding | null,       // the session this input was admitted for; kept after that session ends
  idempotency_key: string | null,       // the key the owner sent; only for origin "owner" + input_type "chat", else null
  evidence: object, created_at, settled_at }
```

* `binding` is resolved from the input's immutable session reference, so a fresh page load still names the exact conversation and
  generation an old receipt belongs to, whether or not that session is still open. An input is never moved to a replacement
  session. A client shows or offers anything about a receipt only for the provider/conversation of its `binding`, never by a
  remembered mapping and never by treating an unknown binding as the current one.
* `idempotency_key` lets the page that sent a Chat message find its own receipt. It is receipt metadata (there is no second
  message store); the source keys of Telegram, broadcast, direct-message and inbox inputs are internal and always `null` here.

* `waiting` — admitted, nothing typed; it is typed into **the conversation it was admitted for** when that composer is ready (the
  owner's draft is never touched). `submitting` — the claim before typing. `submitted` — the submit key was sent (the transcript,
  not this record, is the confirmation; processing is not confirmed). `uncertain` — it may have reached the session; it is
  **never retyped** (show "may not have been delivered; check the conversation", offer no automatic resend). `failed` — definitely
  not typed (refused by the transport). `undelivered` — definitely not typed because the session ended or a different
  conversation took over; the composer may offer to resend the text (the application keeps `body` for a waiting input only).
* `body` is the text the application still holds only while the native conversation certainly does not hold the message
  (`waiting`, and `failed`/`undelivered` so the composer can show or resend it, and `uncertain` so the owner can compare); it is
  cleared once `submitted`, and a private payload (Telegram relay) is never stored at all. There is no writable normalized
  message-body store behind the message list: the list is the transcript.
* Chat messages appear in `entries` as ordinary `user` turns once the native client writes them, with no link to the receipt (see
  the entry rules). An `inputs[]` row is a delivery receipt: with `state` other than `submitted` it is what the composer renders
  as a pending/failed bubble, found by the `idempotency_key` it sent. Only an owner Chat input that is definitely `failed` or
  `undelivered` may be offered for resend; an `uncertain` one is never resent, and a system or relayed input is never presented
  as an owner Chat message.

## Send (`POST chat/messages`)

Body `{ "text": 1–17000 chars, "idempotency_key": 8–120 chars, "expected_binding": <binding_id> }`; all three are required and
there is no unpinned form. `expected_binding` is the `session.binding.binding_id` the page last saw (the opaque id of an exact
native session), not a new authority. Response `{ input: Input, binding: SessionBinding, created: bool }`; `binding` is the
input's own, equal to `input.binding`.

* The message is admitted for the exact native session the pin names, which must be the agent's **current verified session**,
  and typed by the backend into that session's conversation. The request never touches the terminal.
* Order of checks: the owner's current authorization; then an exact repeat; then, for a new request, whether the pin is still the
  live session.
* An exact repeat (same key, text and pin) returns the original input and its original binding (`created:false`) even after the
  session ended or was replaced and even while the agent is offline. The same key with a different text **or a different pin**
  is `idempotency_conflict`. An ambiguous retry therefore keeps its original key, text and pin; a new deliberate send takes the
  then-observed binding.
* A new request whose pin is not the live session (replaced, ended-and-replaced or unknown) is `terminal_changed`: nothing is
  stored or typed, and no path moves an admitted input to another session.
* A busy agent: the message is steered into the running turn where the client supports it (same conversation), otherwise it waits
  its turn; either way it stays `waiting` until typed.
* Refusals (no input stored; all 422 unless noted): `terminal_offline` (no verified live session — Chat never starts one),
  `capture_disabled`, `capture_not_ready` (retry once the session publishes its transcript), `operation_unavailable` (503;
  sandbox/remote target), `unsupported_runtime`, `terminal_changed`,
  `not_found` (404), `forbidden` (403), `validation_error` (empty/oversized/control characters/bad key or missing/malformed pin),
  `idempotency_conflict` (409).

## Events (`GET chat/events`)

SSE, replay-then-tail by durable cursor (`cursor` is `revision`). One event kind:

```
id: <revision>
event: chat
data: {"cursor":<revision>,"kind":"changed","data":{"revision":N,"newest_sequence":S,"waiting":W}}
```

`changed` means "read `GET chat` again" (a new reply was captured, an input changed state, the session changed, its verified
hook activity switched between working and idle, or a read boundary moved). Activity transitions use the existing native
observer and wake the stream even without new transcript bytes; unchanged activity creates no repeated events. Its `newest_sequence` is informational; an event renders nothing, so it carries no `rendered_boundary` — only a fetched page does. `event: reset` (cursor ahead of this installation, e.g. restored database) means reload from zero; `event: done` means the
caller stopped being authorized; `: heartbeat` every 15 s.

## Read state and notifications

* Each completed reply gets a per-agent monotonic `sequence` (the agent's completion sequence) and a durable completion id, and
  **every completed reply is a visible assistant entry** that carries both (`completion_sequence`, `completion_id`). Usually that
  is the assistant message that ended the turn. A Codex turn that names its last message only at the turn end
  (`last_agent_message`, no assistant message before it) is shown as an agent entry of that turn-end record: full text from the raw
  record (never the ≤180-character notification preview), `identity:"raw_range"`, the same lineage-scoped identity as its
  completion. It is shown only when the backend's complete-stream index completed that exact record; a page that starts in the
  middle of a turn never invents it, and a turn end that repeats an assistant message already in the conversation adds no second
  entry (that message keeps its identity and carries the completion). `newest_sequence` is the latest of the whole agent and
  `last_read_sequence` is the owner's boundary. The unread indicator is `newest_sequence > last_read_sequence`.
* Every page — `GET chat` and `GET chat/history` — carries `rendered_boundary`: the newest `completion_sequence` among the
  assistant entries that page **returns** (after projection and paging), `0` when it returns no completed reply (a user-only,
  tool-only or empty page, or a reply the backend has not indexed yet). To acknowledge, the client posts the `rendered_boundary` of
  what is on screen, **never** `newest_sequence`. A page of an older part of the conversation, or of an older conversation
  (`?conversation=`), therefore cannot acknowledge a reply it did not display, and a reply that completes after the page was
  fetched stays unread.
* `POST chat/read {sequence}` takes a **completed reply's sequence** (the `rendered_boundary` of the page the owner has seen). A
  sequence that is not a completion of this agent is `invalid_read_boundary` (HTTP 422); a boundary behind the saved one changes
  nothing; a reply that arrives while an older boundary is saved stays unread. Only the installation owner can read or
  acknowledge. The saved boundary is one per-agent monotonic position, so acknowledging a later reply also covers the agent's
  earlier ones.
* Replies are captured and indexed by the backend **whether or not Chat was ever opened**. A reply is complete only at the
  provider's own end-of-turn evidence — never narration before a tool call, a tool-only record, an owner echo, a system notice or
  a sidechain.
* `GET /api/root/reply-notifications` / attention items: `{ id: "gchat:<reply id>", kind: "global", chat: true, agent_id,
  agent_name, text (≤180), created_at, unread: true, sequence, nav_only }`. `nav_only` is true for a business agent (it lights the
  navigation dot only; no attention card). Private item threads (`dm:`) and Telegram items are unchanged. Acknowledging an
  attention card is a separate durable preference and never marks Chat read.

## Not in this DTO (unchanged contracts)

Terminal start/restart/kill and agent lifecycle (Terminal tab), Focus, task/Story links, the private-DM history exclusion and
the agent-pane layout.
