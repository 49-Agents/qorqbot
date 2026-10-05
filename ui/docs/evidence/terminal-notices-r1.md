# Terminal notice packaging correction

2026-10-08; branch `codex/fix-terminal-notices-r1`.

Per Manager clarification 212 in `docs/TERMINAL-IMPLEMENTATION.md`, the normal
core host serves `/` and `/assets/*`. The accepted UI emitted the notices at
`dist/terminal-notices.txt`, so `/terminal-notices.txt` returned 404.

Moved `public/terminal-notices.txt` to `public/assets/terminal-notices.txt`.
The corrected build emits `dist/assets/terminal-notices.txt`; the old
`dist/terminal-notices.txt` is absent. All 2,396 notice bytes match the accepted
source, the assignment base, the relocated source and the corrected output:
SHA256 `adab7e720e02259bc98b5d8542d6e99d2e6438239a089579cd6cffd9a9e944e5`.
Git records a 100% rename.

Clean private exports were built from accepted UI commit
`e380b4c053a2bbc602e775cb2e167f6b3e30c32c` and corrected source tree
`38829c74dfbd07ff2b87bebe441412bece188f39`, based on
`6b7352ad712c636ca564c64f78a666fe7a64af4b`. Only this evidence file was added
after that corrected source snapshot. Both exports ran `npm ci` followed by
`npm run build` (including `tsc --noEmit`) successfully, using Node v22.22.0,
npm 10.9.4 and locked Vite 8.3.3. The unchanged lockfile SHA256 is
`1c07d1422b8271985fe16bf8761326b0078d18ddfbfa4f8ef682e00cd778e3de`.

The complete build comparison found only the notice relocation. These existing
outputs match byte for byte, including their names:

- `index.html`
- `assets/index-CdidpGAZ.js`
- `assets/index-CFwrtNAV.css`

Source comparison confirmed that index.html, application source/styles, tests,
dependencies and every other runtime asset are unchanged. Builds did not alter
any exported tracked input. The assignment base differs from accepted UI only
by the pre-existing Manager clarification. Source/build diffs were inspected;
`git diff --check` passed.

Private evidence directory:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/terminal-notices-r1-private`.
It contains the clean exports/archives, `commands.log` with exact commands and
working directories, `toolchain.json`, `provenance.json`, `comparison.json`,
`accepted-dist.sha256`, `corrected-dist.sha256`, both source/dist JSON manifests,
both npm-ci/build logs, `source-diff.patch`, `build-diff.txt`, and
`final-provenance.json` tying the completed commit to the built source.

No new tests or browser/UI rerun were needed for this mechanical relocation.
Real-host fetching was not performed here and is not claimed to pass; the
separate integration actor owns it after independent review and Manager
acceptance. No core route change, merge or deployment is included.
