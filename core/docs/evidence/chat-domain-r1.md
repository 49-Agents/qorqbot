# Chat domain and dated publication evidence

2026-10-08. Implementation branch: `codex/chat-domain-r1`.
Base: `055cbd48071d28c03cde3daf1a08521875c130a3`.
The final full commit SHA and push verification accompany the ACLA completion
report; this evidence is committed with the implementation it describes.

Executed the supplied CHAT-IMPLEMENTATION design plus Manager amendment
`756693d4893e82ed4ef982e40dfce754db7a6878` (inbox decision 113). Kept the assigned
base rather than importing concurrent coordination commits. Decision 115
authorized only the expected inventory changes in `tests/test_foundation.py`,
`tests/agents/test_agent_flow.py` and `tests/archive/test_passive.py`: 14
operations, five fixed job handlers and host schema 5. Exact-set, parity and
passive-assembly assertions remain intact.

Manager review 127 identified two recovery defects in candidate
`63001b3afd6e1dfd932b13b052fe8f842ea9ba5d`; that candidate was not accepted.
This revision follows the supplied correction design and changes only the
publisher, its existing publication tests and this evidence. An already-covered
publication with a saved error now verifies the owned directory/marker and
committed file through the existing no-follow, streaming `file_hash` helper,
syncs file/directory evidence and successfully exits its ownership context
before a snapshot-checked transaction clears the error. Its cursor, bytes,
inode and receipts remain unchanged. The error-free fast path is retained.
The publisher also syncs the owning agent directory every time it opens the
validated transcript directory, repairing the failed-mkdir-parent-sync retry
case without changing the shared workspace helper.

## Implemented and checked

- Migration 5 adds concrete immutable threads/messages/handling and conversation
  entries, delivery state, per-conversation cursors and daily publication
  receipts. Foreign keys, unique cursors/identities and update/delete/replace
  guards protect permanent records.
- `messages_send`, `messages_list`, `messages_acknowledge`, `deliveries_get` and
  `conversation_list` use the shared application registry and frozen schemas.
  The checks exercise exact UTF-8/control-character bodies, transactional
  rollback, response-loss replay, endpoint/reply routing, caller attribution,
  complete-batch handling validation, agent/self/onbash conversations,
  equal-text identities, pagination and recovery-epoch refusal.
- Safe messages remain `pending/runtime_unavailable`; their fixed delivery job
  waits on `dependency_unavailable` without terminal input or runtime startup.
  Unsafe terminal text remains stored exactly with failed delivery. Restore
  activation records the actual actor while holding unfinished deliveries as
  `deferred/recovery_review`; immutable handling and unsafe failures survive.
- The publisher uses the existing completed workspace marker and directory
  descriptors, then streams canonical JSONL through `Files.stage/attach/install/
  finalize`. There is no file publication in the public message transaction and
  no new file-effect protocol. Staging renews leases between bounded pages of 32
  entries. A >8 MiB day test advances the clock during pages and adds a message
  during staging, proving lease renewal, a frozen target and two coalesced
  replacements without a whole-day size ceiling.
- UTC midnight and delayed occurrence checks retain occurrence times and select
  files by the entry's durable UTC recording date. Both endpoint agents and
  onbash receive deterministic files beside `workspace/`. Delivery and handling
  views are excluded from the serializer. Publication reads expose saved lag
  and errors without scanning files or creating work.
- Crash injection and reopen exercise nine foundation/publisher boundaries:
  `file_before_stage_fsync`, `file_after_stage_directory_fsync`,
  `file_after_receipt_insert`, `conversation_after_attach_commit`,
  `file_after_rename`, `file_before_receipt_commit`, `file_after_receipt_commit`,
  `conversation_before_publication_commit`, and
  `conversation_after_publication_commit`. Original-epoch jobs reconcile and
  finish without another message. These are injected exceptions and real
  filesystem/SQLite reopen tests, not claims of a process-kill campaign.
- Additional checks cover a transient ENOSPC followed by restart, lost/expired
  claims, identical first content, unexpected content, symlinks/special files,
  replaced parents, invalid ownership, and restore holds both before and after
  receipt attachment. New messages cannot bypass a held daily partition.
  Unknown stage orphans retain the existing Files semantics.
- Per Manager's explicit boundary, the ordinary file installer uses its pinned
  directory/hash/recovery evidence. A marker changed after attachment can allow
  those exact bytes to install into the pinned directory while publication
  cursor advancement remains blocked. Marker checks gate new staging,
  attachment and advancement; authored persona content is preserved.
