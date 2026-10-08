# Codex placeholder reset placement correction

Implemented the [fixed Manager decision](../INPUT-PLACEHOLDER-CORRECTION.md)
on branch `codex/fix-codex-placeholder-reset-r1`, based on
`b8128c0e0a88e7d225efaca7f9dfd0dd7147ecc5`.

Only the Codex empty-placeholder branch in `adapters/input_layout.py` changes.
The existing exact dim/text/reset tail remains accepted. The exact dim/text
tail without its trailing reset is also accepted when the next raw row is
exactly empty and the model/footer raw row starts with `ESC[0m  `. Existing
layout, cursor, version, mode, footer and control checks still apply.
`matches_input`, `native_verified` and `permits_paste` are unchanged.

The existing `InputLayoutTests` group gains one compact regression using its
sanitized lower-row fixture at 104 columns by 50 rows. It checks both reset
placements, missing/partial/misplaced footer resets, spaces/text/SGR on the
intermediate row, plain/styled human text, unstyled/altered placeholders and
cursor mismatch. Existing shared refusal checks remain in the same group.

Validation used the existing private `console-test-venv` unchanged
(Python 3.12.3, Pydantic 2.12.3); no dependency changes or installations.
Commands ran from the assigned worktree:

```bash
run_state=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04
test_python="$run_state/console-test-venv/bin/python"
replay_dir="$run_state/fix-codex-placeholder-reset-r1"
PYTHONPATH=src:tests/adapters "$test_python" -m unittest -v test_adapters.InputLayoutTests
PYTHONPATH=src "$test_python" "$replay_dir/replay.py" empty corrected-replay.json
git diff --check
```

All 7 owning tests passed; the exact original replay and whitespace check
passed. Before the correction, all 6 existing tests passed. The new regression
against unchanged production code failed its deferred-reset positive and
cursor-reason assertions. Private logs are `baseline-tests.log`,
`regression-before-fix.log` and `focused-tests.log` under `replay_dir`.

Original completed evidence remains under
`$run_state/connected-chat-codex-r1/evidence/`. SHA-256 values:

| Original artifact | SHA-256 |
| --- | --- |
| `blocked-classifier.json` | `c08212819b46cdab5e543f6005abf8678925ea3257c4888018fc2e5f5d576332` |
| `blocked-screen.sgr` | `6711533eb1d04c5c2a12a3263cf20ea224943831eef09e8775baa01d2199c67b` |
| `blocked-flags.raw` | `d3727947ff43e8cf401ea53d270e89e62c3f0b8ecce5d8852d6fabbf5ba2ca44` |

The supplied `c082…` hash identifies the classifier JSON containing the original
snapshot; the separate SGR screen has the `6711…` hash. The replay validates that
the JSON snapshot screen equals the SGR bytes and that all flags match the raw
flags artifact. Geometry is 104x50 and cursor is `(2,46,true)`.

Private `replay.py` reads those originals without changing the snapshot, verifies
their bytes remain unchanged, and writes separate derived results:
`baseline-replay.json` records `blocked/composer_contains_text_or_unknown_placeholder`
at the base; `corrected-replay.json` records
`empty/native_snapshot_empty_layout_live_unverified` after the fix. Both retain
fingerprint `d5ff2fd7bdb1a7295b38607776d014060390b1d5d5811649c3501f26f3adfdb6`.
Both separately assert `native_verified=false`, `permits_paste=false` and
`matches_input=false` for a synthetic canonical inbox notification.

The earlier `$run_state/manager-review309/replay.json` remains unchanged
(SHA-256 `cc60cce45336c6ec80c43a27e25b818c85d958711fc141eeea821d3e322d7029`).
Its appended prompt-row reset is a diagnostic derived copy. This correction's
replays use the original snapshot without that edit. Private screens/history,
scripts and derived replay results are not committed.

This is pure layout correction evidence, not actual Chat-delivery acceptance.
No actual CLI, live input, full suite, UI build, owner data/settings/services,
runtime readiness, schema, merge or deployment was involved. Manager review and
the repinned connected Codex flow remain separate acceptance work.
