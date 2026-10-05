# Chat and real Terminal views

Manager decision D38, 2026-10-08. Implement against
[core's Terminal server contract](https://github.com/qorqutbot/qorqut-core/blob/main/docs/TERMINAL-SERVER.md).
The worker handoff pins its exact revision and includes the full protocol.
Core and UI build independently; synthetic browser checks are followed by an
actual compiled-UI/installed-core check after both source reviews.

The owner creates an agent, opens Terminal and starts/configures a CLI there.
Switching views must never start or stop a shell or CLI. This increment makes
the existing recorded onbash shell usable; native startup/inbox connection,
assistant capture and Chat dispatch are subsequent backend work. Preserve honest
disconnected Chat/runtime status and the existing durable drafts and messages.

## Presentation and discovery

Add Chat and Terminal view buttons to the selected agent's center-column header,
with the selected state and accessible names. Remember the choice per agent in
the current client session. Preserve the three-column monochrome layout and
mobile navigation. Keep the inspector's browser/machine surfaces separate.
No provider/model/effort/start selector or automatic CLI command is introduced.

Discover `terminal_get` through the existing schema capabilities. Poll the
selected agent's observation with the same lifecycle/cancellation protections
as the current store. This operation returns a cached core observation, never
starts a terminal, and is also used while Chat is visible for the inspector.
The operation is additive: old backends continue to show Terminal unavailable.
Core supports only reserved onbash in this increment; use its returned state
for other agents, never infer host execution from display name or kind.

The public view contains agent/recovery IDs, nullable terminal ID and timestamp,
`status`, bounded `reason`, nullable `control`, and nullable `recording`. Render
disabled, waiting workspace, unsupported target, stopped and unavailable as
plain status text with a manual Retry action where a fresh read is useful. An
available view can open its stream. Display local recording health accurately;
its byte count is not complete native history or proof of host archival.
Do not expose paths, tokens, fingerprints or architecture details in this flow.

Use `@xterm/xterm==6.0.0` and `@xterm/addon-fit==0.11.0`, with exact package.json
pins and lockfile integrity. Import their assets into the existing Vite build;
no CDN, alternative terminal, URL-opening addon or new application framework.
Use a minimal monochrome theme and the existing monospace fonts. Resize the
renderer to its container, including narrow screens and inspector toggles.
Core permits runtime inline styles for this renderer while keeping script CSP.

Integration clarification 212: publish the unchanged bundled third-party
notices at `public/assets/terminal-notices.txt`, producing
`dist/assets/terminal-notices.txt`. The normal core host serves `/` and
`/assets/*`, so the previous root-level notice file was packaged but not served.
Move the file without changing its contents or other UI assets. Core's generic
static routes and the UI's HTML/JS/CSS remain unchanged. Verify the final notice
through that real host during combined acceptance; its absence is a packaging
defect, not a passing integration limitation.

## Connection and explicit control

When Terminal is selected and core reports available, connect to same-origin
`/api/v1/terminal/<agent UUID>` using the existing HttpOnly session cookie.
Derive `ws:` or `wss:` from the page origin; no credential/query-string tokens.
Opening a viewer must not call ensure/start or acquire control. The first text
message is protocol-1 `hello`, including server-assigned viewer UUID and terminal
binding. Binary messages are terminal output: pass Uint8Array chunks to xterm
without independently decoding split UTF-8. Strictly validate server text
messages and binding IDs before applying state or enabling input.

Initially show **View only** and **Take control**. That button sends the frozen
`control/take` message with the observed epoch. Only its successful server reply
enables keyboard/paste and resize input for this exact viewer. Show **Release
control** while owned. Release/takeover is explicit; view changes, close,
disconnect, logout and inactivity do not release the durable hold. Another
viewer can take control with the current expected epoch. A stale failed take
refreshes state and lets the user decide; do not retry it automatically.

State messages and frame receipts may follow a newer control reply. Within the
same terminal/recovery binding, never regress the accepted control epoch or
last sequence. A newer owner immediately disables local input. A changed
binding, lost session or closed connection invalidates that connection's
callbacks/queued input before any replacement viewer can be established.

Changing agent or returning to Chat disposes the viewer/xterm handlers and closes
its WebSocket, preserving the shell and hold. Chat drafts/cursors stay in the
existing store. On connection loss show a reconnect action; reconnect creates a
new read-only viewer. Never resend an input/resize/control frame, automatically
take over the old viewer, or claim that unacknowledged text was processed.

## Human input and bounded delivery

Only actual DOM keyboard/input/composition/paste interactions inside the terminal
may produce pane-input frames. xterm's output parser can emit `onData` protocol
replies; those are not human input. D38 amendment 198 replaces the original
capture-listener/microtask guard, which rejects real Chrome keystrokes. Use these
explicit public input sources instead, always checking the acknowledged current
control/connection binding before accepting bytes:

- Subscribe to public `onKey({key, domEvent})` and queue its exact `key` once.
  Call `domEvent.preventDefault()` for this handled key so its browser default
  cannot also produce textarea text. Ignore keys while composing or when the
  DOM event is composing. Their later ordinary `onData` event is never forwarded.
- Listen on the terminal element for `input` targeted at public `textarea`.
  Accept nonempty `data` only for standalone `insertText`, with neither an active
  composition nor `event.isComposing`. This covers committed text without an
  `onKey`, including Unicode insertion. Other input types do not grant admission.
  Default-canceled `onKey` events must not duplicate this text path.
- Track composition start with its current control binding. On its matching
  `compositionend`, queue nonempty completed `data` once. Ignore intermediate or
  canceled composition and xterm's later asynchronous `onData`. Clear composition
  state on control/connection loss; regaining control must not revive an old
  composition. Do not deduplicate text by value or use a timing window.
- Intercept the user's `paste` in capture, preventing default and stopping its
  propagation before xterm's normal paste listener. Read only that event's
  `clipboardData`. After the raw size check, call public `terminal.paste(text)`
  synchronously inside a `try/finally`-scoped collector. Collect its synchronous
  `onData` result, clear the collector in `finally`, then validate the complete
  framed size/queue capacity and enqueue it once, or reject the entire paste.
  Do not await or send partial pieces inside the collector. This preserves the
  pinned library's newline/bracketed-paste behavior without an admission window.

Every other `onData` and all `onBinary` emissions are ignored for pane input,
including device-query, focus and mouse reports. No mouse protocol forwarding
is added in this increment. Use only public xterm/DOM APIs; do not inspect private
emitter state, add an escape-string filter or generalize the paste collector into
a DOM-dispatch guard. The [Manager's isolated Chrome probe](evidence/terminal-input-decision.md)
establishes the replacement's observed ordering, not application acceptance or
support for every operating-system IME. Report a contrary browser trace before
changing this contract.

Encode admitted terminal data to exact UTF-8 and strict base64 for `input`.
Preserve xterm's user keyboard/paste framing without additional newline
conversion or implicit Enter. Never read the clipboard automatically. Reject a paste larger
than 64 KiB before accepting any of it and show a concise explanation. Ordinary
control keys and multiline text are deliberate user terminal input; do not apply
the automated Chat-message sanitizer to them.

Use one ordered input/resize sequence per acknowledged control epoch. Send at
most one such frame awaiting its receipt; bound queued UTF-8 input to 64 KiB.
Coalesce unsent resize events to the latest valid dimensions, and assign each
sequence only once when sending. If the input queue would overflow, stop
accepting additional input and show that it was not sent; never silently drop
or automatically replay it. Existing admitted input can finish normally.
Only a controlling viewer sends resize; fitting a read-only renderer changes
its local layout only. Start from the last sequence in the control receipt.

An uncertain receipt, lost connection or rejected frame stops input and clears
unsent input with an explicit warning that earlier input may have arrived.
Require an explicit reconnect/control decision before resuming. A `submitted`
receipt is only transport acceptance, not agent execution. Do not automatically
queue user keystrokes while disconnected or without control.

The terminal output is a bounded live view, not a second archive. Use bounded
xterm scrollback (2,000 lines). Track outstanding bytes queued to `terminal.write`
and decrement in its completion callback; if they exceed 1 MiB, close the viewer
and offer reconnect instead of growing browser memory indefinitely. Core
continues recording and reconnect supplies the current display. Handle server
state/error/close messages without rendering arbitrary exception text as HTML.

## Ownership and checks

UI builder owns a small Terminal component/client, the necessary store/types/
header/inspector changes, CSS, dependency lock and focused browser checks. It
does not change core contracts or introduce durable UI storage/business rules.
Preserve current creation, rename/icon, mutation recovery, login and chat tests.

Extend the existing browser checks with two grouped flows using Playwright's
WebSocket routing and the existing synthetic API: view switching/draft
retention/disabled states and responsive layout; protocol/control/typed input,
Unicode/paste/resize, takeover, stale messages, loss/reconnect and no replay.
Include an output-only terminal device-query sequence while control is owned
and assert that it sends no input. Exercise queue/output limits in these groups,
without creating a redundant per-message test matrix. Keep synthetic evidence
explicit; it cannot prove a real shell, recorder or backend accepts the frames.
Use real Playwright keyboard input for ordinary/lowercase/uppercase/control keys
and Unicode insertion. Exercise composition through Chrome's input protocol and
record that it is not a physical operating-system IME test. Verify a following
key and canceled composition do not duplicate text. Cover both plain and
bracketed multiline paste, including framed-size rejection before any send;
label any DOM-dispatched paste fixture as synthetic.

Run the locked build and owning Chrome checks, inspect desktop and mobile
screenshots directly, and record results/limitations in
`docs/evidence/terminal-ui-r1.md`. Use fresh disposable profiles and synthetic
data, no owner browser or credentials. No external QA service, native/provider
call, usage example or deployment. Commit/push only the assigned branch, stop
edits and receive a fresh independent Astra MAX review before integration.
