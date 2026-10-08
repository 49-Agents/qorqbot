# Connected Codex Chat r1 — BLOCKED

2026-10-09 UTC. The pinned product refused the first automatic Chat notification
because its empty-composer classifier rejected the actual Codex SGR layout.
**No automatic delivery attempt occurred. Connected-flow steps 3–7 did not
execute and did not pass.** Manager confirmed the defect and directed completion
of this bounded blocked report in review message 310, answering question 309.
This report records observations; Manager owns acceptance and subsequent work.

The assignment follows [Connected Chat acceptance](../CONNECTED-CHAT-ACCEPTANCE.md).
Only this file is tracked on `codex/verify-connected-chat-codex-r1`. Private
evidence root (`$RUN`):

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/connected-chat-codex-r1
```

## Exact inputs and containment

| Input | Verified identity |
| --- | --- |
| Core source / installed code | `99a63b6aac134b087b59ef3d2b924f69cf25e40a` |
| UI source | `30bf7181127a5bac4ff7c5fa6297112b63a84ea1` in `/home/alp/qorqutbot` |
| Core wheel SHA-256 | `e6eb01d44c990be1c86ce5ecc719c2cbc85add50e23f4e900ce9c86cadc4d2f1` |
| Core lock SHA-256 | `082ab8e50044aaad21ca1da983eeae50e6240cfd817023cb751243825f4b708a` |
| UI lock SHA-256 | `1c07d1422b8271985fe16bf8761326b0078d18ddfbfa4f8ef682e00cd778e3de` |
| Actual native version / executable SHA-256 | `codex-cli 0.160.0` / `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |
| Supporting programs | Python 3.12.3, tmux 3.4, Chrome 153.0.8010.36, Node 22.22.0, locked Playwright 1.64.0 |

Both sources were exported with `git archive`. A fresh private virtual environment
reused compatible copied dependencies from the completed manual-Codex fixture;
all 32 unchanged lock pins were checked. `pip wheel --no-deps
--no-build-isolation` built the exact exported core, followed by private
`pip install --no-deps`. All 85 packaged files match source, wheel and installation;
all 279 exported core and 71 exported UI files remained unchanged. Normal console
logs prove 81 core modules loaded exclusively from the new installed package,
without `PYTHONPATH`, and completed startup/shutdown.

The reused UI artifact came from the completed
`file-reference-verification-r1/ui-source/dist`, built at
`3d5e5e7483d1e5a1eb9ca1814b2fe1faf85ee26e`. Git differences to the assigned UI pin
are documentation only. All 25 non-documentation tracked files, including the
lockfile, matched the artifact's source. The copied four distribution assets
matched the real server's returned bytes. Original build-log/provenance hashes,
file comparisons and served asset hashes are in `ui-reuse.json`, `provenance.json`
and `evidence/01-login-served-assets.json`.

Helpers were copied from completed `manual-native-codex-r1` and
`explore-native-input-r1`; their originals were hash-checked unchanged. The pinned
native launcher is the accepted `native-rehearsal-r1-validation/codex-pkg/shim/codex`;
the exact resolved vendor binary is recorded in `evidence/versions.json`.
No other actor's unfinished work was inspected.

Fresh user, network, PID and mount namespaces contained core, native CLI, canned
provider and Chrome. Only loopback was up; core/provider used 18561/18562.
Private HOME, tmp, data, native stores, browser profile and generated fixture
credentials were used. Secret values were read from fixture files rather than
typed into Terminal. Exact-byte scans found none of the three generated secrets
in evidence or logs. No owner data, real provider credentials/inference, external
route, global configuration, service, VM or browser takeover was used.

The normal installed `qorqut-core serve --installation … --transport-config …
--ui-root …` ran its ordinary workers and monitor with official installed hooks
and managed MCP configuration. Schema 9, seventeen public operations and five
handlers were observed. There was no direct job pumping, fabricated owner/idle
event, SQL mutation, manual native collection/projection or intercepted core
HTTP/WebSocket/MCP response.

## Observed flow and blocker

Real UI sign-in established an HttpOnly, SameSite Strict session cookie.
`02-owner-free-shell.png` shows provisioned recorded Bash, ready managed setup,
no connected owner and waiting native capture. Creation did not start Codex.
Explicit acknowledged Terminal takeover preceded keyboard input.

The manual command retained accepted `--no-daemon` and loopback `qfake`
Responses-provider arguments, without an initial native UUID or native trust
override. The actual eight-hook dialog was answered with “Trust all and continue”;
the bundled-bubblewrap warning was viewed and dismissed. The first manual seed,
`Connected Codex seed.`, was the previously qualified prerequisite for Codex
source/owner discovery. Its canned reply was `Connected Codex seed reply.`
These are Terminal seed entries, **not a reply to the stored Chat message**.

