# D47 — File-reference cards in Chat

Manager implementation plan, 2026-10-09. Consume the accepted core D45 contract
at `c13eb8b2dbf023a69e84cde2eda0368058485e6f` (`files/models.py`,
`conversation/models.py` and `docs/FILE-REFERENCES.md`). Base UI is
`65cfb95a3974bf3310f82b3e3914ca4601cc409a`. No backend change or file-opening API.

## Typed content and rendering

Add the exact FileReference wire type and file_reference source union. Render a
card only when entry.kind and source.type are both file_reference and the saved
reference has the expected shape. Use one small type guard for this content:
UUID identities, state/location literals, string path/requested_path/label/note,
finite nonnegative integer size_bytes, decimal-string mtime_ns, nullable sha256
required to be null and UTC observed/recorded timestamps. Require reference
agent/conversation/actor and label/timestamps to agree with their entry. Keep
UTF-8 path/label/note bounds aligned with D45 without normalizing or truncating
saved text; Python character limits correspond to Unicode code points in JS.
Historical references may have an older recovery epoch: never compare them to
the current Store epoch or claim they are invalid because the server recovered.
Unknown/malformed sources use the existing unsupported-content presentation,
without crashing a page or presenting an actionable card. Ordinary message or
native text mentioning a path/tool name remains ordinary text.

Within the existing EntryRow, show a compact monochrome outlined file card with
saved label, a small file glyph, full selectable normalized path and location
label (Agent workspace / Host path), optional escaped note, recorded size and
observation time. Use the existing sender and entry timestamp header; retain
recorded-versus-occurred distinction without duplicating it needlessly. All
metadata describes that past observation; do not say the file currently exists
or that contents were saved. Size is an exact locale-formatted byte count only
when Number.isSafeInteger; otherwise say Size exceeds precise display range,
so JS rounding is not presented as an exact count. Keep the decimal mtime as a
string in the wire record; no nanosecond Number conversion is needed in this UI.

Provide a keyboard-accessible Copy path button using navigator.clipboard.writeText
with the exact saved path. Local per-card feedback reports Copied or a concise
failure with manual-selection guidance. No persistent state, global toast queue,
file fetch, file:// link, filesystem join, preview/download/open button or new
composer action. The owner deferred opening/preview. The card itself remains
readable content, not a misleading disabled launch control.

Fit the existing thin-line black/white UI. Wrap long paths/labels/notes, preserve
significant whitespace in path/note and keep the complete path available without
horizontal page overflow on desktop or a 390px phone. Use escaped Preact text,
no HTML interpretation, dependencies, icon package or visual redesign.

## State and checks

Reuse Store conversation pagination, entry IDs, refresh and replay deduplication
unchanged. No polling, cursor, mutation, Terminal or runtime-inspector changes.
Own only wire/guard, card component and EntryRow wiring/small CSS, one synthetic
fixture/test extension and docs/evidence/file-reference-cards-r1.md.

Add one focused Chrome group using the existing synthetic server. Include an
owner and agent reference, a historical epoch, Unicode/long/HTML-shaped content,
a malformed reference and an ordinary path-looking message. Check copy success
and clipboard rejection/absence with a local clipboard stub; exact path bytes,
no file-content/network requests, no inferred card and repeat refresh yields one
card per entry ID. Inspect desktop/mobile screenshots, keyboard focus and page
errors/overflow. Include unsafe-integer size presentation within the same group.
Run the locked build, this group and the existing conversation/scrolling group
that shares EntryRow. Do not duplicate suites or rerun all Terminal tests.

These are presentation checks against the frozen schema. Manager will perform
an actual compiled-UI/core/MCP reference-publication flow after acceptance.
Commit/push a clean candidate, preserving original evidence and naming exact
commands, screenshots and limits; no merge/deployment or native model calls.
