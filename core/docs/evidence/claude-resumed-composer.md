# Resumed Claude composer (visible cursor, replayed history)

2026-10-14; base `560cd6dd5ea13057b78ca4b0ca17b79490b7a06c`, branch `codex/fix-resumed-composer-a147d77a`.
Manager decision, implemented by a Worker. Pure pinned Claude Code 2.1.287 layout correction in
`adapters/input_layout.py`; Codex, runtime, hooks, ownership, capture, collector, terminal and UI are unchanged.

## Failure

The real two-agent QA reconnected both exact Claude native sessions (resumed after `/exit`), yet their stored
messages stayed queued. Both exact current panes classified `unknown` /
`ambiguous_or_misplaced_composer`. Read-only captures, 106×52, flags false, cursor `(2, 49, visible)`:
rules at rows 48/50, row 49 `ESC[39m❯NBSP` with **no** inverted cell, the known mode footer at row 51 (the last row).
The resumed pane replays the earlier styled `❯ /exit` prompt at row 45 (A) / 44 (B) just above the box, which the
window-wide prompt count read as a second composer. Once that is fixed, the old code would still have refused the
real cursor, because every Claude form expected a hidden cursor and an inverted cell. tmux `alternate_on=1` is
context only; `TerminalSnapshot` has no such field and none was added.

## Manager decision

1. Keep version/geometry/row-count, ANSI/control validation, exact prompt prefix, rules/footer/known footer hint,
   busy/dialog exclusions and every ownership/capture/human-control barrier.
2. The current Claude composer is the cursor row with both adjacent full-width rules and the known footer. Only the
   box and its suffix may hold competing prompt rows; output above the upper rule (`/exit`, quoted prompts, user
   text) is history. The local above-box busy/dialog scan from `row - 5` is unchanged. An arbitrary last prompt
   without cursor, rules and footer is not accepted.
3. Keep the hidden-cursor inverted-cell and dim-suggestion forms. Add the visible-cursor form: an empty composer has
   an exactly empty tail with the cursor at `(2, row)` visible; a single-line short-ASCII expected pointer has an
   unstyled tail exactly equal to it with the cursor at `(2 + len, row)` visible. Each tail form is bound to its own
   cursor presentation, never a loose OR; no arbitrary SGR/whitespace; no typed text as empty; no suggestion as input.
4. Confirm the filled renderer with one bounded non-submitting probe before allowing the filled form; ask if it
   differs from both decided forms.
5. The classifier still grants no paste permission (`native_verified` and `permits_paste` stay false). The existing
   attention path clears automatically through the known-composer verdict.

## Observed probe (disposable fixture only)

Fixture agent A (`61631f72…`), http://127.0.0.1:35611, fresh private Chrome profile + Playwright under the evidence
folder. The pane's first capture was byte-identical to the Manager's `capture-A.ansi`
(SHA-256 `c106107061c3abf236838d62a153ef809cdb58bd2c861c6d324fd8ee672d6a2f`): idle empty composer, cursor `(2, 49, visible)`.
Agent B was never selected or read. The pane was 52 rows before the UI attached and 55 rows (106 columns) at the next
capture, after Terminal was opened and the text typed: the UI fits the shared pane to its viewport.
Focusing the xterm helper textarea (no pointer event) and typing exactly `QORQBOT-CURSOR-PROBE`, no Enter, gave:

| | row | cursor |
|---|---|---|
| prompt row | `ESC[39m❯NBSP` + `QORQBOT-CURSOR-PROBE` — no SGR in the tail, no trailing reset, no inverted cell | `(22, 52, visible)` |
| footer | `ESC[39m  ESC[38;5;220m⏵⏵ auto mode onESC[38;5;246m (shift+tab to cycle)ESC[39m` (no `← for agents` while typing) | |

That is exactly the decided filled form: an unstyled tail equal to the text, cursor at `2 + len` visible. No third
form was needed, and the existing hidden/inverted-cell form is not what the resumed pane draws. Twenty ordinary
Backspace keys then restored the empty row (`ESC[39m❯NBSP`, cursor `(2, 52, visible)`, footer with `← for agents` again); Chat was selected again and only the private
Chrome was closed. No Enter, Ctrl+C, model turn, tmux input, message, acknowledgement or service change occurred.
The pane stayed 55 rows after detaching. Captures and screenshots are private under the evidence folder.

