# Runtime inspector implementation evidence

D42 implementation on `codex/build-runtime-inspector-r1`, from UI base
`d7dd62ee661868401f4fd7604ca1a558fd50c32c`. Runtime wire models were read with
`git show` at core `7a7380a4058551ae1163fd180b35b6b74ec6d43b`. This records
synthetic UI checks; Manager acceptance is separate.

The inspector reads the discovered `runtime_get` operation only for the selected
agent, within selection and the existing visible-page poll. Its validated,
in-memory observation has independent loading/error state and cancellation on
selection, recovery, expiry and logout. Owner epochs use existing recovery;
ownerless responses carry no invented epoch. Failed observations clear prior
values, retain automatic retries and offer a replaceable manual Retry.

Session and Native history replace Current run and the generic Capture row.
They show last-reported observations, timestamps, retained unknown-owner identity,
collapsed native/run IDs, separate setup/collection states and pending counters.
Agent lifecycle/activity/status, Terminal recording, transcript publication and
Chat mutation behavior retain their existing authorities. No configured model is
presented as running and no state grants Chat delivery or complete coverage.

Checks on Node 22.22.0, npm 10.9.4, locked Playwright 1.64.0 and installed Chrome
153.0.8010.36:

| Command | Result |
| --- | --- |
| `npm ci --ignore-scripts` | Passed; lockfile unchanged, 0 reported vulnerabilities |
| `npm run build` | Passed, including TypeScript; final build log below |
| `QORQUT_UI_TEST_ARTIFACTS=<private>/chrome-r1 npm test -- --grep 'synthetic runtime inspector\|synthetic Terminal'` | 3 passed (45.8 s): new owning group and both existing Terminal groups |
| `QORQUT_UI_TEST_ARTIFACTS=<private>/chrome-r2 npm test -- --grep 'synthetic runtime inspector'` | 1 passed (47.7 s), after adding a full desktop Native history screenshot; production code unchanged |
| `git diff --check` | Passed |

The runtime group exercises disconnected/connected/unknown states; null
observations; Terminal/session/source waits; ready/collecting, blocked/gap,
unsupported, recovery-review and stale reasons; timestamps and counters;
zero-pending gaps; escaped provider version text; keyboard-operated collapsed
details; unavailable capability without requests/Retry; a refused read and
successful Retry replacing a held automatic retry; and held-read cancellation on
selection/logout. It rejects wrong agent/conversation, malformed UUID/date,
non-UTC timestamp, unknown enum, invalid generation/counter, input-ready true and
an invented top-level epoch. Owner-epoch recovery and a runtime-specific 401
retain drafts and selected view. Runtime read failures leave a live synthetic
Terminal connected and send no input or Chat mutation.

All three groups reported no page exceptions. Directly inspected desktop
1440×960 and narrow 390×844 screenshots show readable sections and escaped text;
the browser assertions found no horizontal page, inspector or drawer overflow.
The existing inspector scrolls vertically to its lower sections.

Private evidence root (`<private>` above):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-runtime-inspector-r1`.
Final build/test logs are `build.log` and `runtime-chrome-r2.log` there. Runtime
screenshots are under `chrome-r2/shell-synthetic-runtime-in-87334-and-preserves-Terminal-Chat/`:

- `runtime-desktop-connected.png`
- `runtime-desktop-native.png`
- `runtime-mobile-session.png`
- `runtime-mobile-native-gap.png`

The first pass's Terminal screenshots remain under `chrome-r1/`. Playwright used
fresh disposable browser contexts/profiles and synthetic API servers on assigned
loopback ports, with fixture/runner teardown. No live Qorqut data, owner native
homes/browser profiles, real model provider, VM or global service was used.
No native-workflow acceptance, full-suite acceptance, merge or deployment is
claimed. No dependencies, Terminal protocol/client/input files or core code were
changed.
