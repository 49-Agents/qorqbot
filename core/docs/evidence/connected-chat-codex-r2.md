# Connected Codex Chat r2 — observed delivery, MCP handling and restart

2026-10-09 UTC. At the fixed core/UI pins, three Chat messages were each submitted
once as a 51-byte notification, read by the actual native Codex MCP client,
explicitly acknowledged by that agent and followed by a recorded native assistant
reply. The third message completed after a normal core restart with the same live
native session. HTTP, visible Chat and automatic dated JSONL agree on eleven
entries. This is a qualified connected-flow observation, not Manager acceptance
or an error-free UI result: intermittent capacity errors disconnected the Terminal
viewer, and stale presentation remains. No product fix was made.

This follows [the fixed acceptance flow](../CONNECTED-CHAT-ACCEPTANCE.md), with
r1 accepted only as blocked evidence at `8ac24ea`. The reviewed empty-composer
fix `274b19a` is in this run's base. Its pure checks and original replay were not
rerun. Manager owns interpretation, review, merge and deployment.

Only this report is tracked on `codex/verify-connected-chat-codex-r2`. Private
original evidence root (`$RUN`):

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/connected-chat-codex-r2
```

## Pins, build and containment

| Input | Verified identity |
| --- | --- |
| Core source and installed package | `852301fd244bcc83bb86c167ab7d0d8fb6157045` |
| UI source in `/home/alp/qorqutbot` | `30bf7181127a5bac4ff7c5fa6297112b63a84ea1` |
| Fresh wheel SHA-256 | `928519ec87db78d26e6a9071678cb6e3950c671463960852251486d2194c1cd7` |
| Core lock SHA-256 | `082ab8e50044aaad21ca1da983eeae50e6240cfd817023cb751243825f4b708a` |
| UI lock SHA-256 | `1c07d1422b8271985fe16bf8761326b0078d18ddfbfa4f8ef682e00cd778e3de` |
| Native version | `codex-cli 0.160.0` |
| Native executable SHA-256 | `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |
| Supporting programs | Python 3.12.3, tmux 3.4, Chrome 153.0.8010.36, Node 22.22.0, locked Playwright 1.64.0 |

Both pins were exported with `git archive`. A new venv reused compatible dependency
files from completed r1, checked all 32 lock pins, built the current exported core
with `pip wheel --no-deps --no-build-isolation` and installed that new wheel with
`pip install --no-deps`. All 85 packaged files matched source/wheel/installation;
283 exported core and 71 exported UI files stayed unchanged. Both normal server
lifecycles loaded 81 core modules exclusively from the new installed package,
without `PYTHONPATH`. See `build.log`, `exports.json`, `provenance.json` and
`evidence/installed-import-provenance.json`.

The reused UI distribution is byte-identical to completed r1's artifact. Its
original build was at `3d5e5e7483d1e5a1eb9ca1814b2fe1faf85ee26e`; differences to
the assigned pin are documentation only. All 25 non-documentation tracked files,
including the lockfile, matched recorded provenance. Four copied assets matched
bytes served by this real core. `ui-reuse.json` retains the original provenance
and build-log hashes; `evidence/01-login-served-assets.json` and subsequent login
asset observations retain the served comparisons. No broad rebuild was needed.

Copied finished helpers are fingerprinted in `copied-helpers.json`; original
bytes are retained in `original-helpers/` and original r1 helper hashes still
match. The native launcher remains the accepted
`native-rehearsal-r1-validation/codex-pkg/shim/codex`; its resolved binary identity
is in `evidence/versions.json`. No r1 data, credential, native session or browser
profile was reused, and no unfinished actor was inspected.

Fresh user/network/PID/mount namespaces, private HOME/tmp/data/native stores and
Chrome profile contained all fixture processes. Only loopback was up; core and
provider ports were 18641/18642. `evidence/containment.json` records addresses,
routes and mounts. Three newly generated secrets were absent from the 457 scanned
evidence/log/request/response files. No real credentials/inference, external
route, owner session, VM, browser takeover or global configuration change was used.

The ordinary installed `qorqut-core serve --installation … --transport-config …
--ui-root …` ran its real worker and monitor, official managed hooks and core MCP
at `http://127.0.0.1:18641/mcp`. Observed inventory remained schema 9, seventeen
operations and five handlers. There was no API/WS/MCP interception, direct job
pumping, RuntimeInput/TerminalRunner call, fabricated hook/owner, SQL mutation,
manual native collection/projection or synthetic receipt. Read-only diagnostics
and private fixtures remain outside Git.