## Change

- Prompt-row uniqueness for Claude counts from the cursor row to the end (box and suffix), not from `row - 5`;
  Codex keeps its whole window. The busy/dialog scan is untouched.
- The Claude tail is `text + inverted cell` (or, for an empty check, a known suggestion) with a hidden cursor, **or**
  exactly `text` with a visible cursor; the existing final cursor comparison then binds each form to its own cursor.
- Assertions whose history-exclusion meaning changed: a prompt-looking row above the upper rule, even inside the
  local window, is history (the three "prompt at window start / above box / beside rule" refusals, the shared
  `-8`/`-5` ambiguity rows for Claude, and the dim-suggestion test's row-48 second prompt, which now sits after the
  footer). Prompts in the box or after the footer, busy/dialog words, malformed rules and footers still refuse.
- Fixture `claude-resumed-composers.json`: three observed, sanitized snapshots (empty A, empty B, probe typed).
  History above the retained styled `/exit` prompt is synthetic; no native IDs, paths or transcript text remain.
- A shared geometry test now also covers a constructed visible-cursor variant at 80–500 columns; the new
  `test_resumed_claude_box_has_a_visible_cursor_and_never_proves_a_paste_by_itself` covers observed A/B, the probe,
  wrong cursor/text/style/multiline/suggestion refusals, typed drafts, and quoted history prompts.

## Checks

Private venvs from `requirements.lock` (Python 3.12.3); `core/`; `PYTHONPATH=src:tests`:
`python -m unittest adapters.test_adapters.InputLayoutTests` passes **20 tests** (19 earlier + 1 new). Installed wheel
(`qorqut_core-0.1.0-py3-none-any.whl`, SHA-256 `99e11e8ff10f8c29ea178fc052add69e8dc3d449ec7fb43a48654c3db8cfc665`,
installed `input_layout.py` SHA-256 `ccb51211cf9c39eda9a171715989432b6096d1b6ae3f438cd2996928227bf6fa`, equal to
source) from a fresh venv with `PYTHONPATH=tests`: **20 tests** pass. As a bounded regression, `runtime.test_native_attention`
and `runtime.test_input_dispatch` (33 tests) also pass on the source tree. Eleven single-rule weakenings (old window
count, whole-screen count, ignored cursor visibility, loose visibility, trimmed or SGR-tolerant tails, prefix match,
suggestion or cell with the wrong cursor, empty tail for expected text) each fail the group.

Read-only installed check (`python -I`, captures opened read-only, hashes and mtimes unchanged): both original
captures and the probe's before/cleared/after captures classify `empty` /
`native_snapshot_empty_layout_live_unverified` with `native_verified=false`, `permits_paste=false`, attention `none`;
the typed probe is `blocked` (a draft), `matches_input` true only for `QORQBOT-CURSOR-PROBE`. The base package
(`input_layout.py` SHA-256 `ec92c310…`) returns `unknown` / `ambiguous_or_misplaced_composer` for all six. **Constructed**
variants of A and B (the exact pointer with a visible cursor, one column off, hidden, trailing space, the old cell form,
a draft, the cursor on the history prompt, a dialog word) match or refuse exactly as the decision requires.

## Limits

Observed: both original panes and the UI-typed probe. Constructed: every pointer, draft and variant of A/B, and the
80–500-column visible-cursor matrix. None of these is live paste evidence; no Chat delivery, Enter, native receipt or
handling is claimed, and the candidate was not installed in any fixture or owner instance. Only this one printable-ASCII,
single-row visible-cursor shape is accepted; other renderers, versions or locales refuse until observed. Trailing-space
text would be trimmed by tmux in a visible-cursor capture and so refuses. The browser UI fit changed the fixture pane
geometry (52 → 55 rows). The full repository suite was not run. This note amends the prompt-count sentence of the
[composer note's local-window rule](../CHAT-COMPOSER-COMPATIBILITY.md#2026-10-14-amendment--claudes-floating-input-box).
