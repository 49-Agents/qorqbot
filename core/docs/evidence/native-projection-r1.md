# Native projection R1

Implemented against `205ad76d811ff1e6233c568b19b2f813b36b3560`, following D39
and Manager's operation/ownership clarification in
`483181de689689844ecbcbbd92eb4d815047a1dd`. No integration or deployment claim.

The pure `native-v1` parser supports the observed Claude 2.1.287 primary human
input/completed assistant text and Codex 0.160.0 completed UserMessage/AgentMessage
shapes. Native identity, never equal text, selects entries. Codex response-item
echoes, instructions and completion summaries are excluded. UTF-8 text parts
concatenate exactly; absent/unusable native time has explicit ingestion origin.

`NativeProjector.consume(context, RunBinding, source_id, source_epoch)` verifies
the saved owner, complete archive lineage and committed bytes through
`Archive.read_chunk`. Reads occur outside writes; saved coordinates are fenced
again inside each commit. Checkpoints distinguish the completed-record offset
from a scan offset inside a partial line/chunk. Restart reconstructs at most
4 MiB of pending bytes. The budget limits newly scanned bytes (1–4 MiB), with
bounded extra reads to reconstruct that tail. Oversized lines produce a durable
range gap only after their newline. Invalid/unsupported records and conflicting
native identities also retain explicit gaps. Partial tails stay pending.

The SQL-only `append_native` expects caller-verified bytes and typed item/raw
provenance; it independently validates saved ownership and complete inventory
coverage. Default publication creates one agent-attributed `native_entry_import`
receipt per item/part. Exact replay preserves entry/operation/time/job identity,
including missing-time retries. Native user/agent entries reuse the permanent
cursor and automatic UTC dated publisher, with nullable delivery/handling.
An importing run need not still be active; its identity does not claim that its
process originally emitted the whole resumed file. Parser replacement requires
an explicit future rebuild/switch.

Migration 7 preserves all old entry columns/values and adds native companion
provenance, projection selection, checkpoints and gaps. Migrations 1–6 were
compared byte-for-byte with the base and are unchanged. Public inventory remains
16 operations / 5 handlers. Existing format-v1 message bytes remain unchanged.

Checks used the existing private `chat-test-venv/bin/python`, `PYTHONPATH=src:tests`:

```sh
python -m unittest adapters.test_native_records conversation.test_native_projection \
  conversation.test_ledger conversation.test_publication runtime.test_storage \
  archive.test_inventory archive.test_passive agents.test_registry
```

The run executed 38 tests: 35 passed initially. One new framing fixture needed a
longer oversized suffix so a scan actually stopped inside it; that corrected
test passed. The registry's concurrent-create test hit its 70 ms SQLite busy
timeout and passed unchanged on focused rerun. Those two reruns passed in 7.723 s.
All four new grouped checks and existing ledger/publication checks passed.
The remaining registry legacy-creation fixture fails because it sends
`local_terminal` to the older settings schema. The same failure was reproduced
with immutable base source; no unrelated fixture or runtime change was made.
Its private log is `native-projection-r1-validation/baseline-legacy-fixture.txt`
under the supplied build-state directory. `git diff --check` passed.

Read-only format verification of the approved private rehearsal files yielded
Claude: 4 messages (2 user, 2 agent), 18 ignored, 13 attachment gaps; Codex:
4 messages (2 user, 2 agent), 22 ignored. No rehearsal payload/log was added to Git.

Coverage is visible text only. Mixed text/tool, reasoning, unknown attachments,
unobserved semantic/delta shapes and unsupported profiles are gaps, not inferred
messages. No live scheduling/composition, FTS, tool execution or visualization,
native provider execution, browser source, operational data or VM testing is
included. Raw archive completeness and the connected manual-CLI gate remain
separate acceptance work.

## Manager acceptance, 2026-10-08

Accepted candidate `76c3deba5df4571006c26c718bf913650631a1f6` after direct
review of all production changes, migration and owning assertions. Manager
verified the clean/pushed candidate and personally ran the four focused parser
and projection checks: all passed in 8.779 seconds with no skips. They exercise
UTF-8 splitting and echo exclusion, archived framing across chunks and restart,
identity replay/conflict and transactional recovery, migration from schema 6,
and automatic dated publication of user/assistant history from a closed owner.
Private command/result evidence is in `manager-review230/` under the build state
directory. Source and tests were merged unchanged.

The broader builder run and its pre-existing legacy-settings fixture failure
are recorded above; this is not a claim that the whole suite is green. That
fixture must omit fields unavailable in its pinned old settings schema before
the next legacy-creation acceptance run. Native capture scheduling, hook
installation and actual manual-CLI/Chat acceptance remain integration work. No
deployment, browser, VM or complete-archive claim.