## Native startup and connected messages

The real session form established the HttpOnly, SameSite Strict cookie.
`02-owner-free-shell.png` shows recorded Bash without an owner or native process;
creation did not start Codex. An acknowledged UI Terminal takeover preceded the
accepted manual `--no-daemon` launch and loopback `qfake` configuration. Eight
actual managed hooks were inspected and trusted through the native dialog. The
bundled-bubblewrap warning was viewed and dismissed; no permission mode changed.
Native model/effort remained its Terminal default (`gpt-6.1-sol`/default display),
with deterministic canned responses and no inference.

Before the seed there was no admitted owner, primary native file or Chat entry
(`06-pre-seed-snapshot.json`). The one authorized manual prerequisite was
`Connected Codex r2 seed.`, answered with `Connected Codex seed reply.`. It admitted
the owner/source and established the accepted idle hook. These two entries are
seed evidence, not replies to a stored Chat message. First-message Chat delivery
immediately after CLI startup remains unproved.

| Identity | Value |
| --- | --- |
| Installation | `a574a176-6448-44c6-bd25-dc076b8cc669` |
| Agent | `711c1c09-a7db-463d-a31d-1bdcffd28b0e` |
| Agent actor / actual handling actor | `0ec6f926-3aca-45d7-b45d-fe16d9ad159c` |
| Owner actor | `ebfc8cfb-c1de-4258-b996-41eee31207d7` |
| Conversation | `2de293c6-fd3b-45f4-bbeb-ab144e3cb9d0` |
| Recorded terminal | `619c1040-55b1-456a-bd33-5092d86cd7e6` |
| Owner/run, generation 1 | `d39cb5e3-d2d3-44e8-b2e3-50f6a6793b94` |
| Native session | `01a11eae-fba5-7da0-8e4e-dc5b66795c46` |
| Native process | PID 1023; start ticks 209357989; PID namespace 4026533050 |

Actual hook events, source/run linkage, native records and public observations
agree on this binding. Public `input_ready=false` remained conservative and was
not treated as failure. Primary collection and recording were active. Later
semantic gaps (`unsupported_record`: 12; `unsupported_item`: 6) remained visible;
they did not erase verified primary raw collection.

With human control held, the first exact Chat body was durable and dated, with
`deferred/human_control`, zero attempts/handling and no matching native user
record. Original observations are `10-human-held-*`, `11b-human-deferred-*` and
`responses/012.json`. UI Release control left the empty native composer intact.
The ordinary worker then delivered exactly the canonical notification.

The second UI send occurred at `03:24:44.051Z`, 0.091 seconds after the provider's
five-second delay began. `12-while-working-*` shows only the first attempt and no
second UUID in native input. The second message remained deferred while native
work/consent was pending. Only after the next real idle hook and UI release did
ordinary dispatch submit it. No extra idle event or notification was forced.

| Message | Message UUID | Attempt UUID | Accepted idle event / control epoch |
| --- | --- | --- | --- |
| 1 | `718d37cf-5fc4-474f-b5ac-62a15ce1b257` | `3b90dbde-f0ef-45f5-b187-29b97d388795` | 16 / 2 |
| 2 | `eaadbd1b-cba7-4926-b699-1a0bd03912e1` | `ce0e6fcc-6cc2-44de-8892-dc2a4990d884` | 140 / 4 |
| 3 | `8d168dab-dbad-486a-a731-7dce7a50f585` | `c82b54ab-e7c3-4828-afbf-d48af596c53d` | 176 / 6 |

Every row has exactly one submitted attempt and an identical original runner
receipt, including binding digest, payload hash/51-byte length, control epoch,
state, phase and timestamps. The persisted targets retain the same native
process/session/run/terminal. The first two bodies contain literal Unicode, LF
and TAB; JSON escapes below expose the exact control bytes:

```json
"Connected Codex r2 1 d333c3f9-f7db-4fe2-afbc-9fad8e9993b0: café λ 漢字 🧪\nLine two\tTAB retained."
"Connected Codex r2 2 ae43eca3-652a-4813-8394-1d3c998a18d3: café λ 漢字 🧪\nLine two\tTAB retained."
"Connected Codex r2 restart 92aeab61-3d4e-4729-8f9e-4d4ba2721595."
```

