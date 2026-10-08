# Native input observations r1

2026-10-09 UTC. All six assigned submissions completed using the fixed native
executables. **Five preserved exact UTF-8 bytes; Claude's 2 KiB Unicode/LF/TAB
case failed exact preservation.** Its 38 TABs became four spaces each. Both
providers collapsed the longer pastes before Enter. Neither provider's observed
idle or busy screen received an `empty` verdict from the unchanged classifier.
These are original observations for [the fixed assignment](../CHAT-DELIVERY-EVIDENCE.md),
not Chat-delivery acceptance or a design proposal.

Private evidence root (`$OBS`):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/explore-native-input-r1`.
The only tracked output is this document, on `codex/explore-native-input-r1`,
from repository base `e8b71ea39fc58f58f52e296603d992600ac34faf`.
Executed core: `a2a162ce67f0eccafee537bd75a97069c1280788`. Each reused installation's
76 packaged files matched that Git revision, its wheel and exported source;
all 233 exported core files remained unchanged. No product, test, dependency or
plan changes were made.

| Native executable | Observed version | Native SHA-256 |
| --- | --- | --- |
| Accepted Claude launcher, resolving to `@anthropic-ai/claude-code/bin/claude.exe` | `2.1.287 (Claude Code)` | `3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0` |
| Accepted Codex shim, executing `@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex` | `codex-cli 0.160.0` | `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |

Absolute executable paths and versions are retained in each provider's
`evidence/versions.json`. Both used Python 3.12.3 and tmux 3.4. Completed
[Claude attempt-3](manual-native-claude-r1.md) and
[Codex](manual-native-codex-r1.md) helpers were copied privately; original helpers
were hash-checked unchanged. Their installed core was reused read-only.

Claude ran first; its cleanup completed before Codex started. Each had fresh
user/network/PID/mount namespaces, private `/tmp`, data, HOME and workspace,
only loopback up and no IPv4 route. Core/provider ports were 18431/18432 and
18441/18442 respectively. The normal installed console performed setup,
ownership, hooks and collection. No owner/session rows or native trust hashes
were fabricated. The actual CLIs were started manually through the recorded
shell's explicitly acquired human fixture control, using normal write receipts.
No UI or production automated-delivery driver participated.

Claude retained the authorized **fixture-only** `hasCompletedOnboarding: true`
seed. Actual workspace, single-core-MCP and synthetic-key consent dialogs were
answered with native keys; only “Use this MCP server” was selected. Codex used
the accepted manual `--no-daemon` and fake-provider arguments. Its actual eight-hook
trust dialog was answered; its bundled-bubblewrap warning was viewed and dismissed.
Neither first launch supplied a native UUID. Model labels on screen describe the
CLI configuration; responses came only from the copied canned loopback servers.

Each observation contains original `tmux capture-pane -p -e` bytes (`screen.sgr`),
a separate plain capture (`screen.txt`), `flags.raw`, exact native process
fingerprints, saved owners, production hook events and runtime state in
`observation.json`. Cursor coordinates below are zero-based `(x,y,visible)`.
Reads have start/end timestamps and are not an atomic combined snapshot.
The installed `adapters.composer.classify` was called read-only on the unmodified
SGR capture and saved cursor. No escape filtering or layout substitution was used.

| Provider / actual state | Geometry | Cursor | Literal classifier state / reason | Last hook activity |
| --- | --- | --- | --- | --- |
| Claude / Initial consent | 100×30 | `(1,19,false)` | `unknown` / `unsupported_escape_sequence` | None |
| Claude / Idle | 100×30 | `(2,27,false)` | `unknown` / `unsupported_escape_sequence` | `SessionStart` / `idle` |
| Claude / Idle | 80×24 | `(2,21,false)` | `unknown` / `unsupported_escape_sequence` | `SessionStart` / `idle` |
| Claude / Human draft | 100×30 | `(21,27,false)` | `unknown` / `unsupported_escape_sequence` | `SessionStart` / `idle` |
| Claude / Pending response | 100×30 | `(2,27,false)` | `unknown` / `unsupported_escape_sequence` | `UserPromptSubmit` / `working` |
| Codex / Initial consent | 100×30 | `(1,26,false)` | `blocked` / `selection_dialog` | None |
| Codex / Idle | 100×30 | `(2,26,true)` | `unknown` / `unknown_footer_or_multiline_input` | None |
| Codex / Idle | 80×24 | `(2,20,true)` | `unknown` / `unknown_footer_or_multiline_input` | None |
| Codex / Human draft | 100×30 | `(21,26,true)` | `blocked` / `composer_contains_text` | None |
| Codex / Pending response | 100×30 | `(2,26,true)` | `unknown` / `unknown_footer_or_multiline_input` | `UserPromptSubmit` / `working` |

