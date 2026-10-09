# D59 core evidence — 2026-10-09

Implemented on `codex/native-attention-core`, based on
`0e67c83a4147b2f75b5e1169fb9405849f99a689`, following the frozen
[D59 contract](../NATIVE-ATTENTION.md) and Manager clarification 403.

`NativeAttention` runs in SessionMonitor's existing loop. Its read projection
uses SQL and bounded cached data, with five-second monotonic freshness and exact
owner/terminal/recovery binding. `RuntimeInput.attention_snapshot` reuses the
owned workspace, saved recorded shell and native ancestry checks under the
terminal lock, verifies again after capture, and calls only inspection methods.
It does not call the input-readiness path or the shell binding-adoption path.
Human control and a waiting hook do not prevent observation.

The additive RuntimeView field has exactly D59's seven fields. Fresh native waits
are ORed with unresolved browser attention and project `waiting_user` plus its
observation time; authored status is preserved. Only normalized state, prompt or
owner transitions append `native.attention_changed`; timestamps, option selection
and typed answer text do not manufacture changes. Input readiness remains false.
Host schema **10**, **24** public operations and **6** job handlers are unchanged.

## Actual profile coverage

- **Codex 0.160.0 MCP approval:** Manager's saved read-only 69×68 capture,
  `Field 1/1`, four Allow/Cancel choices, reverse-video selection and submit/cancel
  footer. The committed regression keeps only the form, with synthetic tool and
  argument text; owner IDs and conversation content are excluded. No owner
  terminal was accessed or answered.
- **Codex 0.160.0 synchronous Plan-mode questions:** disposable canned-provider
  captures at 80×40 and 69×68. Covers ordinary choices, selection changes,
  editable notes/“None of the above”, switching between two questions, wrapped
  action footers and the confirmation for submitting unanswered questions.
  The classifier exposes the visible current question and options, not hidden
  questions or typed answers.
- **Default-mode asynchronous question:** the actual advertised
  `request_user_input_async` returned immediately and rendered transcript bullets.
  Those are not an active form and do not create a native waiting notice.
- A synchronous question with an empty options array was rejected by Codex with
  `request_user_input requires non-empty options for every question`.
  Free text is covered through the observed notes/“None of the above” form;
  standalone free-text-only questions are not claimed.

Other providers/versions and unrecognized interactive forms remain unknown.
Shell approval profiles, async-question tracking and unsupported setup dialogs
are not claimed. No Claude, browser or VM probe was resumed.

## Checks and installed flow

Private evidence root (`V` below):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-attention-core-r1`.
Raw captures, native stores and synthetic fixture configuration remain there.

The affected source check ran:

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 "$V/venv/bin/python" -m unittest \
  runtime.test_input_dispatch runtime.test_owners runtime.test_transports \
  runtime.test_monitor agents.test_registry agents.test_agent_flow -v
```

39 of 40 passed initially. The remaining agent transport test had an obsolete
16-tool expectation; it now checks the existing 24-tool inventory. Its final
installed-package run passed. A first new transport assertion also needed to
allow an unknown observation to retain the current owner IDs; both null and
current IDs satisfy D59. Final installed checks:

```sh
PYTHONPATH=tests PYTHONDONTWRITEBYTECODE=1 "$V/venv/bin/python" -m unittest \
  runtime.test_native_attention runtime.test_transports agents.test_agent_flow \
  browser.test_requests.Requests.test_exact_facts_one_card_dated_publication_and_terminal_notification -v
```

**11 passed**. These include classifier bounds, history false positives, prompt
change/clear, stale/failing observations, owner replacement/recovery, browser OR,
cached operation reads, human-held observation and no input effects. The browser
case uses its existing deterministic HTTP fixture; it does not launch a browser.
`git diff --check` passed. No full-suite or extra review run was performed.

`"$V/venv/bin/python" -I "$V/installed-flow.py"` passed with installed candidate
bytes, a fresh core installation, recorded shell, pinned native binary/helper,
loopback canned provider and no model inference. Terminal control remained human
while the server observed the actual question. HTTP RuntimeView and agent
directory changed to waiting/question, then none after the fixture answered in
its own Terminal. The exact owner persisted and input readiness stayed false.
Events were unknown → none → waiting/question → none, without poll duplicates.
The initial attempt stopped at native hook onboarding; the successful attempt
trusted only the candidate-generated hooks in its disposable HOME. No production
preparation or hook behavior was changed. Owned test processes were cleaned up;
the provider port and discovery tmux server are unavailable afterward.

Wheel built with `python -m pip wheel . --no-deps --no-build-isolation`:
`$V/dist/qorqut_core-0.1.0-py3-none-any.whl`.
SHA-256: `2f6ced22ba4859f286b9a74a0d35783a5f2b1413507d64e9c2a3372f198cb476`.
All **93** package files match both the worktree and the installed wheel bytes;
`installed-provenance.json` records every file hash and import location.

No hands-on browser/UX check was performed: the owner's built-in Chrome bridge
was unavailable, and no replacement browser tooling was used. This is core
acceptance only. Integration, live read-only observation and deployment belong
to Manager; this worker has not merged or deployed.

## Integrated local deployment

Manager merged and pushed core `27f61d503ce4025b90ab03d3d7d34458c88dd937`
and UI `fd96ee4140bc0ddaa69a15cd6355fd3e17b3a80e`, then deployed the completed
wheel and built UI on the local test instance on 2026-10-09. The owner waived
additional review; the focused checks above are the builder's evidence.

Read-only live verification found the existing MCP approval with attention
`waiting/approval`, a connected matching owner and `needs_user_input=true`.
The core readiness endpoint returned 200. The public application route served
the exact new UI index, and both new JavaScript/CSS assets matched their build.
The original Bash and Codex process identities, terminal, inbox owner and browser
lifetime survived the core restart. No terminal input, approval or message was
sent by Manager.

Codex's built-in browser inventory still returned no browsers. Actual rendered
Chat layout and clicking Open Terminal remain unverified; API/static-asset
verification is not a hands-on UX test. Default-mode asynchronous questions,
shell approvals and unrecognized native layouts remain outside this profile.
Private deployment and continuity evidence is in `native-attention-deployment/`
under the existing build-state directory.