| Identity | Value |
| --- | --- |
| Agent | `48b70685-5242-497a-9d02-f9a6cd06c7ba` |
| Agent actor | `7264cd60-1ca9-4b70-9892-9b30bdfbd3c4` |
| Owner actor | `f012a6eb-7d32-48ae-9f4b-d37aa4bbf668` |
| Conversation | `258c45da-e760-436c-9b51-9e18d7c86fa8` |
| Recorded terminal | `453b8eca-858e-44c8-900d-c7f8507e05c2` |
| Native session | `01a11e99-d33c-7b21-b215-e7be78ef5c18` |
| Admitted owner/run, generation 1 | `2e369d03-1474-441a-865c-444f56eb760e` |
| Native fingerprint | PID 2810, start ticks 209214629, PID namespace 4026532978 |

Actual SessionStart, UserPromptSubmit and Stop hooks were attributed to the agent
actor, with matching session/process/run/terminal bindings. The last accepted
Stop was event 16 with activity `idle`. Normal primary capture and terminal
recording were active. Public `input_ready` remained false, as required; that
literal value was not treated as failure.

With human Terminal control retained, a separate real Chat page saved message
`fef5ce1c-6502-4b92-a836-cbf05448c20a`, thread
`383910d9-d5a1-4821-9ebf-d4506491ca53`, at
`2026-10-09T03:00:02.071171Z`. Its exact synthetic body, using JSON escapes for
the literal LF and TAB, is:

```json
"Connected Codex 1 80b9da20-43f1-4c8f-b9e7-7f39c4cf179d: café λ 漢字 🧪\nLine two\tTAB retained."
```

The 99 UTF-8 bytes have SHA-256
`8a2a8a310233071941d15ac8b8c60d705a9d836cc50d0e88eddac0fce623601f`.
`deliveries_get` returned `deferred/human_control`, `attempts=[]`; no handling or
corresponding native user record existed. The dated file already contained the
unchanged body. UI Release control advanced the control epoch from 1 to 2 and
left an empty composer. Ordinary dispatch then returned `deferred/composer_blocked`
at `2026-10-09T03:00:35.114129Z`, still with no attempt. No saved dispatch target,
automated runner receipt, notification echo or Chat assistant reply exists.

The original unmodified `tmux capture-pane -p -e` snapshot has 104 columns,
50 rows, cursor `(2,46,true)` and false `in_mode`, `input_off` and `synchronized`.
The actual prompt tail is `ESC[2mAsk Codex to do anything`; its reset appears at
the start of the model/footer row after a blank row. At the pinned source,
`src/qorqut_core/adapters/input_layout.py:113` requires a same-row terminal
`ESC[0m`. Actual `classify_input` returned
`blocked/composer_contains_text_or_unknown_placeholder`, fingerprint
`d5ff2fd7bdb1a7295b38607776d014060390b1d5d5811649c3501f26f3adfdb6`.
Original screen/flags and the derived read-only classifier result are retained
as `evidence/blocked-{screen.sgr,flags.raw,classifier.json}`.

The canned provider was extended privately to request only native MCP
`messages_list` and `messages_acknowledge` plus fixed replies. **That scripting
was never exercised:** no notification reached the native CLI. Its bounded
25-second wait for the first five-second response delay expired. The conditional
second message was never sent; neither was the restart message. The provider
received only the seed request and its native title request. There are no actual
MCP read/ack response bodies or semantic handling results to report, and no SDK
call was substituted. Steps 3–7, including D48 reopen/reconnected dispatch, remain
unexecuted. No resize workaround, hotpatch, forced notification, draft clearing,
new idle event or workflow repetition occurred.

## Preserved history and UI evidence

The primary source `449cbe85-c55b-4192-a249-147fed9803a1`, epoch
`79d3bc73-e79a-41e6-b48c-e355d10343e0`, has 50,952 saved bytes, SHA-256
`20da621ca7b3ae527fc654927d858da38347990d87bfcd122b0db3e7949d85c1`.
The final original native file equals its saved copy and reconstructed immutable
host chunks. Each chunk and the following native spans were checked:

| Entry | Entry ID | Raw offset + length | Raw SHA-256 |
| --- | --- | --- | --- |
| Manual seed user | `ed95aa83-dfbb-4357-bc57-0c4a6c32aa29` | 47671 + 473 | `12f278ebea0382f477e866e5f938bcd37dbf48bfd04e8ca8e2ec0fb276d20570` |
| Seed assistant | `4ffb3532-b7fe-45a5-84ab-ec777f598353` | 48144 + 403 | `1ec6fd62536cc9b4d397df19de11e84f3f91cfd29504b75f2d2e4e3e86ace5c1` |
| Stored Chat | `772baf7b-557a-48a6-a00f-b83d2a0dfa04` | Message provenance; no native span | Body hash above |

