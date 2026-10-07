# Manual creation: combined verification r1

2026-10-08, branch `codex/manual-creation-integration-r1`. Implementation of
the Manager's [combined check](../MANUAL-CREATION-INTEGRATION.md), pending fresh
independent review. No acceptance gate, merge or deployment is claimed.

## Inputs and change

- Prepared core merge: `12d1139d9319da2cf347d310b4f82409ab65783c`;
  Manager plan: `8345c148b22b70e632fb8d530a6a2f9179de0f9a`.
- Accepted creation: `8d2039f8efc5572726c70aafbf12d50d16558c69`;
  accepted ownership: `1af5f852ccbc83cc898df11099beb4031ca0b2ca`.
- UI: `6a4841d5830ae3970b492ebf87528de4f94f787c`, privately exported with
  `git -C /home/alp/qorqutbot archive` at that exact commit.
- Historical receipt writer: `862d989edf0fef0bc6c5100a051e936cb5afbcde`.
  Its real source subprocess ran from the assigned worktree's available Git
  history; the old-code test passed without skipping.

Only `tests/agents/test_registry.py` and this evidence change. The test asserts
schema-5 open refusal, explicitly migrates the same database under its host lock
and configured busy timeout, then opens normally at schema 6. It compares all
five watched tables and prior migration checksums across migration. Original
explicit-true/omitted-start receipts, conflict/replay, no-launch provisioning
and reopen assertions remain. The shared flow already counted fifteen tools;
`test_agent_flow.py`, production, migrations, locks and UI bytes are unchanged.
All other 182 tracked paths from the prepared core merge matched byte for byte.

## Commands and results