Claude's consent capture used the normal screen; its idle/draft/busy captures
used the alternate screen with mouse-any and mouse-SGR flags set. Codex used
the alternate screen with those mouse flags unset. All these captures had wrap
on, insert/keypad flags off and `pane_in_mode=0`; complete flags remain private.
Claude's initial text captures include real OSC-8 hyperlinks, which cause
`unsupported_escape_sequence`. After that content scrolled away, its post-turn
empty composer returned `unknown/unknown_footer_or_multiline_input`; its large
paste placeholder returned `blocked/composer_contains_text`. Codex returned
`blocked/selection_dialog` for hook trust, `unknown/no_known_composer` for the
warning overlay and `blocked/composer_contains_text` for all pasted states.
All literal verdicts have `evidence=synthetic`, `native_verified=false` and
`permits_paste=false`. The evidence label is the existing classifier's output;
the saved screens themselves are actual native captures.

Claude showed `❯` between horizontal rules with the auto-mode footer; Codex
showed the dim `Ask Codex to do anything` placeholder plus model/path and shortcut
footer rows. The deliberately entered 19-byte human draft moved the cursor from
x=2 to x=21. Nineteen explicit Backspace bytes removed only that draft, with an
empty-screen observation before any payload paste. Claude's last hook still said
`SessionStart/idle` while the draft was present. Codex had no owner or hook until
its first submitted prompt, including during its idle/draft observations.

The short-turn busy captures show Claude's “Tinkering…” and `esc to interrupt`,
and Codex's “Working (1s • esc to interrupt)”. Both also show an empty-looking
composer and a recorded `UserPromptSubmit/working` hook. Each capture completed
within the copied provider's five-second pending response delay (Claude about
24–85 ms after its first pending marker; Codex about 12–110 ms). Request bodies
and pending markers are retained. Both short turns generated two local requests;
the delay was disabled before the remaining submissions. No paste was attempted
in a deliberately busy, modal or draft state.

Every payload begins with its own fresh visible fixture UUID. Payload bytes,
framed streams and full native decoded text remain private. The short payload
has 77 bytes; the medium has exactly 2,048 bytes, Unicode, 39 LF and 38 TAB; the
large has exactly 65,536 UTF-8 bytes, 1,170 LF, no TAB, and an ordinary-text ending.
The driver sent `ESC [ 200 ~` + payload + `ESC [ 201 ~`, then a separate CR key
as Enter. It never used tmux's implicit bracketed-paste option. The 65,548-byte
framed stream was split into legal contiguous frames of 65,536 and 12 bytes,
with the same viewer/control epoch and no interposed input. Exact concatenation,
frame hashes and submitted receipts were checked, without changing the frame limit.

| Provider / payload | Paste frame bytes | Paste sequence(s) → separate Enter | Exact pre-Enter screen visibility | Native exact equality |
| --- | ---: | --- | --- | --- |
| Claude / short | 89 | 14 → 15 | Full short payload | Pass |
| Claude / medium | 2060 | 16 → 17 | `[Pasted text #1 +39 lines]` | **FAIL: 2,048 → 2,162 bytes** |
| Claude / large | 65536 + 12 | 18, 19 → 20 | `[Pasted text #2 +1170 lines]` | Pass |
| Codex / short | 89 | 12 → 13 | Full short payload | Pass |
| Codex / medium | 2060 | 14 → 15 | `[Pasted Content 1706 chars]` | Pass |
| Codex / large | 65536 + 12 | 16, 17 → 18 | `[Pasted Content 65536 chars]` | Pass |

The longer pre-Enter views expose only placeholders, not the UUID or complete
pending text. The earlier conversation still appears above them. No matching
native user record existed in the saved pre-Enter native files. Full short text
was visible, but screen rendering alone is not exact byte proof. Complete
post-Enter native fields were decoded and compared as UTF-8 bytes; no substring
equality was accepted. Codex's screen renders TAB as spacing while its native
records retain literal TABs.

