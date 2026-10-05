# Manual creation UI r1 evidence

2026-10-08. Branch `codex/manual-creation-ui-r1`, base
`5812599566167ffcc524cbb2255dd371e4fb2bfe`. Core D36 plan `862d989` was read
only as the contract; no unfinished core code was used. The completion
envelope records the delivery commit, clean status and push result.

## Change

- Creation dialog: display name and the existing twenty icons only, plus a
  short note that creation starts no CLI and the user starts/configures it in
  Terminal. Provider, model and start controls and model validation are gone.
- `Store.create` accepts `display_name`/`icon_key` and sends only those plus
  its existing `idempotency_key`. Pending requests remain immutable snapshots;
  ambiguity, retry, draft, session and selection guards are untouched.
- Inspector: the configured provider/model/start-request section is replaced by
  "Current run" showing the observed run or Unknown, with the manual-start note.
  No Terminal button was added; the existing "Terminal unavailable" section stays.
- Legacy `cli`/`start_requested` remain in `wire.ts` types, unrendered.
- Synthetic fixture no longer reads `cli`/`start` from the request.
- The existing grouped creation check now asserts the three controls are absent,
  the inspector has no provider/model/start text, and the submitted args keys
  are exactly `display_name`, `icon_key`, `idempotency_key`. Rename/icon
  behaviour and the other five grouped checks are unchanged.

## Checks

`npm ci` (locked), `npm run build` (strict typecheck + vite) passed. Six
Playwright checks passed once (41.6s) with `/usr/bin/google-chrome`, fresh
contexts, synthetic API on temporary ports, `TMPDIR=/tmp`; output in
`/tmp/qorqut-ui-mc-r1`, outside Git. Desktop (`desktop.png`) and mobile
(`mobile-inspector.png`) screenshots were inspected: Current run shows Unknown
with the manual-start note, no provider/model fields. The existing
screenshots do not capture the creation dialog itself, which was verified by
the assertions only, not visually.

## Limits

The API is synthetic, so this is not integration with the revised core, and it
establishes no Terminal viewer, native CLI, browser or VM readiness. Manager's
installed-core/compiled-UI acceptance is still required.
