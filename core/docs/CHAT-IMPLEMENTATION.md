# Durable chat and automatic transcript publication

Manager design, 2026-10-08. Implements the first connected M05/M07 chat domain
under D33/D34. This is a plan, not acceptance evidence. Core owns every record
and file below; no UI connection or agent save tool is involved.

## Scope and ownership

One implementation owns host migration 5, application assembly, `messaging/`,
`conversation/`, their focused tests and evidence. It adds real message storage,
offline inbox/handling, conversation reads and dated transcript publication.
It does not paste into terminals, start runtimes, parse native output, implement
browser/file cards, or mark a VM ready. Those integrations remain required.

The separate console assignment owns ASGI sessions/static hosting and the
background worker. Its worker runs the existing typed job queue, including the
new `conversation_publish` handler. Domain tests may pump that queue explicitly;
the combined acceptance must prove publication in a running headless server.

## Message operations

Keep shared frozen Pydantic models and existing `Application.execute` receipts.
All identity comes from `Invocation`; callers cannot supply a sender.

| Operation | Arguments beyond mutation metadata | Result data |
| --- | --- | --- |
| `messages_send` | `recipient_agent_id`; exact nonempty UTF-8 `body` up to 65,536 bytes; optional `thread_id` and `reply_to_message_id` | `message`, `delivery`, `entries` (entry IDs and agent/conversation IDs/sequences) |
| `messages_list` | Optional `agent_id` or `thread_id`; `after_seq=0`, `limit=50` (1–100), optional `recovery_epoch`, `unhandled_only=false` | `items` (message plus delivery and handling), `next_after_seq`, `has_more`, `recovery_epoch` |
| `messages_acknowledge` | One to 100 distinct `message_ids`; optional plain UTF-8 `note` up to 4,000 characters | `items` containing the exact handling receipts |
| `deliveries_get` | `message_id` | `delivery` and `attempts` (empty until dispatch integration) |
| `conversation_list` | `agent_id`; optional `after_seq` or `before_seq` (mutually exclusive); `limit=50` (1–100); optional `recovery_epoch` | Page defined below |

For message listing, an agent actor defaults `agent_id` to its own only when
neither filter is supplied. An explicit thread suppresses that default, so any
registered actor can inspect any existing thread. An owner must supply an
agent or thread filter. Two explicit filters intersect. Agent views
include sent and received messages once each. Positive continuation cursors need
their recovery epoch; a supplied stale epoch fails with `target_changed`.
Validate agent/thread existence. Use a global immutable `message_seq` for this
list; unrelated inserts never disturb a returned cursor.

A thread records the two endpoint actor IDs (the recipient agent has an actor).
Omitting thread/reply starts a new direct thread. A reply derives its existing
thread; an explicitly supplied thread must agree. Sends within a thread must
use its same endpoints in either direction. This is routing integrity, not a
permission tier. Self-messages are allowed and produce one agent entry. Reads
of any existing thread are available to all registered actors.

Message view: `message_id`, `message_seq`, `thread_id`, `reply_to_message_id`,
`sender_actor_id`, nullable `sender_agent_id`, `recipient_agent_id`, exact `body`,
`occurred_at`, `recorded_at`. Allocate UUIDs through the database ID factory;
use the database clock. Messages and their authorship/content never mutate.

Each send atomically stores thread/message, one delivery, event, conversation
entry/entries, publication jobs and its normal operation receipt. For an owner
send append to the recipient conversation. For an agent send append to sender
and recipient conversations (one entry for self-send). `outcome=accepted`
means saved for delivery. It is never a claim of native submission.

Delivery view: `message_id`, `recipient_agent_id`, `state`, nullable `reason`,
`updated_at`. Safe bodies initially remain `pending` with
`reason=runtime_unavailable`. Store every valid body exactly. Bodies violating
D23's terminal control-character rule remain stored but their delivery is
`failed`, `reason=unsafe_terminal_text`; do not create a tmux attempt. A single
fixed `message_delivery` job records the unmet runtime dependency and waits
with `dependency_unavailable`. It performs no terminal effect or automatic start.
Future dispatch will replace that real dependency boundary, not introduce a
second message store. Preserve recovery holds and the pending message.

Within `Application.activate_recovery`'s existing transaction, change unfinished
safe deliveries from pending/deferred to `deferred`, `reason=recovery_review`,
with attributed delivery transition events. Preserve exact messages, handling
and terminal failures; do not turn a known unsafe payload into retryable work.
Any future uncertain attempt must retain its uncertainty, not become ordinary
pending work. No automatic worker releases this hold. This domain hook belongs
to the chat builder's already-owned application assembly/recovery scope.

Handling view: `message_id`, `handled_by`, `handled_at`, `note`. Acknowledgment
records the actual caller, who may intentionally handle another agent's message.
One immutable handling receipt per message: a repeat with the same actor/note
returns it, while a conflicting actor/note returns `invalid_transition`; never
rewrite who handled it. Validate the entire batch before committing. Reading or
replying does not acknowledge anything or change delivery state.

Use concrete tables for threads, messages, deliveries and handling receipts,
foreign keys, identity/cursor uniqueness and appropriate indexes. Append-only
constraints protect permanent message/handling data. Event payloads carry IDs
and bounded state metadata rather than duplicate full bodies. Do not implement
an attempts schema until actual dispatch owns it.

## Conversation ledger and wire page

Store permanent `entry_id`, `agent_id`, `conversation_id`, per-conversation
`conversation_seq`, `kind`, `role`, `direction`, `text`, `sender_actor_id`,
source coordinates and UTC occurrence/recording times. Increment the cursor
inside the insertion transaction; unique `(agent_id, message_id)` makes platform
entry replay exact. Use the registered conversation ID, not a caller override.

