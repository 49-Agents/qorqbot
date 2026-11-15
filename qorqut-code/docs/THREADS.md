# Threads

Ordubashy's pane has a permanent Threads tab immediately before Focus. Focus keeps its saved ordering, hierarchy, and tools. Threads lists existing private owner–Ordubashy record conversations, including closed tasks and records absent from Focus. The former Today task filter and its API arguments are removed. The calendar excerpt is independent.

`owner_conversations_list` is an owner-only, Global, read-only directory. It orders by the sequence of each channel's latest actual owner/coordinator message. Source edits, related work, reads, snoozes and Focus changes cannot move a thread. Pages use `before` message sequences. Sources are authorized on every read; inaccessible sources and retired peer channels are omitted. An empty page may have a continuation when all scanned sources are inaccessible. Listing never acknowledges messages.

Selecting a compact navigation row opens the existing private conversation in the left chat area. The right list contains only the title, latest-message preview, timestamp and unread dot; selection never expands a second detail list. On narrow layouts the conversation appears within the pane. Drafts use the same source-keyed conversation state as Focus. A successful send refreshes the directory; background reads run at the pane polling interval.

A Project (an existing `businesses` row, keyed by its UUID) is a supported private source and appears here only after
its first real owner/coordinator message. Its canonical source names the same UUID in `id` and `business_id`; it
carries the real Project name, has no team-note source, and its `owner_conversation_related` page uses the same
bounded, guarded Project inventory identity with an independent `offset`/`limit` page (see
[Project records](PROJECT-RECORDS.md))
instead of stored concern links.

`owner_conversation_related` is an independent owner-only read with `offset`/`limit` pagination. It projects stored concerns links in either direction, direct child tasks, action evidence links, signoffs targeting the task, and references attached to the record's Focus cards. Every candidate uses the existing source's read authorization. Repeated links to the same scoped record collapse to one entry; canonical Action IDs also deduplicate bare relationship IDs. Relationships grant no access.

Related records render through the same typed-reference components as main Ordubashy Chat: task chips with an Open action, existing inline signoff cards, and ordinary reference links for other records. These are visual entries, never inserted conversation messages, never included in unread/message counts, and never used to sort the directory. Related pagination is independent of older messages. References on real messages stay attached to those messages; related and message-referenced signoffs deduplicate by scoped identity.

WhatsApp/email signoff cards in native chat expose the existing private comment composer while details remain collapsed. Commenting uses the source conversation route and does not approve, reject, or send the external message.

Focused coverage: `app/tests/test_thread_views.py`, dashboard `tests/test_threads_browser.js`, and `tests/test_chat_references_browser.js`, using synthetic databases and local fixture HTTP servers only.

## Validation on 2026-10-27

- Five composed-host thread tests passed.
- The Threads browser scenario passed, including navigation, paging, message-only sorting, related paging across refresh, open-record dialogs, shared drafts, send, revoked-access removal and narrow layouts.
- All 18 native-chat reference/signoff checks passed, including WhatsApp/email private comments and unchanged decision behavior.
- JavaScript syntax and Git whitespace checks passed.
- The older Outreach workspace test assumes `campaign_review` is built into core, but its fixture loads no extensions. It fails with `Unknown workspace type` before its workspace-list assertion; that unrelated fixture remains unchanged. The new host tests verify the core Threads registration directly.

## Independent Focus details and threads

In Focus, opening a Project or another record opens its details on the right without replacing the current Chat. **Open thread** selects its conversation on the left. **Open project** (or **Open details**) in the thread header opens the record on the right. Closing the right-hand record leaves the thread and its draft in place; **Chat** in the thread header returns to the main conversation without closing the record.

The pin button in either header saves that thread to the Focus row. Opening a pinned thread changes only the left Chat. Pins use the existing owner thread-pin preferences, and normal conversation access checks still apply. A pending send or unsaved inline signoff prevents leaving its thread, including leaving Focus for another view or agent, and says why; message drafts remain keyed by source when switching conversations. **Chat** in the thread header always returns to the main conversation, even after the Focus view that opened the thread has gone. A thread that becomes unavailable leaves the Chat area whether or not its record is open on the right. A Focus without a Chat area beside it (a permanent worker's) opens a pinned thread as its record, Chat first.

## Reports and pins by Ordubashy

A report (a read-and-acknowledge Signoff) has no conversation of its own: it belongs to a Task (`report_task`), and one Task can hold many reports. Opening a report in Focus shows it on the right and opens its Task's thread in the Chat area. **Open thread** and the pin button on a report act on that Task's thread, and the report has no Chat tab or comment box. An older report with no Task has no thread. Daily reports all belong to Ordubashy's standing "Daily report" Task.

Ordubashy can list, pin and unpin threads in the owner's pinned-threads row (`thread_pins_list`, `thread_pins_set`), only when the owner asks. Only the owner and the configured coordinator in person may call them. A pin needs a record the caller may read, and is stored under the record's canonical identity in the owner's own pin file. A change emits the `thread_pins` change kind, and the dashboard rereads the row.

## Shared Chat presentation

Main Chat, Threads and Focus task conversations use the same message/reference/composer components. `sgChatMount` mounts the existing verified signoff view, including full WhatsApp/email content, decisions and private comments. A signoff shown inside its own thread omits its nested conversation so that its channel has one composer. Other related signoffs retain their own correctly scoped comments. The custom thread-preview renderer, its fetching layer and message-style overrides have been removed.

Threads and Focus use one title/Details/close header. Details opens the existing record reader over the thread and preserves selection and drafts. Focus-specific context is in a collapsed disclosure; it no longer reserves a fixed share of chat height. The right-side Threads list remains a compact navigation list.

Nested signoff conversations are disposed when their host is removed, while source-keyed drafts survive remounts. Denied signoff reads clear private content and invalidate older pending responses. New explicit refreshes can recover access; late responses after close cannot recreate cards.

The targeted ACLA implementation was reviewed by Manager, including reproduction and correction of nested-controller disposal and out-of-order read failures. Focused browser checks pass on both versions for shared Focus/Threads rendering and drafts, source/related signoff comments, canonical card content, Details retention, async denial/disposal races and narrow layout. Existing native Chat reference and signoff tests also pass. No data or backend migration is needed.