| Message | UTF-8 bytes | Body SHA-256 |
| --- | --- | --- |
| 1 | 102 | `71f1397bf88c0834ab7020194e150f87b07db4627682d7104e2c7d59357ff033` |
| 2 | 102 | `4a5175e50e112105415c75e4b2bccf772f5ae7ddd7128dfcd542beabde6612ce` |
| 3 | 64 | `b9271a7af39acc6ac9afd46e011ab03315018067535bc447b237ae71a93661cc` |

## Actual MCP protocol and handling

The seed's actual native request advertised `additional_tools → functions.exec`
as a custom Lark tool; deferred MCP methods were resolved from that native
executor's `ALL_TOOLS`. The private provider checked this actual advertisement
and validated arguments against the current served core schemas. The registered
names returned by native execution were `mcp__qorqut_core__messages_list` and
`mcp__qorqut_core__messages_acknowledge`. The core MCP adapter advertises those
same argument schemas. See `native-advertised-tools.json`,
`provider-schema-validation.json`, `fixtures/response_script.py` and original
`logs/openai-bodies/` / `logs/response-*.sse`.

For each UUID, the real native MCP read returned `ok=true`, `isError=false`, the
exact unchanged body above, sender/thread/timestamps and null prior handling.
The real native acknowledgment returned the handling row below. All six original
`item_completed/McpToolCall` records name server `qorqut-core`, exact arguments,
completed status and actual response envelopes. Their raw spans/hashes, full
synthetic responses and call IDs are saved in
`evidence/36-connected-complete-verified.json`. The saved agent-attributed
operation/event/handling rows match those responses. No out-of-band SDK call
was used to satisfy the gate.

Reads required no displayed consent. Each acknowledgment displayed the exact
server/tool/UUID and received only the native **Allow** choice through an explicit
UI Terminal takeover, followed by release. Screenshots `14-ack-consent.png`,
`18-second-native-tool.png` and `32-third-ack-prompt.png` preserve the dialogs.
No session-wide/future permission, classifier scripting, denial bypass or mode
switch was used.

| Message | Actual acknowledgment operation | Handled at UTC |
| --- | --- | --- |
| 1 | `c66a371e-9568-422b-8808-05ba0ca8bfde` | `2026-10-09T03:25:46.082719Z` |
| 2 | `a025c4a2-98e8-4059-8901-e4c1485f014a` | `2026-10-09T03:26:20.192325Z` |
| 3 | `f8d7efa7-2e85-4d45-b297-7746f035aab0` | `2026-10-09T03:31:56.278882Z` |

Actual native order is read → acknowledged result → assistant entry for each
message. Replies are exactly `Connected Codex r2 assistant N: acknowledged.`
for N=1,2,3. Submitted delivery still says `processing_not_confirmed`; explicit
handling is separate, and assistant prose is not treated as proof of handling.

One provider qualification is retained: the first acknowledgment's outer exec
returned `Script running with cell ID 2` after its 31-second yield while consent
was pending. Native MCP nevertheless completed at `03:25:46.096Z`; the next
provider request arrived at `03:25:46.106Z`, and the native assistant entry was
recorded at `03:25:46.113Z`. The completed acknowledgment response is in original
native ordinal 29, not in that provider request's yielded status. The script did
not exercise `functions.wait`; its `ack_result_then_final` phase label alone is
not completion evidence. The other two acknowledgment results were present in
provider input. These facts limit the canned harness protocol claim without
replacing or inferring any actual handling result.

## Projection, raw spans and restart

The three stable owner messages and native replies match HTTP `conversation_list`,
visible Chat and `agents/<agent-id>/transcripts/2026-10-09.jsonl`, including entry
identity, text, UTC timestamps and provenance. The dated file has 11 entries,
8,873 bytes, SHA-256 `22f730a6c064682befc82a4b8ff8a06afc807366e319f203d86154cd2f45531a`.
Publication is current with zero pending entries. Original native notification
echoes remain separate native entries, with no `message_id`, delivery or handling
association; equal text was not used to rewrite/deduplicate them.

| Message | Stable owner entry | Native notification entry | Native assistant entry |
| --- | --- | --- | --- |
| 1 | `b7c729fb-936d-46f3-8478-8bdbdd4c86e7` | `2680effe-0dd8-45a3-b576-f4cf084b1ff2` | `8f7ee22b-d2ce-48d8-91c0-bea5583ade33` |
| 2 | `e9c1d6be-e4d8-4d62-871f-770b5eddb341` | `43589b33-3273-4ab9-b7b7-affeb2713780` | `cb03f460-6cfb-45dd-a917-810ebadcb6ec` |
| 3 | `9ea8a0c4-e235-42b8-bc53-032ad855274c` | `b3b50be1-4fdd-48d6-9647-422845188fed` | `c8e070dd-3dfc-4c11-af8a-859f16882a04` |

