# Claude question visibility evidence — 2026-10-14

Branch `codex/fix-claude-attention-a147d77a`, base `6aaec62`. Implements the
[Claude question amendment](../NATIVE-ATTENTION.md#amendment--claude-code-question-form)
in the existing pure `classify_attention`; the observer, `runtime_get`, UI and
input paths are untouched.

## Actual coverage

One read-only live Claude Code 2.1.287 `AskUserQuestion` snapshot (106x55, cursor
hidden at the selected row), saved by the Manager during QA. The sanitized fixture
`core/tests/adapters/fixtures/claude-attention.json` keeps rows 40-54 verbatim,
SGR included, with the 40 history rows above them blanked; it records the original
file and screen SHA-256 values but no conversation, IDs, store or process paths.
Nothing else was captured: option 2-4 selection and every negative variant in
`test_native_attention.py` are labelled synthetic edits of it.

## Checks

- Owning module once after the final edits, from source (`PYTHONPATH=src:tests`,
  private venv from `requirements.lock`): `runtime.test_native_attention`, 12
  tests OK. Four are new: actual form and selection normalization; known
  composers (all four Claude fixtures, plus the answered form replayed as
  scrollback) give `none`, a typed composer and every Codex capture passed as
  Claude stay `unknown`; 22 impostor/malformed/unsupported-variant refusals;
  the 6000-character bound.
- Wheel built from the same tree (`--no-deps --no-build-isolation`) and installed
  into a private venv. `python -I` from outside the checkout classified the actual
  form `waiting`/`question` with the exact visible prompt, matched after a
  synthetic selection move, cleared to `none` on the known post-turn composer, and
  refused the unstyled copy, version 2.1.288 and a Codex capture. The installed
  `attention.py` SHA-256 equals the source file's.

Raw logs, the wheel and the check script are kept with the QA run, outside Git.

## Limits

No paid inference, model launch, owner session, shared fixture or service was
used. The whole suite was not run. Live integrated acceptance is not claimed:
the Manager validates the still-open real question after integration. Multi-
question tabs, free-text editing, approvals and other layouts stay `unknown`.