This increment emits `kind=message`, `role=user` for owner messages and
`role=agent` for agent messages; `direction` is `incoming` or `outgoing`.
`source` is a typed object with `type=message`, `message_id`, and nullable
`run_id`, `provider`, `source_id`, `source_epoch`, `offset`, `length` (unknown
coordinates are null, never fabricated). Native normalization will extend the
same internal insertion seam with verified archive coordinates and
`kind=native_text`, `role=user|assistant`; it is not a public assistant-write tool.
Do not implement or claim native ingestion/echo verification in this assignment.

`conversation_list` returns `agent_id`, `conversation_id`, `recovery_epoch`,
`items`, nullable `first_seq`/`last_seq`, `has_older`, `has_newer`, and
`publication`. Items are the immutable entries plus separately named `delivery`
and nullable `handling` views. The transcript serializer omits those mutable
views. With no cursor return the latest limit entries in ascending sequence.
`after_seq` returns the earliest entries after that cursor; `before_seq` returns
the latest entries before it, also ascending. A cursor requires its recovery
epoch, including explicit zero; supplied stale epochs fail. Empty pages retain
honest older/newer flags. Reads do not allocate entries or trigger work.

`publication` contains `pending_entries`, `state` (`current`, `pending`,
`blocked`), and nullable bounded `error` code/message. Derive lag by comparing
each day's committed publication sequence against that day's ledger entries.
It is a saved publication observation, not a live filesystem integrity scan.

Equal text always remains separate when message identity differs. Never infer
native echo links, cards, tool effects, provider state or handling from text.
The ledger and full bodies survive provider changes, restarts and UI closure.

## Dated files using existing file receipts

Create `agents/<agent-id>/transcripts/` only under the already-established
agent ownership marker. Validate the marker as workspace code does; walk with
directory descriptors, reject symlinks/special files and recheck parent identity.
Directory creation cannot reseed `persona.md`, notes or any authored file.

Filename: the entry's durable UTC recording date plus `.jsonl`. File format:
one UTF-8 canonical JSON object per line, ending in LF, containing
`format_version=1` and the immutable entry fields above. Unicode, embedded
newlines/control characters and exact text survive JSON encoding. Retain
occurrence times separately. The directory is beside `workspace/`, not inside
it. Onbash uses the identical publisher.

Keep a small `conversation_publications` row per agent/date with committed
through-sequence, hash/length, and nullable pending file-receipt ID and target
sequence. Queue a `conversation_publish` job in each entry's transaction,
carrying agent ID, date and required sequence. No extra scheduler/database.

Use the reviewed `Files.stage/attach/install/finalize` mechanics rather than
writing another file-effect protocol. A publisher snapshots the latest committed
sequence for that date, streams immutable ledger pages through that frozen
sequence into `Files.stage`, and attaches its durable file receipt in a short
transaction along with the pending publication coordinates. `Files.attach`
creates the ordinary `file_install` job. Return retry-delay while installation
is pending; after verified installation/finalization, atomically advance the
publication cursor/hash/length and clear pending coordinates. No file I/O while
a SQL transaction is open. Guard job/recovery/agent coordinates before effects
and before saving progress; hold the existing installation effect gate.
Renew the job lease between bounded pages during long staging; no whole-day
size cap may be used to hide an unrenewed lease or an unbounded memory buffer.

Ownership is established before staging and rechecked before attaching its
receipt and before final publication progress. Match the staged parent
device/inode to the checked transcript directory. Once attached, that explicit
file receipt authorizes installation into that exact previously checked parent;
the ordinary `file_install` handler retains its existing parent/hash/recovery
checks and needs no transcript-specific marker callback. A marker change after
attachment can therefore leave the exact staged bytes installed in the pinned
directory, but blocks new staging and publication-cursor advancement until
ownership is valid again. A replaced parent fails the existing receipt check.
This is the precise effect boundary, not an atomic live-marker or hostile-writer
guarantee. Do not modify generic Files/Jobs semantics for this assignment.

For replacements, the previous committed hash is the expected file hash; for
the first publication require absent or matching content under existing file
receipt rules. An unexpected existing/changed file is a visible conflict,
never something to overwrite or truncate. A pending receipt is reconciled
before staging another partition. A job already covered by a verified newer
publication completes without rewriting it. Multiple queued jobs coalesce to
the latest committed date snapshot.

This release deliberately streams a deterministic complete daily partition
through existing atomic file replacement. It avoids a new append/recovery
engine and partial JSONL lines. It adds write amplification on very busy days;
there is no total-day size ceiling or in-memory whole-day buffer. Measure
before designing incremental publication; retain the same public format.

Normal restart reconciles original-epoch `file_install` and
`conversation_publish` uncertainties from their receipts, without new chat.
Transient filesystem failures use a finite retry delay and expose errors/lag.
An unfamiliar target or ownership conflict stays visibly unresolved; do not
hot-loop. Restore/import recovery holds remain held, including stale jobs that
share a familiar path. Known orphans remain under existing Files semantics.

## Checks and completion boundary

Use a compact set of behavior tests covering transactional rollback/idempotent
replay and exact bodies; thread/actor/handling correctness; cursor paging and
recovery fencing; two equal-text entries; both agents and onbash; UTC midnight
and late occurrence; file install/receipt/publisher crash boundaries; ownership
and unexpected-file refusal; transient failure followed by restart/recovery
without another message. Reuse foundation file fault hooks rather than rerun
its entire matrix. Verify HTTP/MCP discovery and one shared send/list/ack flow.

Report exact commands/results and source versus installed-package coverage.
Native parsing, terminal delivery, browser/file cards, exports and whole G3/G4
remain outstanding. Completion of this assignment is a real offline chat
domain and publisher, not a claim that an assistant has run or replied.
