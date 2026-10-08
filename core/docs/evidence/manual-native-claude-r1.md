# Manual native Claude workflow r1

2026-10-09 UTC (October 8 in the owner's timezone). **The qualified three-turn
workflow passed in attempt-3**, for Manager review under
[D40's assignment](../NATIVE-WORKFLOW-ACCEPTANCE.md). This is verification evidence,
not production acceptance, a product change or deployment. This document is the
only tracked output on `codex/verify-manual-native-claude-r1`.

The installed core is exactly `a2a162ce67f0eccafee537bd75a97069c1280788`;
the compiled UI is exactly `5a9090c7566edc7df1b3afa8facb7134dd420eca` from
`/home/alp/qorqutbot`. Both came from Git archives and unchanged dependency locks.
All 76 packaged files matched source, wheel and private installation. Each of the
two normal console processes imported 72 core modules exclusively from that
installation, with completed startup/shutdown. Both browser sessions fetched
matching built distribution bytes. The wheel SHA-256 is
`34617a2335d366756848922bef9a881fd0c47e1ac5533d244bf092e785d8ddb3`.

Private artifacts are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manual-native-claude-r1`
(`$RUN` below). The completed flow is `$RUN/attempt-3`; earlier attempts remain
intact. Accepted Terminal helpers and the unchanged canned Anthropic provider
were copied into this private root. No previous worker fixture was modified.

The real executable `/home/alp/.nvm/versions/node/v22.22.0/bin/claude` resolved to
the configured `@anthropic-ai/claude-code/bin/claude.exe`, reported
`2.1.287 (Claude Code)`, and matched native SHA-256
`3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0`.
Python 3.12.3, tmux 3.4, Bash, Node 22.22.0, locked Playwright 1.64.0 and installed
Chrome 153.0.8010.36 were used. No provider package was upgraded.

Core, Chrome, Bash, Claude and the canned provider shared a disposable
user/network/PID/mount namespace with only loopback up and an empty IPv4 routing
table. Ports were 18241/18242; `/tmp`, HOME, native stores, workspace and Chrome
profiles were private. The native environment contained only explicit fixture
inputs and a new synthetic provider key. No paid inference, personal CLI home,
owner session, browser profile, production data or suppressed material was used.

## Connected observations

The normal installed `qorqut-core serve --installation … --transport-config …
--ui-root …` entrypoint ran real provisioning, observation and publication
workers. Initial observations showed recorded Bash, `native_setup=ready`,
`configuration_installed`, `native_capture=waiting_session`, no native owner or
Bash child, and `input_ready=false`. Schema 7, sixteen operations and five job
handlers were unchanged. No manual job pumping, fabricated session owner,
test hook sink, MCP probe or intercepted HTTP/WebSocket result was used.

Actual UI login established HttpOnly, SameSite Strict cookie sessions. Explicit
Terminal takeover preceded input. Per Manager decisions 259/262, long text used
`keyboard.insertText` on the real focused textarea: exact bytes and submitted
receipts were checked before a separate, single Enter. The completed flow had
22 submitted input/resize receipts across three viewers, without WebSocket domain
errors, page errors or failed requests. Initial unauthenticated session 401s and
favicon 404s were retained separately.

**Fixture-only post-onboarding qualification:** product setup did not create the
onboarding state. The harness then wrote only
`{"hasCompletedOnboarding": true}` to managed HOME `.claude/.claude.json`, as
explicitly authorized. Native workspace, MCP and synthetic-key dialogs were
answered through Terminal. In attempt-3 the displayed MCP choices were inspected,
then the selected **Use this MCP server** option was visually confirmed before
Enter. Native consent enabled only `qorqut-core`; no future-server trust flag was
written. No trust hashes, authentication or permission preferences were seeded.
The bare first launch supplied no native UUID or extra configuration flags.

Production SessionStart connected before the first prompt. Stable fixture IDs:

| Binding | Value |
| --- | --- |
| Agent | `1644b083-85f0-4382-9c35-83c187830768` |
| Conversation | `52ee4b6e-3c2a-42ff-8225-9ad60c836c0c` |
| Recorded Terminal | `c1dc1cda-181d-4e63-9cfe-da2572167e19` |
| Native-selected Claude session | `f9fc4d0e-9b32-4d98-960e-8c313825ff11` |
| First owner/run, generation 1 | `e83e0b3d-4ba9-4149-ae81-aeb29696f618` |
| Resumed owner/run, generation 2 | `d4bb5766-17a7-4c16-bb2e-67bdf3a648d9` |

The first native process was namespace PID 385/start ticks 208236483; resumed
Claude was PID 2101/start ticks 208242517. The production hook records, exact
executable fingerprints, runtime owner and terminal agreed. The source's saved
native-session ownership span was independently hash-checked against its bytes.
The installed `python -I -m qorqut_core.runtime.hook_client` supplied the hooks.

The native provider request advertised all sixteen real core MCP tools. Separately,
the official MCP SDK performed one status mutation, attributed in both its result
and persisted operation `2874b41d-a533-4fd7-9000-c5355c394c61` to the real agent
actor `39e39efa-9840-4143-adb7-2ceb6835aa2e`. This is **SDK attribution**, not a
semantic tool call by the native model. The provider returned canned text only.

The three exact Terminal user/assistant pairs were:

| Turn | Native user text | Canned assistant text |
| --- | --- | --- |
| Initial | Claude R1 initial: reply with the canned sentence. Do not use tools. | claude-r1-initial-reply |
| Resumed | Claude R1 resumed: reply with the second canned sentence. Do not use tools. | claude-r1-resumed-reply |
| Core restarted | Claude R1 server restarted: reply with the third canned sentence. Do not use tools. | claude-r1-restarted-reply |

Normal `/exit` returned to the same Bash and ownership became `closed/exited`.
Manual `--resume` of the saved UUID established the new owner above, retaining
the same conversation, Terminal and native source. Initial entries stayed exact;
new native IDs were unique. With resumed Claude idle/alive, normal core shutdown
and restart preserved Bash, recorder, tmux, native fingerprint, owner and human
control. The third pair was then collected and published by normal workers.

Credentials, private binding, instructions and other saved configuration bytes
were identical across restart. Per Manager decision 265, the full parsed
`.claude/settings.local.json` value was compared canonically, preserving types,
arrays and every setting: only object-key ordering changed. Before/after hashes
and the complete formatting diff remain in `restart-config-comparison.json` and
`restart-config.diff`; no product configuration was edited to satisfy the check.

## Archive, Chat and dated files

All six native entries appeared in the compiled Chat DOM and `conversation_list`
with exact text and native provenance, and automatically in
`agents/<id>/transcripts/2026-10-09.jsonl` using durable UTC recording dates.
The earlier dated bytes remained an exact prefix after resume and restart.
Publication finished `current`, zero pending. One separate owner Chat note remained
stored/pending and absent from provider requests; `input_ready` stayed false.
Terminal keyboard input is not Chat dispatch.

One native source `257f58b2-9caa-4f8c-9fd7-360e2659114e`, epoch
`437cf8ef-c8f3-47a8-9c24-0ce33cbb7350`, linked both admitted runs. Projection
retained the earliest admitted run as its stable parsing provenance, including
later turns, as specified by D40.

| Entry | Entry ID | Raw offset + length |
| --- | --- | ---: |
| Initial user | `23d9fb13-5c00-4156-9f9e-f6af282b003d` | 504 + 747 |
| Initial assistant | `889d2264-d37e-4370-b0e0-ab8daf790718` | 40362 + 1304 |
| Resumed user | `a2ddf8cf-2cc6-499d-99dc-e87540572ba7` | 159780 + 788 |
| Resumed assistant | `53d696de-798b-4ea5-861e-dc17f4867366` | 161302 + 1304 |
| Restarted user | `8aff3651-467c-4f7d-84c1-8085a1bf7911` | 164593 + 796 |
| Restarted assistant | `5c0d539d-f914-4b7c-bb82-faed5a6ce770` | 166123 + 1241 |

Six immutable host chunk receipts reconstructed 169,115 bytes, SHA-256
`ae381ef4384b51cff92c0634e49d3863b26bc4e2a6059385520a6600559cb193`.
Every receipt and entry span hash matched its actual bytes. Local captured,
host-acknowledged and parser checkpoint offsets all reached 169,115.

| After turn | Archived bytes | Daily bytes | Daily SHA-256 |
| --- | ---: | ---: | --- |
| 1 | 158084 | 1727 | `c79aaa955bd36a391818440c2f8f8c4adb7d94ff1fc9d1a5aaa0c7a71b6d4584` |
| 2 | 164357 | 4089 | `14381aee2454dfab477220c04203ef1a1c1d686b1f033c895046881e6bbf3ab1` |
| 3 | 169115 | 5837 | `1bc0cb0b611ad630b90eb6e08444046f2eda0cac6864d203e66816749e8e27dd` |

Zero lag is not complete coverage: fourteen `unsupported_record` parser gaps
remained, and child/attachment coverage was explicitly unverified. Runtime
reported `gap/parser_gap`. After the collector stopped for teardown, native
metadata appended another 1,235 bytes (`cost-state`, `last-prompt`, `cost-state`),
retained privately but not included in the 169,115-byte archive receipt. No
final-drain or complete transcript archive claim is made. Terminal spool capture
remained separate, with zero terminal bytes archived by the host.

All thirteen saved screenshots were directly inspected. Initial native Chat text
is readable at 1440×960 and 390×844 in `turn1-chat-desktop.png` and
`turn1-chat-narrow.png`; actual dialogs, exit and all three Terminal replies are
also saved. Later Chat screenshots retain the top scroll position, so their newest
reply is below the saved viewport; DOM/API checks establish that entry's presence.
`visual-inspection.json` records every image hash and this visibility limit.

## Retained attempts, checks and cleanup

The first run stopped before Enter: 139 command bytes sent, 138 submitted input
receipts and one in-flight byte when the copied 45-second drain check expired.
Manager decisions 259/262 authorized fresh text-insertion testing. That run and
its timing evidence remain intact; per-character responsiveness is **not accepted**.
A preceding namespace-launch-only mount-capability error is separately retained.

Attempt-2 reached two native turns and restart, then stopped at the overly strict
settings byte comparison. Manager decision 265 confirmed an object-order-only
change and authorized the scoped comparison and fresh attempt-3. Attempt-2's
broader native MCP consent is recorded accurately; attempt-3 explicitly selected
only core. Neither earlier attempt is counted as a completed three-turn pass.

Executed checks are the private locked build/provenance helpers, the connected
`attempt-3/launch.py` flow, accepted `verify-imports.py attempt-3/evidence`, and
read-only receipt/ownership/daily-prefix checks recorded in `checked-facts.json`.
No existing setup/monitor/collector suite or compatibility matrix was repeated.
Principal evidence also includes `result.json`, `browser-result.json`,
`*-snapshot.json`, `turn*-publication.json`, `sdk-attribution.json`,
`native-tool-discovery.json`, request/frame logs and native/raw/daily files.

Logout closed the viewer. Normal console shutdown, exact-terminal shutdown and
fingerprint-checked pidfd cleanup left all nine recorded process identities
absent. Cleanup included SIGKILL of the still-owned recorder/native process after
shell shutdown; this is not a graceful final capture-drain result. Both ports
refused connections, the namespace contained no remaining process, and its one
leftover private socket was removed. `cleanup.json` and
`namespace-cleanup.json` record the checks. Private evidence is retained.

This does not accept fresh Claude onboarding, real login/inference, semantic
model-driven MCP calls, Chat-to-CLI dispatch, terminal raw host transfer,
browser/VM integration, backup/restore, deployment or complete G0–G6. No product,
test, dependency or plan was changed; no subagent, PR, merge or deployment was used.
