# Claude hook budget and non-continuing failure feedback R1

2026-10-14. Implementation of the Manager decision recorded in the
[NATIVE-CONNECTION amendment](../NATIVE-CONNECTION.md), against
`6aaec62` on `codex/fix-hook-feedback-a147d77a`.

## Changed

- `runtime/hook_client.py`: for Claude, a failed or conflicting result keeps the
  fixed stderr diagnostic and exit 0; stdout is `systemMessage` plus
  `additionalContext` for SessionStart only, `systemMessage` alone for
  UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure,
  PermissionRequest and Stop, and nothing for SessionEnd, unknown or non-string
  events. Codex output is unchanged. Named HTTP budget constants: Claude 1 s
  connect/write/pool, 8 s read, 8 s response elapsed; Codex 1 s, 1 s, 2 s.
- `adapters/manual_hooks.py`: `CLAUDE_HOOK_TIMEOUT = 10`, `CODEX_HOOK_TIMEOUT = 3`
  and `hook_timeout(provider)` for generation.
- `runtime/input_dispatch.py`: RuntimeInput's exact expected entries use the same
  `hook_timeout`. Admission, ownership, process proof, reconnect and delivery
  barriers are untouched.

## Checks

Focused owning tests on the edited source, run after the last edit:
`test_native_hooks.py` 9 tests, `test_input_dispatch.py` 21 tests and
`test_native_setup.py` 5 tests all passed. `test_native_setup.py` needs the
documented editable install: setup spawns `python -I`, which ignores
`PYTHONPATH`.

New or extended tests:

- Failure-output matrix: provider x conflict/unavailable x event, including
  SessionEnd, Interrupt, an unknown name and a non-string name; success outcomes are
  silent for both providers; a real submit failing with hostile bodies prints only
  the fixed diagnostic.
- Generated timeout upgrade: previous-generation timeout-3 Claude groups are
  upgraded to 10 with authored entries and settings preserved, the manifest
  follows, the result is idempotent, and an edited, extended or deleted owned
  group, or a later timeout edit, still refuses before anything is written.
  RuntimeInput refuses a stale or edited Claude timeout as `hook_unavailable`.
- Loopback budget over a real HTTP server: a valid startup result delayed 1.3 s,
  longer than the old 1-second client budget, succeeds under the Claude budget
  with exactly one request. The same answer to Codex is unavailable, still one
  request. Scaled Claude budgets end both a never-answering receiver and a
  never-completing trickled response as unavailable in about 0.4 s. This shows
  budget behavior, not the historical timeout cause or a real resume success.

Each of four reverted behaviors made the corresponding new test fail (Stop
context returning, Claude timeout 3, read timeout 1 s, elapsed check removed).

Installed wheel (`qorqut_core-0.1.0-py3-none-any.whl`, built with the pinned
build requirements, installed with the locked dependencies into a private venv):
`test_native_hooks.py` (9), the input-dispatch managed-hook-change test and the
normal-console setup test pass. The generated hook command then ran as a real
`python -I -m qorqut_core.runtime.hook_client` subprocess, killed at its generated
native timeout, against synthetic loopback receivers (exit 0 in every case):

| Case | Provider, event | Elapsed | Result |
| --- | --- | --- | --- |
| Valid result after 1.3 s | Claude SessionStart | 1.95 s | quiet, one request |
| Same receiver | Codex SessionStart | 1.59 s | unavailable, unchanged output, one request |
| Receiver never answers | Claude SessionStart, UserPromptSubmit, Stop, SessionEnd | 8.9-9.2 s | unavailable inside the 10 s timeout; SessionStart `systemMessage` + context, others `systemMessage` only, SessionEnd nothing |
| Incumbent conflict | Claude SessionStart, PermissionRequest, Stop, SessionEnd | 1.1-1.2 s | conflict wording as context only on SessionStart |
| Connection refused | Claude Stop, SessionEnd | 0.7 s | `systemMessage` only; nothing for SessionEnd |

Elapsed times include interpreter and import start-up, about 0.7 s here and over
1 s with four concurrent hooks. Raw outputs are in the private QA evidence
directory, outside Git.

## Limits

- The elapsed and read checks run inside the sink and are bounded by the native
  timeout; they are not a hard real-time guarantee. In the never-answering case
  the sink finished about one second inside the 10-second timeout, so start-up
  slower than that makes Claude time the hook out first.
- An already running Claude can keep its earlier hook snapshot, with the old
  3-second timeout, until the user resumes it. Nothing restarts it.
- A timed-out startup stays unavailable; there is no reconnect from later hooks.
- No native CLI, paid inference, owner data or live service was used, and no
  fixture or service was changed. The integrated terminal-contention fix still
  needs its separate retest.