Private artifacts are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/worker-manual-creation-integration-r1`
(below, `$A`); `$W` is the assigned core worktree and `$P=$A/venv/bin/python`.
Python 3.12.3, Node 22.22.0, npm 10.9.4, locked Playwright 1.64.0 and installed
`/usr/bin/google-chrome` 153.0.8010.36 were used. Dependencies stayed private;
normal pip/npm caches were used without global package/configuration changes.

```sh
/usr/bin/python3.12 -m venv "$A/venv"
"$P" -m pip install -r "$W/requirements.lock"
cd "$W"
PYTHONPATH=src:tests PYTHONNOUSERSITE=1 "$P" -m unittest discover -s tests/agents -t tests -v
PYTHONPATH=src:tests PYTHONNOUSERSITE=1 "$P" -m unittest discover -s tests/runtime -t tests -v
# core-source is the private Git archive of the exact prepared merge.
cd "$A/core-source"
"$P" -m pip wheel --no-deps --no-build-isolation --wheel-dir ../wheels .
"$P" -m pip install --no-deps "$A/wheels/qorqut_core-0.1.0-py3-none-any.whl"
cd "$A/candidate"
npm ci --no-audit --no-fund
npm run build
cd "$A"
PYTHONPATH="$A/tests-only/tests" PYTHONNOUSERSITE=1 "$P" run_installed.py
PYTHONPATH="$A/tests-only/tests" PYTHONNOUSERSITE=1 "$P" run.py
```

Source agents: **20 passed** (27.092s); source runtime: **12 passed** (10.997s).
Installed package: **2 passed** (5.975s), reusing the unchanged shared
HTTP/MCP creation/provisioning flow and actual-child race/reconnect/reopen/exit/
stale-release flow. Neither installed flow skipped. UI strict typecheck/build
passed. The compact actual UI/core workflow and normal-restart continuation
both passed; synthetic UI API tests were not substituted for these checks.

`run_installed.py`, `run.py`, `browser.mjs`, `provenance.py`, command log,
test/build logs, `provenance.json`, `installed-provenance.json`, `result.json`,
`frozen-paths.json` and `SHA256SUMS` are retained privately for review. The
installed runs use staged test fixtures only, outside the source checkout.
All 61 package files match Git, exported source, wheel and installation exactly;
all 31 dependency pins match. Every loaded core module is in the private
site-packages directory (46 during the UI run). The child command, `/proc`
command line, clean installed-only PYTHONPATH and generated console-script bytes
are recorded: the normal `venv/bin/qorqut-core serve --installation ...
--transport-config ... --ui-root .../candidate/dist` entrypoint, with no source
fallback, debug server, gated worker or manually pumped jobs.

| Artifact | SHA-256 |
| --- | --- |
| Core source manifest | `b7d1675e0b100e8bdb972b5de24736b06ee596c483ba560c1ad1c6741b397962` |
| New wheel | `c9b74e57b4ea7aa1fafe8f7df3378a6b6fe5ef67dd0735d9031be0a4c72f8501` |
| Core requirements.lock | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| UI source manifest (43 files) | `6363def54434ff30e17d23935e46380984ba1b1983029060fc83ef34d2eb7018` |
| UI package-lock.json | `1bb41c1a285e07e63ed856ef05f12ecc3351cae41d88f04e66863d52ce22b0cd` |
| dist/index.html | `387e8e4f7188f83df542a242a2247b4fb981c4760e6e1194b46ff8c8349c2dd6` |
| dist/assets/index-0gBfR39L.js | `34dfb34757cf7ec5f15a8389b8a97bfb2e8c33e6a77b475f445aafa035e31462` |
| dist/assets/index-UG1v5Rja.css | `05ce6fec2a63ed77f26e7cd0404b8993277409385f4ca37a4c24abe4317ce773` |

Manifest hashes cover sorted `sha256  path\n` records. All UI source bytes
match the immutable export after building. HTTP-served asset bytes match the
build before and after server restart (`index.html` is served at `/`).

## Observed actual UI/core behavior

Fresh Chrome contexts signed in with the disposable installation's actual
HttpOnly/SameSite=Strict cookie session. Configuration exposed twenty icons and
discovery exposed fifteen operations. Onbash runtime was disconnected with
`input_ready=false` and no owner. Creation exposed only name/icons, showed the
manual Terminal explanation, and sent exactly `display_name`, `icon_key` and
`idempotency_key`. Its receipt had `start_requested=false`. Rename and icon
changes preserved identity/conversation/workspace/resources; automatic workspace
publication reached revision 3 while resource provisioning retained its real
`dependency_unavailable` hold. Current run stayed Unknown and Terminal unavailable.
There were zero session-owner rows and no server child processes observed in
10,869 owned `/proc` task-children reads; the source no-launch guards also passed.

Two equal Unicode/multiline bodies received distinct keys, messages and entries;
one further message targeted onbash. Exact DOM/API/SQL/dated-file content and
known/null provenance matched. Drafts survived agent selection; authenticated
reload preserved history and honest queued/runtime-unavailable state. After
each browser closed, both automatic UTC-dated JSONL files matched the durable
entries exactly. The restarted normal server and new signed-in context retained
the same metadata, three entries and both transcript hashes. No save/export
action, intercepted response or synthetic native session was used.

Desktop inspector collapse/reopen, mobile navigation, inspector scrolling to
Terminal and expanded identity details, drawer close and logout passed. No
horizontal page overflow, uncaught page error or CSP violation was observed.
All eleven screenshots were visually inspected: `creation-desktop.png`,
`desktop.png`, `restart-desktop.png`, and the `first-`/`restart-` versions of
`mobile.png`, `mobile-agents.png`, `mobile-inspector.png` and
`mobile-inspector-details.png`. The host font environment displays the smile
emoji as a missing-glyph box; its exact Unicode content survives every compared
storage/rendered-text boundary. No font or UI change was made.

Two private harness failures preceded success: the inherited restart assertion
assumed onbash was initially selected, and the added asset check requested
`/index.html` instead of core's `/` route. Explicit selection and the correct
asset route fixed these harness assumptions. Logs remain in `attempt-1/` and
`attempt-2/`. A passing run was extended only to verify the below-fold mobile
inspector content; the final full two-pass workflow also passed.

Owned servers/children and browser contexts were closed; the disposable data
root and synthetic credential manifest were removed. Chrome used fresh
Playwright-managed temporary profiles. Review
artifacts remain private. No owner stores, native/provider turns, live Qorqut
data, other assignments, VM/service setup, merge or deployment were used.
This establishes combined creation, durable messaging/publication and the
existing console shell only. Native startup/hookup, assistant capture, safe
delivery, Terminal transport, browser/VNC and VM readiness remain unproven;
no full G0–G6 gate advances. Manager owns acceptance and landing.
