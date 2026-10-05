# Terminal UI: Manager acceptance of the frontend boundary

2026-10-08. Accepted candidate `2faabdfc3558a5b50396032274d2caf622c6e9a8`,
based on `b3d3a32169dbb8025ec406f4bc2e8fd7681f372b`, implements D38 and
input amendment 198. A fresh independent Astra MAX review reported no confirmed
defect; the Manager independently checked the completed source and evidence
before acceptance. Neither this acceptance nor merging deploys the system.

The increment adds Chat/Terminal selection, a local xterm viewer, explicit
take/release control, bounded input/output and read-only reconnect without
replay. The inspector distinguishes local recording from host archival. Older
backends and unavailable targets remain explicit. Selecting Terminal does not
launch a CLI; native startup/inbox/Chat/history connection remains separate work.

The independent reviewer built the exact candidate and passed all eight Chrome
groups, including the six retained interface checks. Its additional diagnostic
passed with a separately written strict peer using a real Chrome WebSocket
upgrade and automatically attached HttpOnly cookie. It checked actual keyboard,
Unicode and clipboard input, CDP composition, framed paste limits, exclusion of
device replies, stale control/receipts, recovery and no replay. The HTTP and
Terminal peers and data were synthetic; this is not installed-core acceptance.

The Manager inspected protocol/client/component and store/wire/layout changes,
the independent diagnostic source and results, and desktop, mobile and uncertain
input screenshots. Controls/warnings fit and were readable. The Manager matched
all 51 tracked candidate files across Git, builder/reviewer worktrees and the
tested export; verified the six original test bodies and 47 prior lock entries
were preserved; and verified all 161 xterm and nine fit-addon package files
against integrity-checked official tarballs. Both MIT notices are included.
The tested JS/CSS responses match the reviewed build and builder's bundle bytes.
No source or bundle discrepancy remained. The Manager did not rerun a third
copy of the full browser suite after those checks passed.

Reproducible private evidence is under the build run's
`review-terminal-ui-r1-private/` and `manager-review203/verification.json`.
The initial input-ordering decision and its narrower probe remain documented
in [the input decision](terminal-input-decision.md). Composition checks use
Chrome's input protocol, not a physical OS IME. Only Chrome 153.0.8010.36 was
tested. The installed font lacks the fixture's smiley glyph; Unicode bytes and
content assertions pass. Owned browser/server fixtures were closed.

Actual compiled-UI/installed-core Terminal acceptance remains required. The
first server candidate needs four confirmed corrections and a fresh review;
the UI alone does not prove a real shell, durable recording, native session,
processed Chat message, VM or browser surface. No owner session/data/profile,
native/provider call or deployment was used for this acceptance.