Primary source `1dd35817-884d-4f47-b5a0-13232f7f6f55`, epoch
`d0ae4bf1-e9b2-4474-baad-8b8bde92afc7`, contains 130,849 bytes,
SHA-256 `fe00813d595cab558075476e908527154d46ca5c56a05641b3a7630d3d758be1`. The live file, final copied native file and
reconstructed immutable host chunks are identical. Every chunk and these
projection spans were checked:

| Entry | Raw offset + length | Raw SHA-256 |
| --- | --- | --- |
| Manual seed | 47671 + 476 | `934c80aca76d6f809789f7803532ccc2a1a18db07ca4b0ffe1bf53e40d245830` |
| Seed reply | 48147 + 403 | `c3993aade889fce4a471423943fe6a8984a9a202f81e046503cc19fb2eaf05aa` |
| Notification 1 | 61382 + 504 | `334573d0b69e7fa15e68f005ec5d2e23814867244aa61ef2f6e4a40ae7e8e728` |
| Assistant 1 | 73941 + 421 | `66d3b3edc3d750dc8e708e5c4a53c0d4db36d6a5024d98a292563dca2433a8dd` |
| Notification 2 | 87768 + 504 | `e54a53eebcde9f6345a476e383b07f42a1c8ee958162e2322b1dd690db9c88ca` |
| Assistant 2 | 101185 + 421 | `f05c50227f11e21f8d63aac9dfef87993939b4e44ea34ff659373fa5a349f816` |
| Notification 3 | 114730 + 504 | `92c036b02d8ac896dea6fb93dd4e5194f5379838430c686101962408b67f8707` |
| Assistant 3 | 127979 + 421 | `0e745e0b9df2706d394d4d5a69d50c6683f423d481c33770dd5bcff392c2ecdd` |

Original native file:

```text
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/connected-chat-codex-r2/run/data/agents/711c1c09-a7db-463d-a31d-1bdcffd28b0e/runtime/home/.codex/sessions/2026/10/08/rollout-2026-10-08T23-22-26-01a11eae-fba5-7da0-8e4e-dc5b66795c46.jsonl
```

After the first two messages, normal SIGTERM stopped core; native Codex, Bash,
recorder and tmux fingerprints remained live while the core port refused
connections (`restart-outage.json`). Normal restart and real-form reauthentication
restored the same owner without any new native startup consent, seed or hook.
`restart-comparison.json` proves unchanged messages, attempts, handling rows,
entries, dated bytes, source/run links, native file/process and managed config.
No submitted message was pasted again. The fresh third Chat message then used
that same session, saved pre-restart idle event 176 and reopened native capture,
with a new single attempt and actual native MCP read/ack/reply. This observes the
D48 reopen plus D46 input composition, not only persistence.

## UI observations, failures and cleanup

Inspected screenshots include desktop Chat `38-verified-chat-desktop.png`,
desktop Terminal `36-final-terminal-desktop.png`, and 390×844 Chat
`39-final-chat-390.png`, under `evidence/screenshots/`. Their hashes and dimensions
are in `final-audit.json`. Text and UUIDs wrap within the narrow viewport; composer
and navigation remain visible, with no horizontal page overflow or CSP violation.
The conversation scroll area shows part of the eleven-entry history; narrowing
retained a mid-history position with later entries below the viewport. This was
not an exhaustive long-history scroll test. Chat composition and acknowledged
Terminal keyboard input worked. The test-tube emoji rendered as a missing glyph.
The UI still displayed `Activity unknown`, provisioning status, and Terminal's
`Chat delivery is not connected yet` footer despite successful deliveries.

There were no page exceptions or non-fixture page origins. There **were** ten
observed operation HTTP 503s (six runtime_get, four terminal_get):
`unavailable / Operation capacity unavailable; call not admitted`, with
`retryable=false`. They occurred under ordinary multi-page observation, without
fault injection. Desktop Chat exposed `Session information unavailable`; the
Terminal viewer later disconnected with `Terminal availability or identity
changed. Earlier input may have arrived…`. No cause beyond these observations is
claimed. Six connection-refused event polls occurred around the intentional core
outage; three aborted operation reads and sign-out/initial-auth 401s are also
retained. The helper's generic `expectedClosure=false` flags were not rewritten.
This is not a clean network/UI pass. Original responses, frame closures, console
errors, page text and failure screenshots are preserved.

