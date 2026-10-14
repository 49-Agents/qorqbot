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

## 2026-10-14 amendment — Claude's floating input box

Live QA with pinned Claude Code 2.1.287 on Sonnet 5.5 (106 columns) showed the
real model replying to Terminal input while every Chat readiness message stayed
deferred with no delivery attempt. The classifier rejected the actual box for two
independent reasons, each confirmed offline on read-only snapshots (no input sent):

- Claude draws its box where its cursor is. Under short output it floats high on
  a 52-row pane; after a turn it sits above blank bottom margin rows (24 rows,
  box at 17–19, footer 20, three blank rows). The old `rows - 3` row
  rejected both. This amends the bottom-only Claude rule in
  [INBOX-NOTIFICATIONS](INBOX-NOTIFICATIONS.md); Codex keeps its fixed
  `rows - 4` position.
- On the first turn the empty box shows a styled suggestion, not the empty
  cursor cell: `Try "fix lint errors"`.

Both changes stay in the pure pinned Claude 2.1.287 layout in `adapters/input_layout.py`.
The profile still returns `native_verified=false` and `permits_paste=false`.

**Box location.** The Claude composer row is the validated `snapshot.cursor_y`.
Rows `row-1` and `row+1` must be the existing full-width rule cells and `row+2`
the existing known footer (the `auto mode on` / `bypass permissions on` text, optional
`← for agents`, no busy suffix). Every row after the footer must be blank once the
supported SGR and OSC-8 delimiters are removed; any visible extra status, footer or
input row refuses. A box without a row above or two rows below refuses.

**Local window.** Busy/dialog phrases and prompt-looking rows are examined from
`max(0, row - 5)` to the end of the screen (identical to the old last-eight rows when
the box is at the bottom). The selected row must be the only prompt-looking row there;
a busy or dialog phrase anywhere in the window refuses. Older output outside it, including a
quoted prompt or a whole stale box, is history and never becomes the active composer.
Row count, geometry, flags, escape handling, version, hidden cursor at `(2, row)`
and the exact full rules are unchanged.

**Suggestion.** Only for the empty check (`expected_text=None`) the tail may also be
exactly the observed shape: inverted literal `T` (`SGR 7`), dim `ry` (`SGR 0;2`) and
reset, one plain space, then one or more dim words (`SGR 2`, each ended by `SGR 0`)
separated by single plain spaces. The visible text must be `Try "…"` with a
nonempty body that neither begins nor ends with a space nor contains another quote,
use printable ASCII words and fit one row (`2 + length <= columns`). The words may
vary; unstyled, differently styled, concealed, extra, wrapped or non-ASCII text, a
second cursor cell and any other cursor position still refuse. A suggestion proves an
empty box only: `matches_input` still requires the exact printable ASCII text plus the
inverted-space cursor cell at `2 + length`, now on the cursor-relative row, so a
placeholder can never prove that a notification was pasted.

Existing first-SessionStart awaiting-primary capture, the 51-byte inbox pointer and
the whole-runtime dispatch gates are unchanged. No runtime, hook, ownership, queue,
retry, UI or Codex change. Existing deferred messages dispatch through the normal path
once actual input proof succeeds; do not paste or resend them manually. Sanitized
observed rows are in `tests/adapters/fixtures/claude-floating-composers.json`; see
[the bounded evidence](evidence/claude-live-composer-qa.md).
