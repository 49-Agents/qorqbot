# Claude 2.1.287 live composer QA

2026-10-14. The independent live-agent tester started two ordinary agents,
launched pinned Claude Code 2.1.287 (Sonnet 5.5) by hand in each recorded shell,
and connected them through the real startup hooks. The real model answered Terminal
input and its output reached Chat, but Chat readiness messages stayed deferred with
no delivery attempt: `classify_input` returned `ambiguous_or_misplaced_composer`.
The correction is [amended in the composer note](../CHAT-COMPOSER-COMPATIBILITY.md#2026-10-14-amendment--claudes-floating-input-box);
base `bda02be52cb572c7801f4c33ddc4958ac4f98c73`.

## Offline diagnosis

The Manager saved two read-only snapshots (no input sent) and varied one cause at a time.

| | A (first turn) | B (after a turn) |
|---|---|---|
| geometry / cursor | 106×52, hidden (2,12) | 106×24, hidden (2,18) |
| rules / prompt / footer | 11, 13 / 12 / 14, then blanks | 17, 19 / 18 / 20, then three blanks |
| empty tail | styled `Try "fix lint errors"` | inverted-space cell |
| old verdict | ambiguous composer | ambiguous composer |
| box moved to old position only | `blocked` (the suggestion) | `empty` |

So the old fixed `rows - 3` row was one cause and the first-turn suggestion the second.
Snapshot file SHA-256: A `783ac02192a5fa04172b31018a7771e1d7cd54ac2180d3adf090b9aa5e99dcbc`,
B `73913778f942b954f172aed1018af0387bb4bd31959ed15893cacd1d3625cb20`.
The tracked fixture `tests/adapters/fixtures/claude-floating-composers.json` keeps the
geometry, cursor, flags, local notices, rules, prompt, footer and blank rows verbatim.
Only history rows above the box (which held an agent id and a workspace path) are
replaced with synthetic text; each capture lists them in `sanitized_rows`.

## Change (pure `adapters/input_layout.py` only)

Claude's row is the validated `cursor_y`, with exact rules on both sides, the existing
footer two rows below and blank rows after it; the busy/dialog/prompt window is
`max(0, row - 5)` to the end; the observed styled suggestion (inverted `T`, dim `ry`,
dim quoted words) is accepted as empty for `expected_text=None` only. `matches_input`
and the cursor-cell comparison are unchanged, `native_verified`/`permits_paste` stay false.
No runtime, hook, queue, retry, authentication, provider, UI, Codex, legacy composer,
migration or dependency change.

## Checks

Private venv from `requirements.lock` (Python 3.12.3). Commands run from `core/`:

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  adapters.test_adapters.InputLayoutTests runtime.test_input_dispatch -v
```

Source tree: **37 tests passed** in 85.1 s (16 input-layout, 21 runtime dispatch).
Six input-layout tests are new: both observed boxes classify empty without paste
authority; other quoted suggestions in the same style; exact 51-byte notification at the
relative rows; refusal of 34 unstyled/differently styled/extended/non-ASCII/empty-body
suggestion edits and wrong cursors; extra footer/tail rows, multiline input, wrong or
missing rules, ambiguity, busy/dialog in the local window, flags, versions and geometry
(output above the window stays history); and a box with no room for its rules/footer.
The ten pre-existing input-layout tests and all dispatch guards (first turn, control,
capture, uncertainty) pass unchanged. Against the base source the new tests fail (62
failing subtests); temporarily weakening single rules (tail check, window start/size,
hint text, width, reset/style shape, the expected-text guard) each fails them.

Wheel, built from a copy of `core/` with `pip wheel --no-deps --no-build-isolation`:
`qorqut_core-0.1.0-py3-none-any.whl`, SHA-256
`3edd5b1bdc380ffb96d1ff0bf534a2c2d62268e7b76af05d46e68271b61e8e81`; installed
`adapters/input_layout.py` SHA-256 `9572f0cbcee342aa17213855b4d7f5a810f0ff03e34d0292e3dd6d6266a93a7f`
matches the source. Installed in a fresh venv (not the source tree), the same command
with `PYTHONPATH=tests` passed **37 tests** in 79.3 s.

Read-only installed-wheel check (`python -I`, snapshots opened read-only): both original
private snapshots now classify `empty` with reason
`native_snapshot_empty_layout_live_unverified`, `native_verified=false`, unchanged
fingerprints, and `matches_input` false for the notification and for `Try "fix lint errors"`.
A **constructed** shape (prompt row rewritten to the exact 51-byte pointer plus the
inverted-space cell, cursor x=53) matches for both and a one-cell wrong cursor does not.
That construction is not live paste evidence. Output:
`/home/alp/qorqbot-launch/qa/2026-10-14/acla-browser-a147d77a/fix-claude-composer/evidence/installed-classification.json`.

## Limits

No service, tmux pane, database or native session was touched, and nothing was pasted.
Real delivery, Enter, native receipt and handling are not claimed: the existing live QA
tester verifies those after the Manager installs the wheel. The suggestion is accepted
only in the observed printable-ASCII `Try "…"` shape; other hints, locales or CLI versions
refuse until observed. Transient Stop-hook failures seen in snapshot B's history are
separate and not addressed here. The full repository suite was not run. Private logs, venvs
and wheel are under `/home/alp/qorqbot-launch/qa/2026-10-14/acla-browser-a147d77a/fix-claude-composer/`.

## Follow-up: post-turn promotional hint

2026-10-14; base `eb6476d847eed99a04496cff8ada12307abcbc9f`. After the repair above the original
queued READY-A message reached real Claude and produced a real reply in Chat without a
resend, but agent B's original message stayed deferred with zero attempts. The Manager
captured B read-only (no input sent): 106×55, hidden cursor (2,50), rules at 49/51, the
existing mode footer at 52, a right-aligned `control this session from your phone · /remote-control`
row at 53 (exactly 50 leading spaces, visible text ending at column 104) and a blank row 54. The
old classifier returned `unknown_footer_or_multiline_input`; removing only the hint row gave
`empty`. A's current post-turn pane has no hint row and stays `empty`. Original snapshot SHA-256:
B `2f43b36455cc8e132cd68a4c6e504cf0b90bb603892275802192a3d6a5ee1e8d`,
A `8f8ca349f0b023aaa42b6510e6eca9a7d9a8378759e243e8cf71f5a55c274b28`.
The new fixture entry `post-turn-remote-control-hint` keeps rows 47–54, geometry, cursor and
flags verbatim; the 41 non-blank history rows above (0–45) are replaced with synthetic text.

**Change** (pure `adapters/input_layout.py`): after the unchanged mode footer, at most one row equal to the
exact hint text right-aligned to `columns - 2` is skipped; every other row must be blank.

**Checks** (private venv from `requirements.lock`, Python 3.12.3; `core/`; existing group only,
no full suite and no runtime suite — runtime is unchanged):

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 python -m unittest adapters.test_adapters.InputLayoutTests -v
```

Source tree and, from a fresh venv, the installed wheel (`PYTHONPATH=tests`): **18 tests passed** each (16 earlier;
two new). The new tests cover the sanitized observed pane (empty, no paste authority, hint-removed
counterfactual equal), the constructed exact 51-byte pointer with the hint (matches; extra text, ±1 cursor,
visible cursor and a draft refuse) and, labeled constructed rather than observed, 80/106/160-column alignment with
the hint, a hint as the last row and no hint. They refuse a hint with no/one-column-off margin, left alignment, trailing
space, changed/extra/shortened text, case or separator, a wrapped hint, a displaced or duplicate hint, any visible row
after it, unrelated status rows, and an unknown footer or a hint in place of the footer. Against the base
source the new tests fail; each of six single-rule weakenings (margin 0 and 1, hint accepted on any row, whitespace
trimming, no check after the hint, suffix-only hint match) also fails them. Existing busy/dialog, Codex and other
Claude cases pass unchanged.

Wheel built from a copy of `core/` (`pip wheel --no-deps --no-build-isolation`):
`qorqut_core-0.1.0-py3-none-any.whl` SHA-256 `9c9ace4e4f14369810f12244ab20eb7929b8b1d78ec4b462554ae3ed3d41cce8`;
source and installed `adapters/input_layout.py` SHA-256 `a9db8161eaf003fa6e8d573b2c5d417582acb69c3c76ceb15d2487ba2a0d20cd`.
Read-only installed check (`python -I`, snapshots opened read-only): original B now classifies `empty` /
`native_snapshot_empty_layout_live_unverified` (previously `unknown_footer_or_multiline_input`), `native_verified=false`,
fingerprint unchanged; its hint-removed counterfactual has the Manager's recorded fingerprint; A remains `empty` with
its recorded fingerprint; `matches_input` is false for the notification on both originals. A **constructed** pointer
shape (prompt row rewritten to the exact 51-byte pointer plus the inverted-space cell, cursor x=53) matches for A and B
while a one-cell wrong cursor and a draft do not; constructed displaced/duplicate/extra-row/changed-text/no-margin
variants of B refuse; constructed 80/106/160-column hint rows classify `empty`, match the exact pointer, and refuse at the
right edge. Private outputs: `/home/alp/qorqbot-launch/qa/2026-10-14/acla-browser-a147d77a/fix-claude-footer/evidence/`.

**Limits.** No service, tmux pane, database, owner session or provider was touched; nothing was pasted or resent and
no queue was drained. The constructed shapes are not live paste evidence, and 80/160-column alignment is constructed,
not observed. Real delivery, Enter, native receipt and handling are not claimed; the live QA tester verifies them after
the Manager installs the wheel. Only this one observed hint text is accepted: other hints, locales or CLI versions refuse
until observed. The hint is not remote control being activated and this change never activates it. The runtime banner,
delivery badge and Stop-hook reports are separate and not addressed here. The full repository suite was not run.

## Follow-up: dim post-turn suggestion

2026-10-14; base `f134066d22acfa1e6c11d2176f7ff81e43b1d1b0`. After the two repairs above, automatic Chat-to-A and A-to-B
delivery completed on real Claude 2.1.287 agents, and B's real reply to A stayed deferred as `composer_blocked` with no
input attempt. The Manager captured A read-only (no input sent): 106×55, hidden cursor (2,50), no mode/input-off/synchronization,
full rules at 49/51, the known mode footer at 52 and blank rows 53/54. The active row is the normal `❯`+NBSP prompt, then an
inverted `c` (`SGR 7`), the dim remainder `heck whether the relay replied` (`SGR 0;2`) and a reset: a ghost suggestion, unlike
the first-turn `Try "…"`. The old verdict was `blocked` / `composer_contains_text_or_unknown_cursor_cell`; replacing only that
row with the known empty cursor cell offline gave `empty`. Original snapshot file SHA-256
`bfd83ca41f7b24480cd4e654049d20f12ffa43b4347ea089e698adb047c9166e`, screen `199bd79441b23e0490dfc8acc18ae2b1d654fe90b00f3378f5e2965153089ad6`.
The new fixture entry `post-turn-dim-suggestion` keeps rows 46–54, geometry, cursor and flags verbatim; the 31 non-blank history
rows above (0–45) are replaced with synthetic text (derived screen `e0943f4b4e941bf4b8b8020e02440354669be6005e39297a1fde200e173f69a9`).

**Change** (pure `adapters/input_layout.py`): for the empty check only, the tail may also match, in full,
`SGR 7`, one printable non-space ASCII character, `SGR 0;2`, a nonempty printable ASCII remainder ending in a non-space, `SGR 0`,
within the row (`2 + length <= columns`). The wording is not checked and the first-turn shape is unchanged.

**Checks** (private venv from `requirements.lock`, Python 3.12.3; `core/`; existing group only, no full suite and no runtime
suite because runtime is unchanged):

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 python -m unittest adapters.test_adapters.InputLayoutTests -v
```

Source tree and, from a fresh venv, the installed wheel (`PYTHONPATH=tests`): **19 tests passed** each (18 earlier; one new
grouped test). It covers the sanitized observed pane and its known-empty-cell counterfactual (equal verdicts, no paste authority);
varying wording and the row-width boundary at 106 and 80 columns; ordinary typed text with the cursor at its start, middle and end,
unstyled, dim-only, reverse-only and other styled or colored variants; extra SGR, cursor cell, prefix, suffix, trailing space,
non-ASCII and control characters; wrapped, multiline and displaced rows; bad cursor visibility and position; layout, version, busy,
dialog and Codex refusals; and that a suggestion never satisfies `matches_input`, even when its visible text is the exact pointer or
its cursor is at the pointer's end, while the exact pointer plus the empty cursor cell still matches. Against the base source the new
test fails, and so does each of 13 single-rule weakenings (width check, trailing space, inverted space, empty remainder, two inverted
characters, loosened dim, reset or reverse sequences, SGR or non-ASCII inside the text, prefix-only match, tolerated suffix, a suggestion accepted for
expected text). Existing first-turn, footer, busy/dialog and Codex cases pass unchanged.

Wheel built from a copy of `core/` (`pip wheel --no-deps --no-build-isolation`): `qorqut_core-0.1.0-py3-none-any.whl` SHA-256
`05a7a2b7806ae86bb800544021a54b9535e4acb33ce2eb11382e04e0ba34ed89`; source and installed `adapters/input_layout.py` SHA-256
`ec92c310fc672bec70383b82c3e4e386f50c86fde2566ce056379e41c8497034`. Read-only installed check (`python -I`, snapshot opened
read-only, hash and mtime unchanged): the unmodified snapshot now classifies `empty` / `native_snapshot_empty_layout_live_unverified`
(previously `composer_contains_text_or_unknown_cursor_cell`), `native_verified=false`, fingerprint unchanged and equal to the
recorded one; the known-empty-cell counterfactual has the Manager's recorded fingerprint; `matches_input` is false for the pointer
and for the visible suggestion. A **constructed** exact 51-byte pointer plus the inverted-space cell (cursor x=53) matches while a
one-cell wrong cursor, a visible cursor and a draft do not; a constructed dim suggestion whose visible text is the pointer classifies
`empty` but never matches; constructed typed text at the cursor start, unstyled text, an extra suffix and a moved hidden cursor
refuse. Private outputs: `/home/alp/qorqbot-launch/qa/2026-10-14/acla-browser-a147d77a/fix-claude-suggestion/evidence/`.

**Limits.** No service, tmux pane, database, owner session or provider was touched; nothing was pasted, escaped or resent and no queue
was drained. The constructed shapes are not live paste evidence. Real delivery, Enter, native receipt and handling are not claimed;
the live QA tester verifies them after the Manager installs the wheel. Only this printable-ASCII, single-row shape is accepted:
other suggestions, styling, locales or CLI versions refuse until observed. Providers' suggestion settings are not changed. The
full repository suite was not run.
