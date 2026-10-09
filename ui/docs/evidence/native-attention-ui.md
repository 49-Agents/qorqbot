# D59 native attention UI evidence

2026-10-09; branch `codex/native-attention-ui`, based on
`c2b2969434753b984c81536c07be3dcd94c3b474`. Implements the Manager's
[frozen D59 contract](../NATIVE-ATTENTION.md) in the frontend only.

Runtime validation accepts both the original exact response and the additive
optional nullable attention field. Waiting requires a connected current owner,
matching owner/terminal IDs, UTC observation time, approval/question kind and
plain text bounded to 6,000 Unicode code points. Non-waiting prompt/kind are
null and truncation is false. Extra fields and inconsistent observations fail
validation. No native terminal parsing or input-readiness inference was added.

Chat has one notice above its existing composer, with the plain prompt/choices,
approval/question label, observation time, truncation notice, six-line initial
area and an accessible expansion button for longer content. Open Terminal calls
the existing same-agent view switch. There are no answer/approval/dismiss actions.
Unknown/failed reads suppress the old prompt and display an unavailable
observation; a refresh identifies retained information as last observed.
Disconnected/none/old-core responses clear the notice. Changing selection clears
that agent's cached runtime before rereading, preventing an old wait from
reappearing on return. The existing inspector retains its qualified transient
failure behavior. The agent row adds a visible Needs input label using the
existing core-owned flag, preserving browser attention.

Checks on Node 22.22.0 and npm 10.9.4:

- `npm ci` passed using the existing lockfile.
- `npm run build -- --outDir <private-root>/dist` passed, including
  `npm run typecheck` / `tsc --noEmit`. An initial typecheck identified a test
  fixture union annotation; the corrected final build passed.
- `QORQUT_UI_TEST_ARTIFACTS=<private-root>/checks-final npm test -- --grep 'D59 pure'`
  passed: two pure checks in the existing shell suite. Neither requests a
  page/browser fixture nor starts an API server. They cover old/null responses,
  approval/question, bounds, malformed fields/timestamps, identity mismatch,
  clearing, unknown/read failure, superseded reads and selection, literal text
  VNodes without HTML/form injection, the Open Terminal callback, and unchanged
  draft/scroll data without message/input effects.
- `git diff --check` passed.

The Vite chunk-size warning remains nonfatal (621.12 kB JavaScript, 170.75 kB
gzip). The private output directory warning is expected; this was a new output
directory. The built browser and Terminal notice files are retained.

Private root:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-attention-ui-r1`.
It contains npm/build/test logs, `dist/`, `dist.sha256`, `artifact.sha256`, and
`native-attention-ui-dist.tar.gz`. Archive SHA-256:
`9d94eff4f8c89c24dab557a9495b6d5daddedf17e8679d6397127dc765f80789`.

Codex's built-in `cua.getState()` returned no apps or browsers. No hands-on
browser check, automated browser launch, screenshots, visual layout/expansion,
DOM focus/selection/scroll verification, or native end-to-end flow is claimed.
The pure checks verify presentation data/callbacks and store state, not rendered
browser behavior. Installed-core acceptance and deployment remain with Manager.
No owner processes, native stores, profiles, terminal sessions or live services
were used. No nested agents or reviewer batch was started. No merge or
deployment was performed by this lane.
