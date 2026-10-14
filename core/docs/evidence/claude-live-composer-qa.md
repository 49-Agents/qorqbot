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
