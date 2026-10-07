# Preserve the recorded terminal's display dimensions

Manager decision, integration finding 217, 2026-10-08. The original D38 UI
fits a read-only emulator to a narrow container while the real pane remains
wide. The mobile screenshot visibly loses row suffixes. A new tmux mirror also
starts without an explicit PTY size, which cannot guarantee the complete view
of a non-default pane. Correct both boundaries before Terminal acceptance.

The native pane owns the terminal grid. A read-only browser renders that same
grid and scrolls its viewport. Only an acknowledged controlling viewer may ask
to resize the pane. Local display sizing never acquires control or changes the
durable input sequence. Keep fonts readable; do not shrink or reflow the native
screen to conceal overflow.

This supersedes D38's read-only local fitting rule. Core and UI implement the
following frozen contract in parallel, receive direct Manager review/testing
under the latest owner instruction, then repeat the combined real-browser flow. The notice
relocation remains independently accepted and must be included in the final UI.
No native CLI, inbox/Chat connection, VM or browser-MCP behavior is added.

## Shared observation and stream contract

Add a small immutable `ShellDimensions` model in `runner/shell_models.py`:
`columns` and `rows` are strict integers with the existing resize bounds
(20–500 columns, 5–250 rows). Add nullable `dimensions`, default null, to
`ShellView` and public `TerminalView`. An available public view always has
verified dimensions. Unavailable observations never authorize display/input
effects. No dimensions are fabricated from a browser viewport or saved CLI
configuration. Keep schema 6, five handlers, sixteen operations and all existing
input/control/receipt fields and limits unchanged.

Terminal WebSocket **protocol 2** replaces protocol 1 for the corrected pair.
`hello.protocol` becomes 2; its Terminal view includes `dimensions`. Add one
server-to-client message: `{"type":"size","columns":N,"rows":N}`. It describes
the dimensions of this mirror's ensuing byte stream and carries no input
authority. The connection already binds agent/terminal/viewer identity. All
client frames and existing state/control/frame/error messages keep their shapes
apart from the added public view field. Do not add a browser resize request that
bypasses human control.

The serialized writer sends a size message before sending bytes obtained under
a changed mirror size. `hello` supplies the initial size. Periodic observations
remain at least once per second, including when admitted effects wait on the
runner lock. A busy stream read still skips without blocking that writer.
HTTP/state observations may arrive late; they must not resize the live emulator.
Only the ordered hello/size/binary stream drives its display dimensions.

Core/UI are not deployed yet. Pair these changed artifacts for acceptance; the
old strict protocol-1 UI cannot be used with this protocol-2 server. The new UI
rejects an unsupported hello without enabling input and displays a concise
unavailable/reconnect message. Existing fallback for a backend without
`terminal_get` remains. No compatibility server, duplicate endpoint or migration
is needed for these development-only artifacts.

## Core lane

Read the pane dimensions during `ShellManager`'s existing verified, input-locked
inspection. A narrow formatted tmux observation includes server/session/window/
pane/process identity, live state and width/height. Compare those identities to
the accepted binding; retain the existing before/after target checks and bounded
dimensions. Invalid observations yield the existing unknown/unavailable result.
Use this data in `ShellView`, then `Terminals._presentation`; no process or tmux
work enters a host SQL transaction. Construction and `terminal_get` stay passive.

Before starting a `ShellAttachment`'s physically read-only tmux client, set its
new private PTY to the verified pane dimensions. Preserve `-E -r`, raw mode and
the manual tmux window-size authority. Verify that the attached client is both
read-only and ignored for window-size selection; do not silently introduce a
different attach mode if the installed tmux contradicts this behavior.

Add `ShellAttachment.set_display_size(columns, rows)` for trusted internal
mirror maintenance. It validates the same bounds and, under the attachment's
I/O lock, changes only the owned mirror PTY's winsize when needed. Expose its
current dimensions as immutable values. It sends no PTY input, performs no
tmux resize-window command, changes no pane/control/sequence row, and works for
read-only attachments. Reject a closed attachment. The local FD is already
owned; this path must not wait on the runner input flock or run a process query.
Notify only its exact unreaped `Popen` tmux client with `SIGWINCH` after a changed
PTY size so the client notices the new dimensions even without a controlling
terminal. Do not signal the pane, recorder, Bash or a process group. Keep
file-descriptor/child shutdown serialization intact.

The existing authorized pane-resize path retains its sequence/effect semantics
and mirrors the resulting size on its own attachment. After a Terminal stream's
resize settles, inspect/publish the actual pane observation while retaining the
existing target/effect guard. This prevents its next read from applying an older
cached pane size. An unverified outcome must remain unverified; no automatic
resize retry or invented successful dimension is allowed.

Extend the private `TerminalStream.read` result to
`(bytes, closed, dimensions_or_none)`. Under its existing nonblocking stream
lock, require the current available target observation, synchronize this
attachment's display size to those verified dimensions, then read its bytes.
The returned size describes that same read. A skipped busy read returns no size
and no data. The single writer compares against its last announced size and
sends the size message before that read's binary data. It must not infer the
size later after another effect could change it. A size announcement is useful
even if the read has no bytes. Preserve bounded output, cookie/target checks,
stop/reply coordination and all four accepted review-202 corrections.

