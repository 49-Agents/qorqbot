# Inbox reference presentation

D53 is frozen in [core's plan](https://github.com/49-Agents/qorqbot/blob/b07604cc52486581da5f55c3e67eb07f14823897/core/docs/INBOX-REFERENCE-VIEW.md).

The UI and core candidates passed [Manager review and the paired response check](../../core/docs/evidence/inbox-reference-manager-acceptance.md)
on 2026-10-09. The focused Chrome check used a synthetic API; this adds no
native-provider or deployment acceptance.

Conversation items may add `inbox_reference: {message_id: UUID, message_entry_id: UUID} | null`.
Older cores omit it. It is a core-derived read view; UI must not infer it by
parsing text, joining messages or interpreting delivery attempts.

For a well-formed annotation on a native user entry, retain the timeline entry
and show a compact neutral `Inbox reference` disclosure. Expand to see the exact
pointer text and message ID. Keep its timestamp/identity; do not invent a causal
attempt, sender, delivery/handling state or navigation. Missing/malformed data
falls back to the full ordinary entry. No hidden or removed history.

Native user entries with no sender actor use `Terminal input`, since the origin
does not establish who typed them. All other sender labels stay unchanged.
Preserve monochrome styling, keyboard interaction, wrapping and pagination.

Own wire.ts, EntryRow and a small helper/style only as needed. One focused
browser scenario covers annotated/ordinary/malformed entries and native sender
labels, keyboard expansion and a 390px viewport. Label synthetic API evidence.
No broad suite, native CLI run, new dependency or service. Core and UI builders
work independently against this fixed optional field; Manager checks the pair.