Claude medium's first difference is byte offset 75: actual text equals the
original only after replacing every TAB with four ASCII spaces. Its 39 LF and
other bytes were retained. Expected SHA-256 is
`b20612f3fb6ae0de49873d94981eed7ad0d3c421764080d414b88a09e7ced07f`;
actual is `f02c516981d42599d7d12b39c660239a6475fea2022b88abab91304334a93945`.
Question 274 and Manager decision 277 retain this as **FAILED exact-byte
preservation / observed native transformation**. Shell frame submission was
exact; the pinned native CLI's recorded prompt was transformed. No retry,
normalization, configuration change or workaround occurred. This does not prove
byte-preserving Chat delivery for literal TABs or generalize to other versions,
providers or control characters.

The following SHA-256 values identify complete decoded native user text. For all
passing cases they are also the exact original payload hashes.

| Provider | Short (77 bytes) | Medium (native bytes above) | Large (65,536 bytes) |
| --- | --- | --- | --- |
| Claude | `407dcdfdbdc8c6e7b77b35e909bac88a8f847c30f167287e91ad325ce42bad40` | `f02c516981d42599d7d12b39c660239a6475fea2022b88abab91304334a93945` | `c79bfef2bae4711e978cad7534adb69c1e36dd5aa5c9c8c9f4ea94c35d7e4f92` |
| Codex | `69208ecd762c0c50ae58328e7b0c0a9d73dd6edb8592e3bcf66aad6a1ce7a71c` | `7345c20a7eff77daf33cc2ef7df86c3e1517a62dd5933ddc9926bdb32dd0d26a` | `a4bde7ad02a333b6582bfa51e07087df4b634cf739f86c7e968e9dbb1df68914` |

Claude session `e661a50e-fbcb-4a6f-85f2-e768c1d0078e` belonged to normal owner
`a62f424b-35dc-46ab-ae51-acac0900ca84`; native PID 445/start ticks 208414057 in
PID namespace 4026532978 matched every live snapshot. Its original `type=user`
records hold the complete string at `message.content`. Each has distinct `uuid`
and `promptId`, `origin.kind=human`, `promptSource=typed`, `turnOrigin=human`,
and prompt/turn indices 1, 2 and 3 respectively.

| Claude payload | Native `uuid` | Native `promptId` | Original raw offset + length | Original raw SHA-256 |
| --- | --- | --- | ---: | --- |
| short | `0ba9b6b4-65e5-45a3-b112-b999ceb05609` | `f9bc147f-59bd-46b4-84fd-c5cb69a0dd69` | 504 + 753 | `68d44b65f408e7b24b7ebe3ceea4079f626b15517474a35595b895124fa48d81` |
| medium | `ef79f440-73de-4709-82c7-1cc706136f8e` | `83ed1ee2-9ce4-4195-9fd6-f4946cd628e5` | 158758 + 2911 | `2a8384adb4f6fc5ef6178a96ea202cf0bcd580eeaee6c54296415ba37bd570cf` |
| large | `779e3cca-fcb6-409a-b9d2-b2562950aa30` | `3a84d957-0c85-41b1-88b9-a3ee42b46237` | 165625 + 67416 | `8983564db6672616d93191dfa69aea4c63a4850994804d2f13f10c9275a3fb04` |

Claude also emits `last-prompt` metadata. The saved large-turn example contains
only 203 bytes with an ellipsis; it is not the full user record or an equality
proof. The complete 65,536-byte `type=user` record above passed independently.

Codex session/thread `01a11e23-6f50-7833-b085-0e5a7970686f` belonged to normal owner
`9cd26cf6-a298-42e3-a27e-f54c8aaddebb`; native PID 524/start ticks 208441249 in
PID namespace 4026532977 matched every live snapshot. Each submitted turn emitted
both a `response_item` user message and an `event_msg/item_completed/UserMessage`.
Each has one text content entry and both independently preserve the exact payload.
The native item also has a distinct `client_id`, retained in `checked-facts.json`.

