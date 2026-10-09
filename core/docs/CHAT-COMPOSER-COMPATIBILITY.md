# D58 — Recognize the owner's ordinary Codex input layout

2026-10-09. The owner reported that Chat messages never reach the running bot.
Read-only inspection confirmed four stored, deferred messages with
`composer_unknown`. The connected Codex 0.160.0 pane is 69 columns by 68 rows,
with its exact empty placeholder/cursor and a normal `2 warnings · f2 to view`
footer. Existing classification rejects both the width and that footer.
Offline diagnosis independently removed each cause; no live input was sent.

Keep this correction in the pinned Codex input-layout adapter. Accept widths
54–500 for Codex, retaining the existing 80-column minimum for Claude and all
row/cursor/mode checks. The existing inbox pointer is 51 ASCII characters;
54 columns leave the two-column prompt and one spare cell. Preserve the exact
visible post-paste check and refuse wrapping rather than changing the pointer.

Codex's final status row may contain the existing left shortcuts hint while
empty, plus the right-aligned noninteractive warning indicator. Support the
observed warning glyph, a positive decimal count, `warning`/`warnings`, middle
dot, and `f2 to view`, separated/padded by spaces. With expected pasted text,
allow the same warning indicator alongside the existing empty left footer.
Continue rejecting unknown footer text, extra rows, setup dialogs, busy states,
drafts and mismatched cursor/placeholder/pasted text. Retain existing SGR parsing.
This does not make an idle hook or the public input_ready field an input permit.

No changes to session ownership, queue contents, terminal control, message IDs,
delivery attempts, retry/uncertainty semantics, native capture, UI or auth.
No CLI upgrade or restart. Existing deferred messages use normal dispatch once
their actual native input proof succeeds; do not manually paste or resend them.

Extend the owning InputLayoutTests group with sanitized versions of this layout
and the boundary cases. Check the installed candidate against the saved actual
snapshot read-only. Reuse the existing disposable pinned Codex/local canned
provider flow for one narrow-terminal message, exact notification submission,
native receipt and dated history. Never use the owner's session or credentials
as a fixture. No broad suite or additional review; Manager merges and deploys
under the owner's standing authorization, preserving the live native process.
