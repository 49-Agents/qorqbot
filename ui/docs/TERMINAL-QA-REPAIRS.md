# Terminal QA repairs (round 2026-10-13)

Source base `ac90f18`. Bounded, evidence-driven check of the Terminal observations
in the five-tester QA run. Raw scripts, JSON and screenshots live outside Git in
`/home/alp/qorqbot-launch/qa/2026-10-13/acla-fixes-9552beea/terminal-evidence/`.

Fixture: private data root, private HOME, own tmux socket, dynamic port, own headless
`/usr/bin/google-chrome` profile; real core server (worktree `core/src` at the base, unchanged) with the freshly built
`ui/dist` (`index-B3Co151l.js` for the fix, `index-AQAOozgg.js` for the unfixed baseline).
Recorded `onbash` Bash 5.2.21, tmux 3.4. Nothing used a native CLI, provider or owner state.
A submitted receipt means the bytes were pasted into the pane, not that the shell processed them.

## A/B. Ctrl+C followed by typing — reproduced, repaired

Counts are passes/total unless marked failures.

| Case | Result |
| --- | --- |
| Real Chrome, fast server, type, Ctrl+C, type, Enter, ASCII and Unicode (`é日本`), 20 rounds, unfixed UI | 20/20 passed. First frame is one key; the rest is one batch. |
| Same, fixed UI | 20/20 passed. |
| Direct tmux, no browser, runner transport (`load-buffer`/`paste-buffer -r`): `printf JUNK`, `^C`, `printf …`, Enter in **one write**, plain disposable bash | 0/20 passed, 20/20 failed (`bash: rintf: command not found`). |
| Same on the recorded fixture pane | 0/20 passed, 20/20 failed. |
| Same bytes in **two writes split at ^C** (both panes) | 20/20 passed. |
| Same bytes as `p` then the remainder (the UI's usual shape) | 20/20 passed. |
| Real Chrome with 20x CPU throttle (slow receipts, larger batches), `true`+Enter, `printf JUNKn`, Ctrl+C, `printf …`, Enter, unfixed UI | 25/36 passed over three runs of 12; 11/36 failed with the QA symptom (`rintf: command not found`). |
| Same, fixed UI | 24/24 passed over two runs of 12. |

Cause: the tty/readline discards input racing the SIGINT handling, so bytes that follow
^C in the same write can lose their first character. This is native line-discipline
behaviour; the frontend exposed it by coalescing ^C with later printable input while a
receipt was pending. Not a transport loss: every frame received a matching receipt.

Repair (`ui/src/terminal-client.ts`): an explicit Ctrl+C or Ctrl+Z key (xterm `onKey`
value `\x03`/`\x1a`) is a batch barrier; the batch ends after it and later input waits
for its receipt. Bytes and order are unchanged, there is still at most one in-flight
request, no sleep, no key injection and no resend. Paste stays one framed entry (a literal
`\x03` inside a paste is not a barrier) and resize barriers are unchanged. Submitted receipts
still make no claim about process completion; the interrupt may still be a race inside the
shell, but input no longer shares its write.

Regression: `ui/tests/terminal-qa-repairs.spec.ts` fails before the fix (one batch
`ab…cd…éf`) and passes after (`ab\x03`, `cd\x1a`, `éf\r`, each after its own receipt).
`ui/tests/shell.spec.ts` D41 scenario was updated by Manager's decision: the first batch now
ends at Ctrl+C and ArrowLeft plus the paste are the next frame.

## C. Bracketed paste — not reproduced, no change

Real clipboard (`navigator.clipboard` + Ctrl+Shift+V, 3 runs) and a labeled **synthetic**
DOM `ClipboardEvent` (3 runs) both produce exactly one frame
`ESC[200~…ESC[201~` (one xterm framing pass, one handler). Bash's readline
(`enable-bracketed-paste on`) accepts it with no literal `^[[200~`; it inserts the lines
and waits for Enter, which is bash's bracketed-paste behaviour. Direct tmux control: framed
paste identical; unframed paste executes both lines. A framed paste written immediately
after a ^C write left a literal marker 0/20 times. The QA literal `^[[200~` was not
reproduced, so no speculative change; the likely cause would be a stale xterm mode while
bash had bracketed paste off, which this fixture did not show.

## D. 20 rapid Chat/Terminal switches — frontend clean, server capacity race found

Single browser, real server, no fault proxy. Evidence: `d-switch-ui.json` (first run) and
`d-trace/` (second run with private, non-production tracing wrappers around
`Terminals.attach`, `TerminalStream.close`, `TerminalSockets.endpoint` and the log formatter).

Frontend: every superseded socket was closed by the client and no late callback acted. In the
traced run the server saw 41 attach attempts: 22 attached and were closed (22 `close_end`),
19 were refused. After the browser closed the server held 0 attachments (trace
`total: 0`) and 0 established connections on its port. Nothing is stranded.

The handshake 403s are real, not a browser artefact. All 19 refusals are
`CoreError("unavailable", "Terminal stream capacity unavailable")` raised at
`core/src/qorqut_core/runtime/terminals.py:310-312` when `MAX_AGENT_STREAMS = 4` (line 32) is already
used for this agent. The transport swallows it at `transports/server.py:282` and closes before accept,
which the browser sees as HTTP 403. Each time the occupants were superseded viewers: 2-3 still
inside `attach()` (0.13-1.35 s; their client was already gone, the attach still completes and is
then closed) and 1-2 inside `TerminalStream.close()` (0.10-1.11 s). Capacity is therefore held by
closing/abandoned viewers, not by stale observations.

Consequence: in one of three 20-switch bursts the FINAL selected viewer was one of the refused
attaches (per-agent count 4 at its attach). It stayed disconnected with the existing
"Terminal connection lost ... Reconnect terminal" alert and no automatic retry. All other
attachments were released within 0.6 s of it; the explicit Reconnect then attached
and the Terminal was editable with no warning (`d-trace/browser-repeat.json`,
`final-disconnected.png`).

The "Core server diagnostic (details omitted)" lines are `Exception in ASGI application`
(22/22 ordinary closes, `starlette.websockets.WebSocketDisconnect(1006)`): the final
`await websocket.close(code=1008)` at `transports/server.py:298` raises it when the client has
already gone, and the `except` at line 299 lists only `OSError, RuntimeError, TimeoutError`.
It happens after cleanup (`stream.close`) completed, so no resource is affected; it only produces
misleading error logs. No server change was made in this lane.

## Limits

Fixture host load did not reproduce the QA's 0.5-2.9 s echo latency; slow receipts were
emulated with browser CPU throttling. Only one shell and one browser were used. No native
CLI behaviour is claimed.
