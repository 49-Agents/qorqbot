# D47 file-reference cards — implementation evidence

2026-10-09 UTC. Implementation candidate for direct Manager review; this is not
connected-core acceptance. Branch: `codex/build-file-reference-cards-r1`, based on
`15f8d50c3a3cee2c68671e7111160f140bebb4ba` (UI implementation baseline
`65cfb95a3974bf3310f82b3e3914ca4601cc409a`). The frozen reference contract is core
`c13eb8b2dbf023a69e84cde2eda0368058485e6f`, read through immutable Git objects.
The complete Manager assignment is [FILE-REFERENCE-CARDS.md](../FILE-REFERENCE-CARDS.md).

`src/wire.ts` adds the exact D45 reference/source fields. `src/file-reference.ts`
guards the typed source, UUIDs, literals, UTF-8/code-point bounds and controls,
decimal mtime, null hash, UTC dates, and agreement with entry attribution,
label and timestamps. A historical recovery epoch remains valid provenance.
Malformed references and unsupported source/kind combinations have no card or
copy action. Message/native text does not infer a reference.

`src/file-reference-card.tsx`, EntryRow and nine CSS rules provide the outlined
card, saved label/location/path/note, observed byte size, and local Copy path
feedback. The existing occurrence header is labeled Observed for references;
the existing separate Recorded line remains when those timestamps differ.
Unsafe integer sizes display “Size exceeds precise display range”. Paths and
notes keep whitespace and escaped text. No contents are fetched or snapshotted;
there is no file link, opening/preview/download control or composer action.
Store pagination, refresh, entry-ID deduplication, Terminal and runtime inspector
code were not changed.

All checks used this worktree, the existing synthetic HTTP server on disposable
loopback ports, and temporary Playwright browser contexts. Versions: Node
`v22.22.0`, npm `10.9.4`, Playwright `1.64.0`, TypeScript `5.9.3`, Vite `8.3.3`,
`/usr/bin/google-chrome` `153.0.8010.36`. No dependencies were upgraded.
`package-lock.json` SHA-256 is
`1c07d1422b8271985fe16bf8761326b0078d18ddfbfa4f8ef682e00cd778e3de`.

Commands and observed results, run from the assigned worktree:

```bash
npm ci --no-audit --no-fund
npm run build
set -o pipefail
QORQUT_UI_TEST_ARTIFACTS=/tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-1 npm test -- --grep 'file-reference cards|history paging anchors|escaped content and desktop/mobile' 2>&1 | tee /tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-1.log
npm run build 2>&1 | tee /tmp/qorqut-file-reference-cards-r1.6s1yjZ/build-final.log
QORQUT_UI_TEST_ARTIFACTS=/tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-2 npm test -- --grep 'file-reference cards' 2>&1 | tee /tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-2.log
npm run build 2>&1 | tee /tmp/qorqut-file-reference-cards-r1.6s1yjZ/build-3.log
QORQUT_UI_TEST_ARTIFACTS=/tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-3 npm test -- --grep 'file-reference cards' 2>&1 | tee /tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-3.log
git diff --check
```

- Locked install succeeded (26 packages); all three builds/typechecks passed.
- `chrome-1`: existing history paging/scroll-anchor/equal-text/quiet-refresh/
  recovery group passed (18.2s), and existing escaped-content/desktop/mobile/
  focus group passed (1.5s). The new group reached its final reload and then
  timed out selecting an agent without opening the mobile drawer. This was a
  test navigation omission. Its original failure screenshot/context and log
  remain in `chrome-1`; nothing was overwritten.
- After adding the missing drawer-open step, `chrome-2`: focused group passed,
  1 test, 10.1s total (7.9s test).
- Negative label/timestamp fixtures were then aligned with their entry fields
  to ensure field validation was tested independently of agreement validation;
  a missing required null field case was added. `chrome-3`: final focused group
  passed, 1 test, 9.8s total (7.8s test). No production behavior changed after
  the initial three-group run; no Terminal suite was rerun.
- `git diff --check` passed.

The focused group verifies owner/agent attribution, workspace/host location,
old recovery epoch, occurrence/recording distinction, exact saved text and UTF-8
clipboard bytes (including decomposed Unicode and trailing spaces), keyboard
Enter/Space and Tab focus, per-card success, rejection/absent clipboard and
manual path selection, maximum Unicode code-point lengths (4,096 path, 200
label, 4,000 note), unsafe versus largest safe size, malformed references and
entry mismatches, escaped HTML-shaped content, and ordinary/native path-looking
messages. Repeated normal refresh and reload leave one card per entry ID;
reload clears copy feedback. Request inspection permits only static assets,
session/discovery and the existing read operations: zero file-content requests,
card mutations, new messages or WebSockets. The shared page-error fixture remained
empty. Desktop 1440×960 and phone 390×844 checks found no horizontal page,
history, card, path or note overflow.

Original final screenshot paths:

- [Desktop](</tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-3/shell-file-reference-cards-b7e33--and-wrap-on-desktop-mobile/file-reference-desktop.png>)
- [Mobile owner](</tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-3/shell-file-reference-cards-b7e33--and-wrap-on-desktop-mobile/file-reference-mobile-owner.png>)
- [Mobile agent](</tmp/qorqut-file-reference-cards-r1.6s1yjZ/chrome-3/shell-file-reference-cards-b7e33--and-wrap-on-desktop-mobile/file-reference-mobile-agent.png>)

The corresponding `chrome-2` screenshots were visually inspected. Final
`chrome-3` files are byte-identical, verified with `sha256sum`: desktop
`ed531e3cfdd0ea6cc3d0ee4e32e682acfb840a33bf2bb25391652c8603a79ee5`,
mobile owner `3840ced2701b323d2032316544b66b3da44b4664ad4e23fe3d24c49450519159`,
mobile agent `6033cc7e618bd49f601c9adec152cc902f31481efee10b748320549c22c9c9c5`.
Cards and focus indicators are readable, text wraps within the outline, and
copy failure guidance stays local. The agent screenshot is scrolled to its copy
button; its sender/time header is visible in the desktop and mobile-owner
screenshots. This host renders some emoji using missing-glyph boxes; DOM text,
selection and clipboard checks retain their exact Unicode bytes.

Final built assets are `index-BFqM8vsh.css` (SHA-256
`847aa7e2b5af8a913f6bbd19b4f179e538ef5d0d79df884ff2b7a93b2f5fbe89`)
and `index-YBRhn8SX.js` (SHA-256
`faac70676741f950b327570663c6126349874e59bb00805048e4cf0a743f517c`).

These checks exercise the compiled UI with synthetic metadata and a local
clipboard stub. They do not prove real filesystem availability, OS clipboard
permission behavior, core/MCP publication, or native inference. Manager owns
the planned actual compiled-UI/core/MCP flow and final acceptance. No backend,
live installation, owner session/config/data, browser profile, global service,
VM, other worktree or historical repository evidence was changed. No PR,
merge or deployment was performed. The exact committed and pushed candidate
SHA is supplied in the ACLA completion report.
