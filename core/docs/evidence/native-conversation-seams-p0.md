**Native conversation seams P0 — factual exploration, 2026-10-08.**

Base: `010b551722b300ed617dbe9d4e21267a8f35f221`; initially clean branch
`codex/explore-native-conversation-seams-r1`. No connected native-history-to-dated-file
path exists at this base. Below, “implemented” means verified by source inspection;
owning tests were read, not rerun. D36 is the manual-start contract, not runtime
acceptance. Core code citations are relative to `src/qorqut_core/`; `Q/`
denotes the pinned Qorqut source root below.

Starting evidence: [native-capture-p0](native-capture-p0.md), then
[native-rehearsal-r1](native-rehearsal-r1.md), accepted at
`13227a82e0797b081d704998e0dc568c7c60cc12` with
[Manager clarifications](native-rehearsal-manager-acceptance.md). Applicable
AGENTS.md, IMPLEMENTATION-PLAN, CONTRACTS, TERMINAL-OWNED-SESSIONS and M03/M04/M05/M07/M08
were read. No applicable CLAUDE.md was present.

**1. Observed pinned-native records.** Original synthetic files were inspected
for structure, identifiers, byte coordinates and hashes only. Let `R` be
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-rehearsal-r1-validation`.
`C` is the unique `R/native/claude/home/.claude/projects/*/d97e2375-5404-4630-a515-67f23c1871ce.jsonl`;
`X` is `R/native/codex/home/.codex/sessions/2026/10/08/rollout-2026-10-08T12-09-54-01a11c47-4367-7b23-93da-768092953c1f.jsonl`.
Native citations use one-based JSONL lines.

Claude **2.1.287**: C:L5/L28 are `user`, `isSidechain=false`,
`origin.kind=human`, `promptSource=typed`, with `message.role=user` and string
content. They carry distinct `uuid` and `promptId`; the latter matches
`UserPromptSubmit`/`Stop` hook `prompt_id`. C:L17/L30 are `assistant`, with
`message.content[].type=text`, `message.stop_reason=end_turn`, `apiBlockIndex=0`,
their own record `uuid` and `message.id`. `parentUuid` chains through attachment
records, not just user/assistant pairs. Session identity is `sessionId`, also
present in non-message records. Not every record has a UUID.

Codex **0.160.0**: X:L1 has `session_meta.payload.id` and `source=cli`;
all 26 records have successive `ordinal` values 0–25. Root input appears as
`event_msg/item_completed/UserMessage` at L10/L21 with `item.id`, `client_id`,
`thread_id`, `turn_id`, and text blocks. L9/L20 also contain the input as
`response_item/message/role=user`, with **different** message IDs and
`internal_chat_message_metadata_passthrough.content_item_kinds=[user.text]`.
The earlier role=user item L6 contains instruction/environment kinds. Thus role
alone does not identify root input. L11/L22 `AgentMessage` items use `Text`
blocks and share IDs with L12/L23 assistant `response_item` `output_text`
blocks. Those response items carry `metadata.retained_source` identity,
revision and `complete=true`. L15/L26 `task_complete.last_agent_message`
repeats the answer; hook `turn_id` matches the turn. These are recorded
correlations, not proof of a universal deduplication or ID-stability contract.
Resume retains the session and original prefix IDs. C adds new message UUIDs;
X:L16–17 append `thread_settings_applied` for the same thread, with no new
`session_meta`. Rewrite/compaction stability remains unobserved.

Neither final file contains semantic delta records, partial JSONL, or actual tool
calls/results. The rehearsal's provider stubs emitted SSE deltas; that does not
establish native-file delta persistence (rehearsal:156–192,497–521). Actual tool
use, children and external attachment coverage remain absent; C contains 13
attachment records with unsupported discovery layouts. Core's
`tests/adapters/fixtures/*-owned.jsonl` tool/queued/image records are synthetic:
Claude `assistant.message.content[]` blocks with `type=tool_use` have an `id`; Codex
`response_item/function_call` has `call_id`. Neither establishes observed native
tool-result coverage. `Q/tests/test_terminal_owned_chat.py:443–466` models older
Codex 0.157, including `phase=final_answer`; the observed X assistant records
have no `phase` or `channel`. Its visible-text parser excludes tools/reasoning/
children, accepts legacy `event_msg/user_message`, and falls back to byte offset
without a native ID (`Q/bazarlyk/global_chat/terminal.py:31–85`). This is static
format knowledge, not complete capture evidence.

**2. Ownership observations to raw coordinates.** `NativeBinding` assumes the
caller already proved process/run association. `NativeSource` returns provider,
binding, paths, device/inode, observed size and hashed ownership spans, with
`archived=false`; it returns no durable source/epoch ID, retained descriptor or
checkpoint (`adapters/types.py:129–215`). `discover` matches the exact supplied
session/filename, ignores cwd for ownership, and rechecks retained proof spans
and the named inode before closing the descriptor. Growth can conceal changes
outside retained spans; the observation does not freeze later bytes
(`adapters/discovery.py:77–101,129–224`; owning tests:
`tests/adapters/test_adapters.py:609–672,817–899`).
Multiple paths remain separate observations without a durable resume mapping.
Separate child files are skipped. Embedded Claude child records cannot prove
ownership, but an independently proven primary file remains eligible with gaps.
Attachment references are not captured (`adapters/discovery.py:104–126,143–224`).

`translate_hook` requires that source, session/path and caller sequence/time;
it rejects child/platform identity claims and returns an unpersisted activity
observation (`adapters/hooks.py:15–42`). No durable hook sink or admission-to-source
mapping calls it. Capture `RunBinding` adds installation identity but omits
conversation/provider; `Source` holds source/native/parent IDs. Epoch locator,
file identity and capability evidence are opaque supplied strings.
`register_run`, `register_source`, `link_run`, `begin_epoch` persist supplied
lineage; none resolves an adapter observation (`transcripts/spool.py:68–191,259–415`).

Since the earlier P0, `receipts(..., after=0, through=None, limit=16)` preserves
original chunk boundaries/hashes after reclaim. `verify_file_prefix(..., fd,
expected_device=..., expected_inode=..., through=...)` compares a frozen prefix
through a borrowed descriptor, returns prefix/tail hashes and stat evidence,
and rejects changes during inspection. It neither persists the proof nor opens
or attests a native path (`transcripts/spool.py:520–595`; `transcripts/prefix.py:46–124`;
`tests/transcripts/test_prefix.py:173–232,348–388`). Production callsite tracing
finds no native reader, checkpoint/mapping writer, parser or collector connecting
these callables. Existing production source registrations are terminal-only.

**3. Raw intake, canonical entries and dated publication.**
`CaptureSpool.append(source_id, source_epoch, offset, payload)` atomically stores
bytes/cursor, splitting into at-most-256-KiB chunks. Retries require identical
offset/length/hash boundaries; equal bytes at different coordinates remain
distinct. `pending`, `acknowledge`, `reclaim` separate transfer, durability assertion
and payload removal (`transcripts/spool.py:433–518,597–637`).

Host `Archive.register(context, binding, source, source_epoch, metadata)` accepts
registered-caller attribution and declared lineage; it does not authenticate
the native process. `receive(context, binding, chunk)` accepts a validated
`Chunk(source_id, source_epoch, offset, length, sha256, payload)`, requires
contiguity, and returns `ArchiveReceipt` only after object/directory fsync and
inventory/watermark commit. Exact duplicates reverify bytes; conflicts are
preserved separately and close intake/ACK admission. `state` is historical;
`read_chunk(context, source_id, source_epoch, offset)` verifies one committed
chunk, without authorizing ACK (`transcripts/archive.py:86–224,286–395`).
`tests/archive/test_transfer.py:14–71` supplies synthetic bytes and explicitly
calls spool→archive→ACK→reclaim; no native descriptor participates.

The canonical writer is **only**
`Conversations.append_message(tx, agent_id, message, operation_id=...)`:
it verifies the exact saved coordination message. Sending atomically inserts
each participating agent's entry and publication job. Operation replay uses
`(actor_id, operation_name, idempotency_key)` plus validated-input hash;
entry replay uses `(agent_id,message_id)`. New equal-text messages have distinct
entry IDs/increasing conversation sequences
(`app.py:245–279`; `messaging/service.py:55–77`; `conversation/ledger.py:32–74`;
`tests/conversation/test_ledger.py:9–60`).

Entry/source types and SQL permit only `kind=message`, role `user|agent`, and
a required message FK, with 64-KiB bodies. Nullable run/provider/raw coordinates
remain unset by this writer; listing assumes message delivery/handling records
(`conversation/models.py:38–66`; `conversation/ledger.py:102–103`;
`storage/migrations.py:294–309`). No native assistant/user insertion or verified
Chat-echo correlation exists. Qorqut's older `_import` matches pending echoes by
body/session/order (`Q/bazarlyk/global_chat/terminal.py:330–361`); this is not
core's verified-identity requirement (`docs/CONTRACTS.md:309`) or an implemented
core facility. No core-message/native-ID correlation was observed in the rehearsal.

`transcript_line` serializes format-v1 entries; `recorded_at[:10]` selects the
UTC day, preserving `occurred_at`. `Publisher` streams sequence-ordered daily
snapshots to `agents/<id>/transcripts/YYYY-MM-DD.jsonl` through file receipts,
then commits `through_seq`/hash/length; pending/error state is separate from raw
archive progress (`conversation/ledger.py:20–22,55–83`;
`conversation/publication.py:131–238`; `contracts.py:32–43`). Existing date/late-insertion/
restart checks use platform messages, not native replies
(`tests/conversation/test_publication.py:64–172`).

**4. Lifetime and continuity.** `OwnerCandidate` includes terminal, provider/
version, native UUID and process boot/PID/start-ticks/UID/namespace. Local onbash
`SessionOwners.connect` returns owner UUID (=run UUID)/generation: exact live
reconnect reuses it; a different live candidate, including another process
resuming the same native UUID, raises `SessionConflict`. Unknown retains the
claim; verified exit closes it; stale release cannot revoke its successor.
Provider changes after exit allocate another owner without changing the agent's
conversation. Reopen retains rows; explicit recovery activation retires them
(`runtime/models.py:20–73`; `runtime/owners.py:45–202`;
`tests/runtime/test_owners.py:16–120`). These prove supplied-binding semantics,
not terminal/hook/file association. Neither process is killed on conflict;
refusal has no independent native-source collection. Delivery remains
`dependency_unavailable`, and `runtime_get.input_ready` is always false
(`messaging/service.py:136–153`; `runtime/models.py:68–73`). Server startup
starts the job worker and owner monitor, not native ingestion
(`transports/server.py:98–113`).

The accepted rehearsal observed same-session/same-inode prefix-preserving
resume, not arbitrary manual startup. Claude's UUID was fixture-supplied;
Codex's SessionStart occurred only after first input. Claude SessionEnd fired on
stop; Codex's did not (rehearsal:278–390,430–495). Stop is activity-idle;
SessionEnd is unknown, not verified exit. Fixture evidence includes Claude
`sessions/<pid>.json` (`procStart`, `pidDomain`) and hook ancestry; Codex
lock/rollout descriptors were inspected after first input. Codex's recorder
child was Node, with the native binary its grandchild
(rehearsal:194–204,278–377). `finish_recording` closes a terminal
epoch; `link_run` does not reopen it or finalize native files
(`adapters/hooks.py:10–12`; `transcripts/spool.py:328–331,639–673`). The persistent shell's
capture run UUID is its terminal UUID, distinct from native owners
(`runner/shell.py:105–108,235–240`). No connected evidence yet attributes resumed,
competing, child/tool/attachment bytes across owner changes or application restart.

Unanswered Manager decisions: what evidence joins manual process admission to
native files and stable source/run/epoch identity; what checkpoint/finalization
and incomplete-coverage contract applies; what verified native record/echo
identity and partial/final semantics feed the currently message-only ledger?
No answer or implementation strategy is selected here.

Source provenance and inspection hashes:

| Source | Bytes / SHA-256 |
| --- | --- |
| C final | 158067 / `a9753c89ce0eef8d7fe1305bc20a6ca8090cf8a84abd20f5c015c70ee455c1c6` |
| C original fresh prefix | 151061 / `0d86705fa30ea403de57b468c3970f31e60acccea479cead7e83ae985f9801f9` |
| X final | 55505 / `e500cc96aaf3247b4dd91a23e7aff1df75e06231e27b22d52c136265c61882d9` |
| X original fresh prefix | 44580 / `7402101fb96247ca91b235a9393364b4aaf39b62af412512aee8193be048d1ac` |
| `R/logs/hooks-claude.jsonl` | `43678ee3a7403a0f79028c01aac0125bfcf93d774a4a35f39fea079195e6b9c4` |
| `R/logs/hooks-codex.jsonl` | `20869dc08da3986c3aca93a032241bddf12a0b0a9ff33071e8bef61154ba20f7` |
| Pinned Qorqut `bazarlyk/global_chat/terminal.py` | `2ef2d6f1a9752b1756fa4cf08a3817c15dc42fd158c02841e1aea77388871237` |
| Pinned Qorqut `bazarlyk/bootstrap/transcript_snapshot.py` | `42fafd56d00d773fd11ba3de6793ae35e2c31b61c3e4ff1898a8810351cdf562` |
| Private `source-sha256.txt` inventory | `e92f1b06656da3a4202091f50c950869f08ca6583f693cdddbf3af48a495b64c` |
| Private `inspect_metadata.py` | `89b9830530dceeb6e40f791c7e1457645e46f5ccb7330deb104d569073e55939` |

Qorqut reads used only PORTING's immutable `Q/`:
`/home/alp/qorqut/components/core/e71fd1e527368bfd41b9815126f4bf33452aeebb/source/`.
Its snapshot remains an 8-MiB bounded copy (`Q/bazarlyk/bootstrap/transcript_snapshot.py:16,77–124`),
not the implemented core archive. Private scripts, metadata-only output and
source SHA inventory are under sibling `explore-native-conversation-seams-r1-private/`
of R. Inspection used `git status --short --branch`, `git rev-parse HEAD`,
targeted `nl -ba … | sed -n …`, `sha256sum`, and the metadata-only Python script.
The connection search and scope check were:

```sh
rg -n 'NativeSource|verify_file_prefix|translate_hook|\.discover\(|register_source\(|append_message|archive\.register|archive\.receive|read_chunk|session_owners\.connect|SessionMonitor|Publisher|Archive\(' src
git diff --exit-code 010b551722b300ed617dbe9d4e21267a8f35f221 -- . ':(exclude)docs/evidence/native-conversation-seams-p0.md'
git diff --cached --check
```

No product imports or test execution.
Only this report changes. No native/provider execution, live operational data,
other moving worktrees, suppressed material, package/service changes, merge or
deployment; only the authorized dedicated-branch push. Whitespace and report-only
scope checks accompany the completion receipt. No complete-capture or system
acceptance claim.
