# D43 — Exact messages through MCP; short terminal notifications

Manager decision, 2026-10-09. This keeps the owner's manual Terminal session
workflow and single logical inbox owner. Real pinned input observations show
that long pastes collapse before Enter and Claude expands TABs. Messages already
have durable exact bodies. Use that store rather than treating the native
composer as the message database.

## Wire and message lookup

The terminal payload is exactly `[Qorqut inbox <message-uuid>]`, substituting a
canonical lowercase UUID, with no newline or body/label/sender interpolation.
Expose the pure formatter as `messaging.notifications.notification_text(UUID)`.
The attempt records the hash of these ASCII bytes, separately from the stored
message. Sender, recipient, thread, body and handling remain in the message.
A submitted notification means Enter was sent; it is not retrieval or handling.

Extend `MessagesList` with optional `message_id: UUID`. An ID-only read selects
that exact record without an implicit self-agent filter, just like a thread-only
read. Validate the ID exists; explicit agent/thread/cursor/unhandled filters
intersect. Preserve normal paging and recovery rules. No new operation or schema.
Generated instructions for both CLIs say: on this notification, call
`messages_list` with that ID, read the saved sender/thread/body and handling,
then handle the request and explicitly acknowledge when handled. Never treat the
notification itself as the message body or duplicate-work permission. A backend
connection failure remains distinct from an explicit ownership conflict.

New stored messages, including valid UTF-8 control characters, are eligible for
notification; only the fixed ASCII pointer goes to the terminal. Remove the
message-body `unsafe_terminal_text` dispatch rejection for new sends. Preserve
old failed records and immutable old operation results; no automatic resurrection
of historical failures. The low-level runner's text validation stays unchanged.
Messages still have the existing 64-KiB UTF-8 storage bound. Preserve the exact
body in MCP reads, Chat and dated JSONL; do not normalize, trim or rewrite it.

## Native composer observations

Implement two pure functions in `adapters/input_layout.py`:

- `classify_input(provider, version, snapshot) -> ComposerVerdict`, using the
  existing adapter verdict type and runner `TerminalSnapshot` as read-only input.
- `matches_input(provider, version, snapshot, expected_text: str) -> bool`.

These are layout observations, not execution permission. Leave the verdict's
native_verified/permits_paste false and public runtime input_ready false. The
production dispatcher must combine fresh identity, owner, hook, capture and
human-control checks. Do not route the current legacy offline `classify` through
new live permission or change its existing synthetic compatibility behavior.

Use exact supported Claude 2.1.287/Codex 0.160.0 profiles. Refuse unsupported
versions, copy mode, input-off/synchronized panes, malformed dimensions/cursor,
ambiguous composer, modal/busy/draft/unknown layouts. Dimensions may vary in
80–500 columns and 24–250 rows; require exactly the supplied number of captured
lines, compute bottom-relative rows, and require full-width rules where present.
Other dimensions return unknown. Never infer an empty screen from missing rows.

Remove only well-formed SGR and OSC-8 hyperlink delimiters for plain comparison;
retain raw SGR for placeholder/cursor proof. Reject other escape sequences,
unterminated OSC and unexpected controls. Do not interpret hyperlink URLs.
The bottom eight rows are the composer region: busy phrases including
`esc to interrupt`, `ctrl+c to interrupt`, `tab to queue`, `enter to interrupt`
block readiness even when the input is visually empty. Known dialog text from
the current classifier likewise blocks. Earlier output is not a composer.

Claude: prompt row is rows-3; previous and following rows must be exactly
columns horizontal-rule cells. Empty prompt is `❯` + NBSP + the single reverse
video cursor-space, as in original observations, cursor=(2,prompt-row,false).
Allow only observed `auto mode on` or `bypass permissions on` footer with
`(shift+tab to cycle)`, optional ` · ← for agents`; no busy suffix. Expected
nonempty input must be the exact ASCII expected_text followed by the reverse
cursor-space, with cursor x=2+len(expected_text), same hidden cursor. Its footer
may omit the agent hint. No paste-placeholder or multiline acceptance.

Codex: prompt row is rows-4; next row is blank; model/path row has nonempty model,
` · ` and absolute or ~/ path (including observed visual truncation), never an
extra interactive row. Empty prompt uses the exact dim/reset
`Ask Codex to do anything` placeholder and final `  ? for shortcuts` row;
cursor=(2,prompt-row,true). The [observed reset-placement correction](INPUT-PLACEHOLDER-CORRECTION.md)
also permits the exact dim placeholder without its same-row reset only when the
next raw row is empty and the model/footer raw row begins with a full SGR reset
followed by the two existing spaces. All other checks remain required. Expected nonempty input has exactly the ASCII text
after `› `, no dim placeholder styling, a blank final row and cursor immediately
after it. No wrapped/collapsed/multiline text or cursor guess. Do not demand a
specific model name or private path. Expected text must be printable ASCII,
nonempty, single-line and fit in the composer with a spare cursor cell.

`matches_input` is exact visible proof for the transport-owned short ASCII
notification, not proof of arbitrary Unicode/TAB/long payload preservation.
Neither function sleeps, touches processes, changes files or writes input.

## Ownership and validation

This builder owns the formatter, MessagesList/send changes, generated instruction
text, new pure layout module, their existing owning tests and one evidence note.
No host migration, Application assembly, native ownership, runner input gate,
UI, dispatch loop, parser correlation, VM or browser changes. Do not inspect
unfinished parallel builders.

Run focused message/transport tests affected by the filter and control-body
change, preparation preservation checks, and one compact layout group using
sanitized lower-composer fixtures. Check exact ID/intersection/replay, exact
body in publication, both positive layouts, actual draft/busy/modal refusal,
OSC-8 handling, unknown-version/escape/cursor/geometry and exact notification
mismatch. Replay saved original observations privately read-only and report
which original screens match; do not commit native private history or large
fixtures. Do not rerun actual CLIs or accepted workflow suites for these pure
changes. A later normal-server flow must prove actual delivery separately.