Private harness failures are also retained in `harness-errors.txt`:

- `responses/011.json` is a read-only terminal_get 503; later independent reads
  succeeded. The failing helper did not save that response body; actual UI 503
  bodies are retained separately.
- The evidence parser first assumed every exec result was a text-block array,
  then looked for the first ack response in provider input. The string-valued
  yield exposed both assumptions. Assertions now use original completed native
  MCP records, retain provider-visible results and verify chronological order.
  No notification/tool was repeated to obtain a missing result.
- `responses/046.json` records Take control timing out in the disconnected
  viewer. Copied action.py incorrectly returned shell success for `ok:false`, so
  its next `/exit` keyboard action ran in that disconnected view. It emitted
  **zero** control/input/resize frames; runner control stayed unclaimed at epoch
  8, sequence 0, and Codex stayed live with an empty composer. Exact proof is in
  `exit-no-input-proof.json`. Early `42-*`/`43-*` filenames are labels, not proof
  of exit. The private helper was corrected to fail its shell command on failed
  actions. Normal Chat/Terminal view reconnection and a separately acknowledged
  takeover at epoch 9 then sent the one actual `/exit` with submitted receipts.

At `03:35:35.376232Z` the native owner closed as `exited/process_missing`.
`48-native-exited-snapshot.json` and `48-verified-bash-after-exit.png` prove Codex
absent while the same recorded Bash/recorder/tmux persisted. All message,
attempt, handling, entry, native and dated content remained unchanged through
exit. Control was released; the UI signed out and owned Chrome closed normally.
All 23 manual input and five resize frames have matching submitted receipts;
none contains a stored body or notification UUID. Resize frames were normal
UI takeover geometry (104×50 after the initial takeover), never a classifier
workaround.

Both core processes completed normal lifespan shutdown. Fingerprinted cleanup
stopped the owned provider/shell and killed only the remaining owned recorder
after native exit. A redundant stop of the already-closed browser helper refused
its nonlive fingerprint without sending an unverified signal. Cleanup has zero
live tracked processes, both private ports refuse connections, and the outer
namespace supervisor exited **0**. Independent host namespace audit found no
remaining network/PID-namespace process. The one leftover private tmux socket
was removed. See `evidence/cleanup.json`, `namespace-cleanup.json` and
`evidence/final-audit.json`.

The final live Terminal observation was 188,483 local bytes versus 93,489 archived
bytes. Private spool/data remain preserved. No full Terminal shutdown-drain
or complete native child/attachment history claim follows from the primary-file
comparison.

## Executed checks and submission

| Command / retained inputs | Result |
| --- | --- |
| Exact `git archive`; private `pip wheel --no-deps --no-build-isolation`; `pip install --no-deps`; `provenance.py` | Current wheel installed, 85 package files and 32 pins matched |
| `launch.py`; `action.py` with `requests/001.json`–`057.json` | Actual isolated UI/native flow; original success/failure responses retained |
| `verify-flow.py 22-before-restart 2` | 2 messages, 8 entries, 4 actual native MCP results verified |
| `verify-flow.py 36-connected-complete 3` | 3 messages, 11 entries, 6 actual native MCP results verified |
| Private restart comparison and `outage.py` | Same live session; unchanged identities/bytes; no duplicate input |
| `verify-imports.py evidence` | Both servers: 81 installed modules, clean startup/shutdown |
| `venv/bin/python -I final-audit.py` | Exact runner receipts, native/dated equality, final exit, frames, secrets, unchanged sources/helpers and cleanup passed |

`verification-before-restart.log`, `verification-complete.log`, `final-audit.log`
and original helpers contain the checks. No broad component suite, pure layout
suite, earlier resume matrix, fault matrix, nested agent or reviewer was run.
The 503-file `evidence-manifest.json` has SHA-256
`f892f8e794e260a9ad2bac267dd00b27b9305e9aaf2171e67fd407c4fc021d35`. Later submission metadata is separate:
`report-result.json` records the exact report commit/content hashes, pushed branch
comparison and clean status; the completion report names that same immutable SHA.

No merge or deployment was performed. This Codex-only lane made no Claude
onboarding change; the separate Claude fixture's onboarding/classifier
qualifications do not become a Codex result. The manual-seed prerequisite,
ordinary native consent and canned provider remain qualifications. This does
not establish real provider login/inference, complete transcript/child/attachment
or shutdown coverage, VM execution, browser takeover, backup/restore or deployment.
