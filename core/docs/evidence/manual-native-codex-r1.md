# Manual native Codex workflow r1

2026-10-08–09 UTC. The assigned three-turn connected flow completed against the
exact installed pair below. Manager acceptance is pending; the observed Terminal
typing latency remains open. This is factual verification, not product changes
or complete system acceptance. Only this document is tracked on
`codex/verify-manual-native-codex-r1`.

`$RUN` denotes the private evidence root
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/manual-native-codex-r1`.
The workflow follows [D40 acceptance](../NATIVE-WORKFLOW-ACCEPTANCE.md), with
Manager decision 258 answering question 257 about the copied browser helper's
timeout. Accepted [autoload](manual-native-autoload-r1.md) and
[Terminal](terminal-integration-r1.md) helpers were copied into this root;
their originals were not edited.

## Fixed inputs and containment

| Input | Verified value |
| --- | --- |
| Core source and installed code | `a2a162ce67f0eccafee537bd75a97069c1280788` |
| UI export, compiled and served | `5a9090c7566edc7df1b3afa8facb7134dd420eca` |
| Native executable | Accepted `native-rehearsal-r1-validation/codex-pkg/shim/codex` launcher; NativeProgram bound to its recorded `@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex` |
| Native version / SHA-256 | `codex-cli 0.160.0` / `12eb3e81114588aca3b7998f4f19e8997b056aca08e57a7ca7c8a3ec8c652aad` |
| Core wheel SHA-256 | `32dba51f380dcb989e07ffac472cecd4e949ed0d43e280ff8252d86150b7c45f` |
| Browser tooling | Chrome `153.0.8010.36`, locked Playwright `1.64.0`, Node `22.22.0` |

Both commits were exported with `git archive`. A fresh Python 3.12.3 environment
installed the unchanged core lock and built/installed its wheel; the UI used
`npm ci --no-audit --no-fund` and `npm run build` with its unchanged lock.
All 76 installed package files match source and wheel. Both normal console logs
show 72 core modules imported exclusively from this installation, with completed
startup/shutdown. All 233 core and 56 UI exported files remained unchanged.
All four served distribution files matched their build hashes before and after
restart; `provenance.json` and `evidence/*-served-assets.json` retain those hashes.

Core, canned provider, native CLI and Chrome shared a fresh user/network/PID/mount
namespace with only loopback and no external route. Private `/tmp`, HOME, managed
native stores, workspace, credentials and browser profiles were isolated. Ports
18321/18322 served core/the accepted unchanged fake OpenAI provider. Provider
keys were freshly generated synthetic values. No personal CLI homes, owner
sessions, credentials, browser profiles, suppressed material or another worker's
unfinished tree were used.

The normal installed `qorqut-core serve --installation … --transport-config …
--ui-root …` ran real workers and the installed
`python -I -m qorqut_core.runtime.hook_client`. There was no hook sink, MCP probe,
SQL mutation, job pumping, API/WS interception or automatic native launch.
Schema 7, sixteen operations and five handlers were observed. Existing focused
suites were not rerun. No dependency upgrade, global installation, PR, merge,
deployment, VM/service change or delegation occurred.

## Connected observations

Actual compiled-UI login established HttpOnly, SameSite Strict cookie sessions.
Normal provisioning produced recorded Bash and private managed hooks/MCP config
and credential. Before manual input there were zero native processes/owners,
setup was `ready/configuration_installed`, capture was `waiting_session`, and
`input_ready=false`. Terminal protocol 2 started view-only; explicit acknowledged
takeover preceded keyboard input.

The manual launcher used `--no-daemon` and the accepted local fake-provider
overrides, with no initial native UUID or native trust/sandbox override. Codex's
actual “8 hooks are new or changed” dialog was answered with “Trust all and
continue.” Its bundled-bubblewrap warning was inspected and dismissed. Native
`/mcp verbose` reported `qorqut-core: connected (16 tools)` and listed every
actual core tool. Native rmcp initialization corroborates that observation.
The canned request's top-level tool-name list was empty; it is not used as
discovery evidence or claimed as semantic tool use.

One separately labeled official MCP SDK `agents_status_set` call used the
generated credential. Status and persisted operation
`0a9450ee-d1a9-408b-a7e9-b9ba6550f095` attribute the change to actual agent actor
`ee704afb-b9d8-470d-9874-6a706a40eced`, not the owner. This is SDK attribution,
not a native-model MCP mutation.

| Phase | Exact Terminal prompt | Exact canned reply | Ownership |
| --- | --- | --- | --- |
| Initial | `Codex native turn one.` | `Codex fixture reply one.` | First input admitted run/owner `9240e363-108c-4565-ac0b-b4182e272392`, generation 1 |
| Resume | `Codex native turn two.` | `Codex fixture reply two.` | New run/owner `33001386-fcbb-4d9a-a575-b0a365ec179f`, generation 2 |
| Console restarted | `Codex native turn three.` | `Codex fixture reply three.` | Same resumed process and owner, without replacement |

Native UUID `01a11dfa-19d9-7453-87bf-1a98fafc921c`, agent
`b8677986-515a-4a54-a31a-713d81b82513`, conversation
`da43e879-ee9a-4f22-a294-f8db55f47a37` and recorded terminal
`d34a8648-1e94-4916-9463-287ff8a4fe19` are linked in the actual durable records.
The admitted fingerprints exactly matched native processes 27441/37998 in the
private PID namespace. SessionStart ownership appeared on first submitted input,
including resumed input, not merely when configuration loaded.

Normal `/exit` positively ended the first native process and closed its owner;
Bash persisted. Through the real Terminal keyboard, shell history recalled the
original command and appended `resume <saved UUID>`. No source/session identity
was fabricated. Old entries remained byte-for-byte unchanged and no native entry
IDs duplicated. Three user turns generated six local canned requests: one user
and one native title request per turn. Displayed model labels were not inference.

With the resumed CLI idle/alive, the owned normal console stopped and restarted
with identical arguments/environment. Bash, recorder, tmux, native fingerprint,
owner/run, terminal/source bindings and held control survived. Credential/config
hashes and mtimes, Chat entries and daily bytes were unchanged. A fresh UI cookie
login restored viewing; explicit takeover preceded the third prompt. Normal hook,
capture and publication workers handled that prompt without a replacement run.

`Queued Chat stays queued.` was saved through Chat as entry
`450ea097-42ed-4638-a049-7dd75d61140a`. Its delivery remained
`pending/runtime_unavailable`, including across restart, and its text never
entered the native source. `input_ready` remained false throughout. Terminal
keyboard submission is not Chat delivery.

## Archive, entries and publication

One native source `68f6fb37-eabe-4420-9f1f-c72ad373a6ec`, epoch
`f8817544-1d0c-4e72-ab58-c4d05e12c4b4`, has two admitted run/source links.
Every immutable host chunk's offset, length, hash and bytes were checked against
the owned native file. Captured/host-ACK/parser offsets agree; prior archived
prefixes remain unchanged. The final primary file is 74,028 bytes in eight
chunks, SHA-256 `b8f1f8e31dbdf0b53a265b8b74aa60d36f3cf3b9012ec4a65812053ec37001e9`.

| Turn | User entry ID / raw offset + length | Assistant entry ID / raw offset + length |
| --- | --- | --- |
| One | `7b9b044d-40fb-4b63-b5aa-a7886b80d6bf` / 43667 + 474 | `38ac6d85-3487-4e42-aa36-7a8b35ec48b5` / 44141 + 400 |
| Two | `18d29062-2a8e-428d-8e8d-40bb7261e4fe` / 58805 + 475 | `2e780ee6-c8db-4a1c-a5ab-02f275cd610c` / 59280 + 400 |
| Three | `5bab5bd6-0f25-4c35-90cc-e756636ed262` / 70745 + 477 | `75bd8965-c875-4d80-a85a-b090b6fbef6a` / 71222 + 402 |

`conversation_list`, actual Chat and automatic
`agents/<id>/transcripts/2026-10-09.jsonl` preserve exact text, IDs and native
source provenance. The publication date follows durable UTC recording time;
native filenames and browser-local dates still show October 8. Existing source
entries retain the original stable parsing-run provenance, as specified by D40.

| Checkpoint | Raw bytes / native entries | Daily file SHA-256 |
| --- | --- | --- |
| First published | 46,940 / 2 | `baa75a5737e88e6308403cdfd60aabfcf57168a39e6b6e21baa40bfed75f277d` |
| Resumed published | 62,081 / 4 | `dbe6b02400337152ad9069265acfd375032ce41f3934a38cd6fd8dbbd52f17db` |
| Restarted and final exit | 74,028 / 6 | `4a7044dc060c635494058ea5fe26f620848c29330f037855ab56346f9ab7a392` |

The final daily file is 5,631 bytes, seven entries, publication `through_seq=7`,
zero pending entries and no publication error. Native parser `native-v1` reports
no parse-gap rows for this source. Its declared child/attachment coverage remains
unverified. This partial Chat projection is not a complete native transcript
archive. Separately, Terminal reported 45,553 locally captured bytes and **zero
host-archived Terminal bytes** before shell cleanup.

## UI evidence, retained failures and cleanup

Nine saved screenshots were directly inspected; hashes/dimensions are in
`evidence/visual-inspection.json`. Native Chat is readable at desktop 1440×960
and narrow 390×844. Key images are `first-chat-desktop-verified.png`,
`first-chat-narrow.png`, `final-chat-desktop-bottom.png` and
`final-chat-narrow-bottom.png`. A separate fresh cookie viewer scrolled the real
conversation to the last pair for the final two captures, then logged out/closed.
No DOM-only screenshot claim or external QA service was used. All 689 input and
four resize frames have matching submitted receipts; there were no WS error
frames or page exceptions. Expected unauthenticated 401s, favicon 404 and three
event-poll connection refusals during the deliberate console outage are retained.

**Typing responsiveness is not accepted.** The copied `keyboard.type` helper's
45-second receipt wait expired while a 536-byte command drained at approximately
three characters/second. Question 257 preserved the failure. Under decision 258,
all 536 bytes and submitted receipts were verified exactly once, with unchanged
control and no prior Enter, before pressing Enter once. Draining took 180.335
seconds. No text was resent/cleared, no native turn repeated, and no product fix
was made. Subsequent short text and shell-history keys used the same UI keyboard
path. This flow does not resolve the latency concern.

Two private evidence assertions were also corrected without changing product
data: an overstrict full-owner-row comparison included mutable observation time
(the stopped rows were not retained; after restart only `last_observed_at`
differed), and an assumed four-request total omitted two native title requests.
Their failed observations are retained; identity, byte, entry and publication
assertions passed independently against the actual records.

Cleanup completed at **2026-10-09 00:22:02 UTC**. Both native executions exited
normally; viewers logged out, both Chrome instances closed, both console starts
completed lifespan shutdown, and the exact recorded shell plus fake provider
stopped. All 25 saved process fingerprints were absent; ports 18321/18322 refused
connections. The empty namespace holder survived TERM; fingerprint-revalidated
SIGKILL of its owned launcher removed it through `--kill-child`. No process
remained in that namespace. One leftover socket was removed only from the private
mount. Synthetic raw evidence remains outside Git.

Final read-only checks: `verify-recorded.py`, `verify-imports.py evidence` and
`provenance.py` passed. Principal evidence includes stage snapshots/checks,
`sdk-attribution.json`, `native-mcp-discovery-checks.json`, raw archived copies,
dated copies, `restart-checks.json`, `recorded-checks.json`, browser frames and
cleanup records under `$RUN/evidence`.

This does not accept real provider login/inference, semantic native MCP calls,
Chat dispatch, Terminal raw host transfer, complete child/attachment history,
browser-MCP/VM integration, backup/restore, deployment or complete G0–G6.