| Codex payload | Native turn ID | `response_item.payload.id` | `UserMessage.id` |
| --- | --- | --- | --- |
| short | `01a11e24-ced3-7690-86d1-515cfa910237` | `msg_01a11e24-d475-7451-bcb7-254c6759ec9e` | `01a11e24-d475-7451-bcb7-2559c1d65007` |
| medium | `01a11e25-7063-72f1-8f77-67ef51c13570` | `msg_01a11e25-72f1-7b70-84ab-f436c010e05f` | `01a11e25-72f1-7b70-84ab-f4418a6b42b3` |
| large | `01a11e25-fbf3-79c0-8efa-557964db7644` | `msg_01a11e25-ff40-7de2-b18c-22ed4b5a69be` | `01a11e25-ff40-7de2-b18c-22f2d6a54a56` |

| Codex payload / record | Original raw offset + length | Original raw SHA-256 |
| --- | ---: | --- |
| short / `response_item` | 46123 + 767 | `935d175f7d87bf1ebf317acb9d58f5b3f02e42d1e47312409e3584b489d07a74` |
| short / `event_msg` | 46890 + 529 | `13f98fec1f7b281c54b233954145522000765d065eaa1b2a9ca15b7ae8c94b9f` |
| medium / `response_item` | 60048 + 2816 | `4a2ee2c796d99ff708bc1263267e532a861f8d42c9426bf69136497ce8927888` |
| medium / `event_msg` | 62864 + 2578 | `cb0c31aec6a75a9a96da09c30e8c8111295f16885768002f20c34f666e2a7d32` |
| large / `response_item` | 78072 + 67398 | `02fb5b54807c29bca6314ad1635e8af71056d154e1e31b16332fc1333ebba474` |
| large / `event_msg` | 145470 + 67159 | `a9ab86e5545bce0f05e72e375365fe99eec30ec9dc411f5b2657f1ba4f5fb781` |

Codex's `metadata.retained_source.complete` is true for short/medium but **false**
for the large `response_item`, despite the complete preserved text in that raw
record and its separate UserMessage. The literal flag, retained revision IDs,
ordinals, native IDs and full records are preserved; its meaning was not inferred.
No platform message was sent or associated: conversation entries have null
`message_id`. Fixture UUID lookup only located observation records; it does not
establish a production correlation or deduplication method.

Normal collection archived every compared user span. The final saved primary
files exactly match reconstructed immutable host chunks: Claude 238,975 bytes
in eight chunks; Codex 215,459 bytes in six. Both have six native conversation
entries. Claude retains fourteen parser gaps; Codex has none for this primary
file. Child/attachment coverage remains unverified. Derived excerpts,
`checked-facts.json`, native text decoding and classifier outputs are labeled
analysis of original data. Canned replies and test payloads are fixture data;
the native files, hook events, screens and receipts were emitted by the real
pinned programs and normal core workers.

Both `/exit` commands completed normally, returned to the persistent Bash shell,
and eventually produced no native process and `closed/exited` ownership before
teardown. Human control was released and the normal console completed lifespan
shutdown. Accepted owned cleanup then stopped each recorded shell and provider;
it used fingerprint-checked SIGKILL for the remaining recorder in each fixture,
after the native CLI had already exited. All six tracked process identities per
fixture were absent, all four fixture ports refused connections, both namespace
launchers exited zero, no namespace process remained, and only private leftover
tmux sockets were removed. Raw evidence and failed byte comparison are retained.

Executed verification: private `verify-observations.py` passed against the saved
originals, receipts, installed pins, raw spans, host chunks, pending timing and
cleanup. There were 19 Claude and 17 Codex submitted input frames, plus three
submitted resize receipts per provider. The helper preserves each input frame
and receipt; no accepted workflow suite was rerun. Principal evidence is
`checked-facts.json`, each provider's `evidence/{initial-consent,idle-*,human-draft,
short-busy,*-before,*-pasted,*-post}/`, `payloads/`, `frames/`, native comparisons,
`final-original-native.jsonl`, `final-host-native.raw`, `final-host-rows.json`,
`cleanup.json` and `namespace-cleanup.json`.

These observations leave long-paste content unavailable from the collapsed
pre-Enter screen alone. No live automated readiness, permission to paste,
platform-message association, real inference, fresh Claude onboarding, other
version/layout compatibility, terminal host-archive acceptance or complete
G0–G6 acceptance is established. `input_ready` remained false throughout.
No delegation, browser QA, PR, main merge or deployment occurred.
