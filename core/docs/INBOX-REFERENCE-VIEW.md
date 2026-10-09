# Compact inbox references in Chat

Manager decision D53, 2026-10-09. Accepted inspection 343 found one native
`[Qorqut inbox UUID]` entry in addition to each stored message body. Keep both
immutable entries and all raw/daily bytes. Add a small read-time annotation so
the UI can render the pointer as a reference rather than another Owner bubble.
No migration, parser rewrite, entry deletion, new operation or service.

## Frozen response shape

Add `InboxReference` with two UUID fields, `message_id` and `message_entry_id`.
Add `ConversationItem.inbox_reference: InboxReference | None = None`.
This is a mutable read view, like delivery/handling. Do not add it to Entry,
NativeSource, persisted entries, native idempotency inputs or transcript_line.
Keep conversation count, ordering and cursors unchanged. Older cores omit the
field; newer cores return null when no reference is established.

## Core matching

Enrich only native user entries whose full text is exactly the canonical
51-byte notification_text(UUID), with no trimming or case normalization.
Require a single projected text part for that native item (part_index zero),
not an embedded pointer in a larger text or multi-part item. Find the saved
message and its existing message entry for the same agent/conversation; the
message's recipient must be this agent.

Require at least one submitted delivery attempt for that message with a valid
saved target/hash and matching submitted runner receipt. Reuse the existing
attempt target/receipt validators. Its target must match this agent,
conversation, provider and the source's native UUID. Verify the admitted
archive source/epoch has a run link to that exact attempt's run and generation,
with the corresponding installation/recovery/environment/boot/terminal binding.
Use the persisted link, not just the projection's first run_id: resumed sessions
can legitimately share a source. A conflicted or mismatched source/epoch does
not qualify. No live process, current-session, network or filesystem lookup.

No match, malformed target/receipt, uncertain/deferred attempt or wrong lineage
leaves the annotation null. Do not break history reads or hide an ordinary text
entry when a reference cannot be established. Keep matching bounded to this
page's candidate message IDs using ordinary SQL; no scan of unrelated history.

This proves a reference to a message that has a matching submitted notification.
It does NOT identify which attempt caused the native item, prove automated
authorship, prove handling, or deduplicate text. Manually typing an identical
pointer may produce the same reference; its original native entry remains
inspectable. Multiple matching attempts do not select an alleged causal attempt.

## UI behavior

For a valid core annotation, render a compact neutral disclosure titled
`Inbox reference` at the same timeline position. Expanded content shows the
unchanged pointer and referenced message ID. Keep time/entry identity. Do not
duplicate the message body or invent delivery/handling state, navigation,
network requests or presentation matching rules in the UI.

Missing/malformed annotations use ordinary entry rendering. A native user entry
without a sender actor is labelled `Terminal input`, not `Owner`; native origin
does not prove who typed it. Other sender labels remain unchanged. Preserve
monochrome styling, keyboard accessibility and mobile wrapping.

## Parallel ownership and checks

Core owns conversation/models.py, ledger.py (a small private helper is allowed),
focused owning tests and a concise evidence note. Two grouped tests suffice:
positive/negative matching including resumed lineage, and immutable raw/entry/
daily output across annotation/read/reopen. No native CLI rerun is needed;
accepted archived fixtures and existing synthetic domain fixtures suffice.

UI owns wire.ts, EntryRow and a small disclosure/validator/style helper if
needed, one focused browser test and a concise evidence note. Verify valid,
absent/malformed annotations and native sender labels, desktop/390px wrapping
and keyboard expansion. Synthetic API evidence must be labelled as such.
No broad browser suite or complete native-flow repetition. Manager reviews
both independently, then verifies the paired response contract.
