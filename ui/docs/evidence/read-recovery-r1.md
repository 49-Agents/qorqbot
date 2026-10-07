# Status-read recovery evidence

2026-10-09 UTC. Implements the fixed [D49 plan](../READ-RECOVERY.md) on
`codex/fix-ui-read-recovery-r1`, from clean base
`04efca2723da0911d3bd13f64eeee1ad6e8d046a`. The completion report records the
candidate commit and verified remote SHA; Manager acceptance remains separate.

Transient `unavailable`, network and 5xx transport refusals retain the last
validated runtime/Terminal view with a visible observation warning and manual
retry. Runtime and native-history observations are labeled stale. Invalid
responses, including inconsistent control observations, still discard the view.
Existing selection, recovery and authentication guards remain in place.

An existing Terminal receives no retained HTTP snapshot while the read is in
error. Its socket, viewer, acknowledged control and queued/pending frames remain
intact. Valid socket observations continue to apply without clearing the HTTP
warning or its in-flight retry state; only a successful status read clears it.
New/reconnected viewers cannot use a retained view while its read is in error.
Failed reconnect reads preserve the disconnected display. TerminalClient,
protocol validation, receipts and input admission were unchanged.

## Checks

Private evidence root, **P**:

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/fix-ui-read-recovery-r1
```

Used Node 22.22.0, npm 10.9.4, locked Playwright 1.64.0 and installed
`/usr/bin/google-chrome` 153.0.8010.36. Copied compatible installed dependencies
from the accepted `build-runtime-inspector-r1` worktree with Python
`shutil.copytree(..., symlinks=True)` after exact lockfile-byte comparison.
The source dependency tree, declarations and lockfile were unchanged.

| Command (from the assigned worktree) | Result |
| --- | --- |
| `npm run build` | Passed strict TypeScript and production Vite build; `P/build-r1.log` |
| `QORQUT_UI_TEST_ARTIFACTS=P/chrome-r1 npm test -- --grep 'synthetic read recovery\|synthetic runtime inspector'` | 3 passed in 24.2 s; `P/chrome-r1.log` |
| `git diff --check` | Passed |

`P` in the command table denotes the full private path above. The three groups
cover the existing runtime inspector and two compact read-recovery cases:

- All three transient error classes against both reads; visible retained
  identity/timestamps/native counters; the same viewer and control epoch;
  pending plus queued human input submitted once and in sequence; continued
  output and socket observations; warning persistence during a held automatic
  retry and after frame receipts; later successful polls clear the warnings.
- First attachment from an errored cache stays closed, including while manual
  retry is pending. Actual socket closure clears unsent input. A failed reconnect
  read opens no viewer; successful reconnect is read-only with no replay.
  Positive unavailable/stopped/changed-identity observations and malformed or
  inconsistent-control observations still close the old viewer.
- Existing runtime validation, manual retry replacing an in-flight read,
  selection/recovery/expiry/logout fencing, memory-only drafts and desktop/mobile
  inspector assertions pass. No page exceptions were reported.

Directly inspected `P/chrome-r1/shell-synthetic-read-recov-f7519--through-transient-failures/read-recovery-live-warning.png`:
the warning, retained output/control and stale session/native-history labels are
readable. Runtime screenshots are also retained under `P/chrome-r1/`.

All build/test commands passed on their first run. Early discovery probes for
root/ancestor `node_modules`, `src/app.tsx` and `src/*inspector*` found no such
paths; the existing inspector is in `src/main.tsx`. Exact probe errors and the
dependency reuse step are recorded in `P/setup-notes.txt`. No failed test was
discarded or rewritten. Intentional transient failures remain protocol fixtures.

These checks use synthetic HTTP on ephemeral loopback ports and Playwright
WebSocket routing in fresh actual-Chrome contexts, with fixture/runner cleanup.
They are not integrated core, real shell, native CLI or provider acceptance.
No broad UI/native suite, real provider call, owner store/profile, live Qorqut
change, merge, PR or deployment was performed. Manager owns the later combined
core/UI check.