- Revision regressions inject a marker-read EIO after the publication watermark
  commits. Both execute and original-epoch inspect recover without another
  message, including reopen, while preserving exact bytes/inode/receipt count.
  Failed ownership-context exit retains the saved error. Missing/changed files,
  a symlink, a wrong marker and a recovery hold remain unresolved. A one-shot
  owning-parent ENOSPC is followed by a successful owning-parent sync before
  staging and publication durability are recorded.

## Commands and results

Python 3.12.3; SQLite 3.45.1. Dependencies were installed only in private test
virtual environments. Below, these variables abbreviate the actual absolute
paths used by the commands:

```sh
chat_run=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04
chat_tree=/home/alp/qorqut-core/worktrees/chat-domain-r1
chat_artifacts="$chat_run/chat-r2-artifacts"
source_python="$chat_run/chat-test-venv/bin/python"
wheel_python="$chat_run/chat-wheel-venv/bin/python"
```

Revised source check, from the worktree; **22 passed in 71.692 seconds**:

```sh
TMPDIR="$chat_artifacts" PYTHONPATH=src:tests "$source_python" -m unittest \
  messaging.test_messages messaging.test_transports \
  conversation.test_ledger conversation.test_publication -q
```

Wheel build and install, both exit 0:

```sh
"$chat_run/chat-test-venv/bin/pip" wheel --no-deps --wheel-dir "$chat_artifacts/wheels" .
"$chat_run/chat-wheel-venv/bin/pip" install --no-deps --force-reinstall \
  "$chat_artifacts/wheels/qorqut_core-0.1.0-py3-none-any.whl"
```

Wheel SHA-256:
`b435de39ca6595ede6535bc4fc234cfdb6f80e030d5ec75b9ab53a96441f1374`.

Installed check, working directory `$chat_artifacts`, with only the tests on
`PYTHONPATH`; **4 passed in 11.611 seconds**:

```sh
TMPDIR="$chat_artifacts" PYTHONPATH="$chat_tree/tests" "$wheel_python" -m unittest \
  conversation.test_publication.Publication.test_committed_error_revalidates_without_new_message_execute_and_inspect \
  conversation.test_publication.Publication.test_committed_error_refuses_file_ownership_mismatches_and_recovery_hold \
  conversation.test_publication.Publication.test_transcript_parent_sync_is_retried_before_publication_becomes_current \
  messaging.test_transports -q
```

The imported package path was verified as
`chat-wheel-venv/lib/python3.12/site-packages/qorqut_core/__init__.py` below the
private run directory. All 50 product Python files were byte-compared with the
installed package and matched. HTTP/MCP checks use real isolated loopback servers and the official MCP
SDK; installed runs use the generated console entrypoint. Synthetic identities,
credentials and data are temporary. Final logs and wheel stay outside Git in
`$chat_artifacts` (`source-tests.log`, `wheel-build.log`, `wheel-install.log`,
`installed-tests.log`). `git diff --check` passes.

The reviewer's unchanged reproduction was also run against the revised
installed package, from `$chat_artifacts`:

```sh
TMPDIR="$chat_artifacts" PYTHONPATH="$chat_tree/tests" "$wheel_python" \
  "$chat_run/review-chat-r1-etvc9rYx/publication-diagnostics.py"
```

It exited 0: the saved-error case reported `current`, `error=null` after retry
and reopen, with one message; the mkdir case recorded the failed owning-parent
sync followed by successful owning-parent syncs and `current`, `error=null`.
Output is retained privately in `reviewer-diagnostic-replay.log`. Actual power
loss was not tested or claimed.

The initial candidate's 23 source and six installed checks remain recorded in
the prior commit and private `chat-artifacts/` logs. They did not cover these
two review findings. This revision retains the existing publication
receipt/recovery checks and adds three focused regression methods. Unrelated
foundation suites were not rerun.

## Completion boundary

This is offline chat storage, conversation reads and durable dated publication.
Domain tests explicitly drive the typed queue. Automatic publication in the
combined running headless server remains the separate console-worker integration
acceptance. Browser/UI, VM and real provider execution were not exercised.
Native normalization and echo verification, assistant ingestion, terminal
delivery, browser/file cards, exports and whole G3/G4 acceptance remain open.
Complete daily replacement has the agreed write amplification; there is no
incremental append engine. No global plans, transports, Files/Jobs semantics,
worker service or agent seeding/lifecycle code were changed. No subagents,
merge or deployment were performed; independent Manager review remains pending.