HTTP `conversation_list`, visible Chat and automatic
`agents/<agent-id>/transcripts/2026-10-09.jsonl` agree on these three entries'
content, identity, timestamps and provenance. The dated file is 2,359 bytes,
SHA-256 `1c9ac42b4a05aee2483c0b8d221ed7153351dea4fadc642d46f708ad9e86a3f4`,
and remained unchanged through normal native exit. Publication reports current,
zero pending entries. No notification/message association or echo deduplication
was exercised. This is a partial Chat projection, not a complete transcript.

Directly inspected screenshots include `13-blocked-chat-desktop.png` (1440×960),
`14-blocked-chat-390.png` (390×844), `15-blocked-terminal-desktop.png`, initial
Bash/consent, and final persistent Bash. The three-entry conversation fits both
Chat views; the marker wraps within the narrow viewport and controls remain
visible, with no horizontal page overflow or CSP violations. Long-history scroll
behavior was not exercised. Terminal keyboard focus and Chat composition worked;
the test-tube emoji rendered as a missing glyph. The UI retained “Activity unknown”
despite recorded idle hooks, and Terminal's footer still said Chat delivery was
not connected. These literal presentation observations were not corrected here.

All 19 manual input frames and two resize frames have submitted receipts. Those
frames comprise launch, consent, seed and exit; neither the Chat body nor its UUID
appears in manual input or the native source. There were no WebSocket error
frames, page exceptions or failed requests. Page requests used only the fixture
origin. Expected initial 401, favicon 404 and the second page's 401 after explicit
sign-out are retained. Screenshot hashes/dimensions are in `evidence/final-audit.json`.

## Retained harness failures, cleanup and checks

Original failures remain distinct from the product blocker:

- The first provider adaptation asserted on an import-order assumption before
  writing its file. The unchanged canned provider was stopped and replaced
  before any native request; its original process/log evidence was retained.
- The copied old snapshot helper encountered reclaimed terminal `payload=None`.
  Response `003.json` preserves that failure. The private read-only helper was
  corrected to distinguish remaining local payloads from verified host chunks.
- A separate `nsenter` inspector lost capabilities at exec and could not verify
  the recorded process. The private inspector subsequently entered only the
  fingerprint-checked owned namespaces in-process. No product state changed.
- A private evidence assertion initially expected a nested receipt field; the
  actual WebSocket frame carries `outcome` directly. Only that assertion changed.
- The bounded first-notification wait failed as recorded in `responses/012.json`
  and its failure screenshot. That is the observed product refusal, not a pass.

Under Manager direction 310, explicit human takeover preceded one normal `/exit`.
The early exit snapshot still saw the draining native process; it was preserved.
At `2026-10-09T03:07:13.869045Z` its owner became `closed/exited` with
`close_reason=process_missing`. The later snapshot proves no native process and
the same live Bash, recorder and tmux. Control was released, UI signed out and
owned Chrome closed. Core completed normal lifespan shutdown. The copied cleanup
stopped the owned shell/provider and fingerprint-checked SIGKILL of the remaining
recorder, after the native CLI had exited. Before core shutdown, Terminal reported
20,443 local and host-archived bytes; no complete shutdown-drain claim follows.

Cleanup retains two qualifications. A redundant stop of the already-closed browser
helper refused its no-longer-live fingerprint and sent no unverified signal.
The outer supervisor tool returned exit **143**, and its expected final record
was absent; successful supervisor exit is **not** claimed. Independent host audit
at `2026-10-09T03:08:41Z` found the original launcher absent and no process in either
owned network/PID namespace. All seven tracked cleanup identities were absent,
both fixture ports returned connection-refused (111), and only the private
leftover tmux socket was removed. See `evidence/cleanup.json` and
`namespace-cleanup-audit.json`.

Executed checks: private `verify-evidence.py`, `final-audit.py`,
`verify-imports.py evidence` and `provenance.py` passed. They check unchanged
pins/helpers, body bytes, no-attempt/handling evidence, source chunks/spans,
HTTP/daily equality, frame receipts, native exit and cleanup. No broad component
suite or earlier resume matrix was rerun. `build.log`, `verification.log`,
`final-audit.log`, `harness-errors.txt`, private helper sources, actual browser
frames/API observations and native copies retain the commands and results.
The 184-file `evidence-manifest.json` has SHA-256
`9de8378d7f7cb439c037db01ea26763b7cafae828c6f44effb8fd20e91285b0f`.

The immutable report commit/content hashes and final clean/pushed checks are
recorded after committing in `$RUN/report-result.json` and the completion report.
No product/dependency changes, delegation, merge or deployment occurred. This
Codex-only fixture made no Claude onboarding change. Canned protocol scaffolding
does not establish real login/inference or native MCP tool use. Connected delivery,
restart composition, complete child/attachment/shutdown history, VM execution,
browser takeover, backup/restore and deployment remain unaccepted by this run.
