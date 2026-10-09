# D58 — Codex narrow composer verification

2026-10-09. Implemented the [Manager's D58 correction](../CHAT-COMPOSER-COMPATIBILITY.md)
on `codex/chat-composer-layout`, based on `0c332672bf4d4dfb80eb6d69347d425f49c232d3`.
Only the pinned input-layout adapter changes production behavior: Codex accepts
54–500 columns and the known positive-count warning footer; Claude retains its
80-column minimum. Exact composer, cursor, row, escape and post-paste checks
remain in force. These observations still grant no input permission themselves.

The freshly built and privately installed `qorqut_core-0.1.0-py3-none-any.whl`
has SHA-256 `6e3f572deeffbf3379809e9f869aaa84c5664f616e076e12a16ea642608d8180`.
Every installed core Python file matches the wheel and candidate source.
The unchanged private owner snapshot now classifies as `empty` at 69×68,
cursor (2,64), with `native_verified=false`. Only synthetic model/path labels
and the observed lower-row layout appear in tracked regression tests.

Focused installed-package command, with the private venv's Python:

```sh
PYTHONPATH=tests PYTHONDONTWRITEBYTECODE=1 python -m unittest \
  adapters.test_adapters.InputLayoutTests runtime.test_input_dispatch -v
```

All **31 tests passed** in 32.247 seconds: 10 input-layout tests and 21 runtime
dispatch tests. Coverage includes empty/exact 51-byte input with singular/plural
warnings, Codex 54/53 and Claude 79 boundaries, ordinary footers, and retained
draft, busy, dialog, cursor, wrapping, escape and unknown-footer refusals.

One disposable recorded-shell flow reused the completed first-Chat helpers and
local canned Responses provider. The real pinned Codex 0.160.0 executable
(SHA-256 `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad`)
was manually launched with a fresh synthetic home in a loopback-only namespace.
The terminal stayed at 69×68 and naturally displayed `1 warning · f2 to view`.
Normal HTTP Chat storage and dispatch produced exactly one submitted attempt,
one 51-byte inbox pointer and one native user echo. Actual native MCP
`messages_list` returned the exact Unicode/multiline body;
`messages_acknowledge` produced agent-attributed handling after its ordinary
one-call Allow prompt. The assistant reply and both user entries appeared in
the automatically published `2026-10-09.jsonl`. All 63,101 captured native bytes
matched their host archive; native entry spans and dated records were verified.

Private evidence is retained under `chat-composer-layout-r1/`, including
`provenance.json`, dependency/helper hashes, focused/build logs, the read-only
snapshot verdict, `08-completed-verified.json` and namespace cleanup. All owned
processes stopped, both fixture ports closed, and no namespace processes remained.

Limits: this used a canned provider, without paid inference or a UI build.
The fixture Terminal WebSocket encountered two keepalive timeouts; reconnecting
preserved the same native session and geometry, with no message/attempt replay.
Existing `unsupported_record`/`unsupported_item` parser gaps remain visible;
the verified raw archive and dated Chat projection do not claim full parser
coverage. No owner session, queue or configuration was changed. Merge and
deployment remain Manager-owned.

## Deployment and native helper repair

Manager merged candidate `0ae8929fd430d5443f730a5d9d43eb07f3734659` through
`758fdd1d2337b38fcf45c87d8922411f6207640e`, pushed main, and installed the wheel
above on the local test instance. Core restarted; the existing Bash, Codex,
terminal, inbox owner and browser lifetime stayed unchanged. No additional
review was performed, as requested by the owner.

The normal dispatcher then submitted the four previously deferred messages.
Read-only terminal observation confirmed Codex received all four inbox pointers,
but its tool calls failed because the deployed native package omitted
`codex-code-mode-host`. The matching helper was restored from the accepted
0.160.0 package beside the unchanged pinned executable. Its SHA-256 is
`37cab1584302611e9936902219640ab5e7a79fcfccd2504c6e85ea8cb97d0e10`.
Future native packaging must retain companion executables, not copy only `codex`.

A focused disposable check using that exact executable and a local canned
Responses endpoint reproduced a missing helper, installed it between tool
calls, and observed the next JavaScript tool call succeed in the same process.
A fresh invocation of the deployed executable also executed both tool calls.
No native restart is required for this repair. The check used a separate empty
home and no paid inference, owner credentials, inbox or terminal input.

Core readiness returned HTTP 200 and the original inbox owner remained
connected. The four earlier messages remain submitted and unacknowledged;
they were not replayed or marked handled. Live post-repair inbox handling awaits
the owner's next message. One transient Stop-hook diagnostic also reported
invalid output; this deployment does not claim that separate hook issue fixed.

Private installation, process fingerprints, queue observations and helper
checks are in `chat-composer-deployment/` and `native-helper-repair/` under the
existing build-state directory. These results establish this delivery repair,
not full-system acceptance.