This is a live terminal view, with normal PTY resize/redraw behavior. It is not
an atomic screen snapshot or a historical screen archive. Recorder bytes and
shell/control identities remain unchanged by viewer fitting/scrolling.

Core owns only runner `shell.py`/`shell_models.py`, runtime `terminals.py`/
`terminal_models.py`, the necessary writer change in `transports/server.py`,
focused existing shell/Terminal tests and `docs/evidence/terminal-geometry-core-r1.md`.
No database/dependency/native-recorder/adapters/messaging/ownership changes.

Extend existing owning groups: an 80x24 initial pane; a non-default wide/tall
pane observed accurately; a second read-only mirror whose actual PTY matches
that grid; local mirror resizing leaves pane dimensions and control/sequence
unchanged; both viewer mirrors follow a subsequent authorized resize; protocol-2
hello/size-before-data ordering, malformed/out-of-bound geometry refusal and
unavailable/stale target closure. Include a tail marker beyond column 80 and
content below row 24 in real mirror evidence. Keep the real runner-lock/state
cadence and cancellation regressions. Use the installed normal console and
positively owned tmux/Bash/recorder; no broad duplicated matrix or new daemon.

## UI lane

Update wire models/strict validation for the added dimensions, protocol-2 hello
and size message. Available views require valid dimensions. Preserve all
control-epoch/sequence, uncertainty, input-source and no-replay behavior.

Use the existing terminal container as a bounded scroll viewport. Give the
xterm grid its native intrinsic width/height, with horizontal and vertical
scrolling confined to that viewport. The header, buttons and note stay visible;
the page itself must not overflow. Retain the existing readable font and 2,000
line scrollback. Native scrollbars/keyboard access suffice; no new toolbar,
zoom system, fake wrapping or provider control is requested.

Remove unconditional `FitAddon.fit()` and the emulator-onResize-to-remote-resize
feedback path. Use the public `FitAddon.proposeDimensions()` against the bounded
viewport to request a pane resize only after acknowledged ownership and on real
container size changes while owned. Clamp to the existing protocol bounds and
coalesce/deduplicate requests through the existing sequence queue. Do not send
resize from read-only viewing, scrolling or an incoming display-size update.
Taking control requests the available viewport size; losing/releasing control
retains the last authoritative grid and permits scrolling. Local render fitting
does not change durable control.

Apply hello/size and binary output in their WebSocket order. xterm writes are
asynchronous, so do not resize it ahead of previously received unparsed bytes.
Use a small serial display FIFO: one `terminal.write` at a time, advance from
its callback, and apply a queued size synchronously between byte entries using
the public `terminal.resize`. Keep the existing 1 MiB pending-byte limit and
cap pending display entries at 1,024; overflow closes only the viewer with the
existing clear warning. Coalesce adjacent size entries without intervening
bytes. Disposal/control changes must not revive stale callbacks or send input.
Control/session messages disable input immediately and do not wait for this
render queue. Use public xterm/DOM APIs only.

UI owns wire/protocol/client/component/CSS changes, the existing terminal browser
groups and their shared synthetic peer/fixtures, plus
`docs/evidence/terminal-geometry-ui-r1.md`. Preserve dependency pins, the notice
asset, unrelated application behavior and the original six nonterminal check
intents. Do not add a second browser framework or a large new suite.

Extend the two terminal groups: a wide/tall read-only grid on a narrow viewport
with actual user scrolling revealing the far-right and bottom markers, no
input/control/resize frames, unchanged font and no page overflow; ordered binary/
size/binary rendering; controlled fitting, takeover, release and geometry
messages without resize feedback; unsupported/invalid geometry; delayed render
callback disposal and the retained bounds. Preserve actual keyboard, Unicode,
paste/composition, device-query separation and uncertainty checks. Run the locked
build and these existing Chrome groups, and inspect desktop/mobile screenshots.
The peer remains synthetic evidence; the separate real-core flow follows.

## Combined acceptance and handoff

The original integration actor keeps its completed original-pair evidence and
records finding 217 as a failure. It does not modify either product or rerun
the same failing pair. After both corrected candidates have Manager review
and acceptance, Manager supplies new immutable core/UI pins and resumes
the complete real Terminal flow, including the served notice file.

Add a mobile read-only check with a pane wider than 80 columns and taller than
24 rows: match the observed pane and mirror dimensions, scroll to reveal unique
right-edge and bottom markers, and verify no view-only input/resize/control
messages or durable hold changes. Then take control and verify the pane resizes
to the bounded viewport and the display follows. Switching back to view-only,
server restart and another controlling viewer must preserve the same rules.
Each builder commits/pushes its assigned branch and stops for Manager
review. No deployment or full native/onbash workflow acceptance follows from
this display correction alone.
