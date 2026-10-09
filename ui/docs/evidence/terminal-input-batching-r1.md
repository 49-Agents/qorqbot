# Queued human Terminal input batching — D41

2026-10-09. Candidate branch `codex/build-terminal-input-batching-r1`, immutable
base `884a458ca275177eb988e89ddca9a735a70d7aa5`. This evidence accompanies the
implementation and owning tests in one commit; the completion report and private
`candidate.json` supply its immutable commit/tree IDs and push result. Manager
owns review and acceptance. No merge, deployment or native-workflow claim.

`TerminalClient.flush` now joins the consecutive admitted input entries at the
front of the raw queue, stopping before a resize. It copies encoded bytes only
when sending a multi-entry batch. The first input still sends immediately; the
existing queued-byte accounting, 64-KiB bound, single pending frame and exact
sequence/epoch/byte-count receipt apply to the whole batch. Input admission,
resize coalescing, output limits and connection/control/identity clearing are
unchanged. There is no timer, retry, pipelining, protocol or backend change.

The existing Terminal Chrome group now holds the first input receipt while
`keyboard.type` produces a 360-character burst, followed by Unicode, control and
arrow keys and a bracketed multiline clipboard paste. It verifies exact base64
bytes in one batch, an intervening resize with its own receipt, and a separate
batch after that barrier. The queue-limit check retains overflow rejection and
the final resize, expecting input lengths `[1, 65536]`. The control-loss check
discards a substantial queued burst and does not revive it after stale receipts
or takeover. Existing keyboard, IME, paste, sequence, uncertainty and replay
checks remain in the same group; the synthetic peer is unchanged.

## Focused checks and provenance

From the assigned worktree, with private cache/output directories:

```sh
npm ci --no-audit --no-fund
npm run build
QORQUT_UI_TEST_ARTIFACTS=<private-root>/chrome-tests npm test -- --grep Terminal
git diff --check
```

Locked installation, TypeScript/Vite build and both Terminal Chrome groups
passed: **2/2 in 18.3 seconds**. Unrelated groups were not rerun. The six
nonterminal groups are unchanged. No package or lock file changed.

The real fixture used the exact core export
`a2a162ce67f0eccafee537bd75a97069c1280788`, installed from its unchanged
`requirements.lock` into a new private venv, with no editable install. All 233
export files remained unchanged; all 76 packaged files matched source, wheel and
installation. Console import logs verified 72 loaded source modules from that
installation. Wheel SHA-256:
`57e61c4cba6856fdf502c733855a2842747dda9fd68e3530ab189ec0ceb73757`.

The same compiled UI was served by normal `qorqut-core serve --installation
<fixture>/data/installation.json --transport-config <fixture>/transport.json
--ui-root <private-root>/ui-dist`. All four served assets matched their build
hashes. JavaScript `index-Cep084dq.js` SHA-256:
`d49a841681c9f0fafd638b53cda1121fc106ca63edf772e73d617891533fb79a`.
The code and test bytes still matched the successful build after the real flow.

Versions: Node 22.22.0, locked Playwright 1.64.0, installed Chrome 153.0.8010.36,
Python 3.12.3 and tmux 3.4. The private harness reused only the completed
recorded-Terminal fixture's normal setup, observation and owned cleanup. It used
fresh installation/data/HOME, an isolated loopback port and a fresh Chrome
profile. No API/WS interception, internal client calls or input injectors were
used. Public WebSocket and Chrome network events supplied passive observations.

## Installed-core / compiled-UI Bash result

Private commands: `<private-root>/venv/bin/python -I
<private-root>/provenance.py`, then the same interpreter running `run.py` from a
test-only working directory. Normal provisioning created the recorded Bash;
the application retained 16 operations, five job handlers and schema 7.

Fresh **attempt-2 passed**, using actual textarea focus and
`page.keyboard.type(command, { delay: 2 })` for an exactly 500-byte harmless
`printf` command. No Enter was included in that typing call.

| Measurement | Result |
| --- | --- |
| Typing time | 2,142.989 ms |
| Last input receipt after typing ended | 394.993 ms; target ≤15,000 ms |
| Typing through final typing receipt | 2,537.981 ms |
| Input-frame lengths | `1, 77, 61, 62, 62, 73, 61, 64, 39` |
| Exact typing bytes / matching receipts | 500 / 9 |
| Separate Enter | One `0d` byte, one frame and matching receipt |
| All frame receipts | 11: initial resize, nine typing batches, Enter |
| Reconnected viewer's outbound frames | 0 |

Concatenated input matched the command byte-for-byte, SHA-256
`bc16cd552387ba3a0a4694cee2cbb15a5cab39dd4534f8ad91e174fd910d39ea`.
The event timeline verified one frame in flight and exact viewer/epoch/sequence,
kind, submitted outcome and byte count on every receipt. Before the separate
Enter, the output file did not exist. After Enter, it contained exactly the
expected 436 bytes, and raw recorder output contained exactly one
`D41_BATCH_DONE\r\n` marker. The inspected screenshot shows that output and the
returned Bash prompt. Chat → Terminal reconnect retained the same shell and
started view-only; two subsequent state observations brought no input replay,
duplicate marker or output-file change.

Attempt-1 remains a failed harness run: its 500 exact bytes used eight frames,
with 3,027.264 ms typing and 326.435 ms drain, but an additional empty-console
assertion found unattributed HTTP 401/404 messages. Its cleanup also encountered
`pidfd_open` EINVAL. Supplemental reconciliation proved all saved PIDs absent,
the listener absent and the fixture removed, sending no further signals.

Manager decision 271 authorized passive URL/status/console-location attribution
and only the fresh attempt-2 repeat against the same built artifacts. That run
correlated the two resource-console messages to same-origin GET
`/api/v1/session` 401 before successful sign-in and `/favicon.ico` 404. There
were no other failed responses/requests, page exceptions, Terminal domain/WS
errors or CSP errors. Attempt-1 was not retroactively accepted or overwritten.

Attempt-2 cleanup recorded process start identities, namespace, Tgid and state;
reaped exited descendants and signaled one still-live, exactly verified residual
recorder through its pidfd after owned shell shutdown. All 17 saved identities
were then positively absent, the listener was absent and the fixture/profile
directory was removed. Cleanup reported no error. Prior accepted evidence and
other actors' fixtures were untouched.

Private evidence is under the handoff's `build-terminal-input-batching-r1`
directory: `ui-checks.log`, `core-build.log`, `provenance.json`, both immutable
attempt directories, their copied harnesses, `browser-result.json`,
`websocket-events.json`, `page-errors.json`, `installed-imports.json`, process/
listener/cleanup records, raw captures and `screenshots/terminal-output.png`.
No private profiles, tokens, transcripts or runtime stores are committed.

Per-frame input receipts still took **234.756–383.018 ms** in the passing
fixture (mean 281.067 ms). Batching removes per-character waits for queued input;
it does not remove single-frame latency or attribute backend costs. The drain
target is bounded fixture evidence, not a universal latency guarantee. This
tests recorded Bash only, excludes the concurrent core archival change, and
makes no native CLI, Chat delivery, host archive, VM or deployment claim.
