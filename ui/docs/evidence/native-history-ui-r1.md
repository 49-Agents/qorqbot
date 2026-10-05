# Native history UI r1 evidence

2026-10-08. Branch `codex/native-history-ui-r1`, base
`95138ae582891375f9a1fe752eb711f95caa3e06`. Conversation types follow accepted
core `35b6d82cd702fe6e82562d7a3c6533ac04050baa`, specifically
`conversation/models.py`. The completion report records the delivery SHA and
clean/pushed status.

## Changes

- `src/wire.ts`: discriminated message/native source union, complete native
  provenance without `message_id`, nullable sender, and separate conversation
  and Terminal pin comments. Flexible entry kind/role and nullable
  delivery/handling remain. Terminal declarations are unchanged.
- `src/main.tsx`: only the composer note changes to “Start and manage the CLI
  in Terminal. Messages are saved for delivery.” Existing EntryRow rendering,
  API/store polling, styling and other notices remain unchanged.
- `tests/synthetic-api.ts`: native user/agent fixture using synthetic UUIDs,
  byte lengths and hashes, null delivery/handling, and a null native user sender.
  Native rows create no coordination message event. Existing message rows retain
  their behavior; message-only access narrows at `source.type`.
- `tests/shell.spec.ts`: the existing escaping/drawer group now checks Claude
  user and Codex agent rows, Owner/Atlas labels, exact Unicode and whitespace,
  escaped markup, empty delivery/handling presentation, both timestamps, equal
  text under two distinct entry IDs, cursor refresh and stable rows after page
  reload. Original escaping, unsupported-content, publication and drawer checks
  remain, alongside a check that message delivery badges remain present. The
  paging group's only edit is the required message-source type narrowing.
- This evidence file records the bounded checks; no dependency or Terminal
  fixture/browser-group changes were made.

## Checks

From the assigned worktree:

```sh
npm ci --no-audit --no-fund
npm run build
TMPDIR=/tmp QORQUT_UI_TEST_ARTIFACTS=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-history-ui-r1/playwright npm test -- --grep 'escaped content and desktop/mobile drawers, focus and screenshots'
git diff --check
```

Locked install succeeded (26 packages); strict TypeScript checking and Vite
production build passed. The selected existing browser group passed once:
1 passed, 3.1 seconds, using `/usr/bin/google-chrome`, a fresh Playwright context,
the compiled `dist` UI and the synthetic API on an allocated loopback port.
The same test command was first attempted with the anchored filter
`'^escaped content and desktop/mobile drawers, focus and screenshots$'`; it
exited 1 with “No tests found” and ran no groups. The corrected filter above
selected only the intended group. `git diff --check` passed.

The group's existing desktop/mobile screenshots remain under the private
artifact directory in the command above, outside Git. No additional screenshot
matrix was needed because this change does not alter layout.

## Limits

These are synthetic frontend checks, not connected native collection, input
dispatch, actual provider, installed-core, browser MCP or VM acceptance.
Terminal suites were not run. No owner sessions, stores, profiles or services
were used as fixtures. No merge or deployment was performed.

## Manager acceptance

Manager reviewed completed candidate
`dc884294ba689ae526ee80b1ccacb213ee036fd6` against the core native Entry model
and the scoped handoff. The message/native union, nullable sender and existing
escaped presentation match the contract. No source-specific badges or text
deduplication were added.

The candidate merged cleanly with accepted Terminal geometry on UI
`3f5684ffd1f0c78d3a99690ea7a11c6ff07381fb`. In that combined coordination tree,
Manager ran the locked install, production build/type check and the same
escaping/drawer browser group: **1 passed in 3.8 seconds**. The first build
attempt found no installed TypeScript dependency (`tsc: not found`); the locked
install resolved the environment issue without changing dependencies.
Manager inspected that run's desktop/mobile screenshots. Logs and captures
are private under `manager-review239` in the existing build-state directory.

The changes are accepted for frontend native-entry compatibility and merged
without modifying the candidate's implementation or tests. The separate real
Terminal acceptance retains its previously pinned core/UI pair. This frontend
check does not establish native capture or dispatch through the normal server.
