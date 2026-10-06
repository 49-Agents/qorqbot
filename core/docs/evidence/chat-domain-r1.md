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

## Commands and results

Python 3.12.3; SQLite 3.45.1. Dependencies were installed only in private test
virtual environments. Below, these variables abbreviate the actual absolute
paths used by the commands:

```sh
chat_run=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04
chat_tree=/home/alp/qorqut-core/worktrees/chat-domain-r1
chat_artifacts="$chat_run/chat-artifacts"
source_python="$chat_run/chat-test-venv/bin/python"
wheel_python="$chat_run/chat-wheel-venv/bin/python"
```

Source check, from the worktree; **23 passed in 99.999 seconds**:

```sh
TMPDIR="$chat_artifacts" PYTHONPATH=src:tests "$source_python" -m unittest \
  messaging.test_messages messaging.test_transports \
  conversation.test_ledger conversation.test_publication \
  test_foundation.FoundationCase.test_config_validation_secrets_and_snapshot_defaults \
  test_foundation.FoundationCase.test_migration_failure_rolls_back_and_upgrade_preserves_records \
  agents.test_agent_flow archive.test_passive -q
```

Wheel build and install, both exit 0:

```sh
"$chat_run/chat-test-venv/bin/pip" wheel --no-deps --wheel-dir "$chat_artifacts/wheels" .
python3.12 -m venv "$chat_run/chat-wheel-venv"
"$chat_run/chat-wheel-venv/bin/pip" install "$chat_artifacts/wheels/qorqut_core-0.1.0-py3-none-any.whl"
```

Wheel SHA-256:
`d4a325bab0c973b6d871260a8ca27fa1eb3c7f3649e260fe2fb3e97978f0ac1b`.

Installed check, working directory `$chat_artifacts`, with only the tests on
`PYTHONPATH`; **6 passed in 19.693 seconds**:

```sh
TMPDIR="$chat_artifacts" PYTHONPATH="$chat_tree/tests" "$wheel_python" -m unittest \
  messaging.test_messages.Messages.test_atomic_send_exact_body_and_receipt_replay \
  messaging.test_transports \
  conversation.test_ledger.Ledger.test_latest_before_after_empty_pages_and_equal_text_identity \
  conversation.test_publication.Publication.test_crash_receipt_boundaries_recover_without_another_message \
  conversation.test_publication.Publication.test_restore_holds_pending_receipt_delivery_and_new_job_for_same_partition \
  archive.test_passive -q
```

The imported package path was verified as
`chat-wheel-venv/lib/python3.12/site-packages/qorqut_core/__init__.py` below the
private run directory. The passive check also verifies all loaded core module
paths. HTTP/MCP checks use real isolated loopback servers and the official MCP
SDK; installed runs use the generated console entrypoint. Synthetic identities,
credentials and data are temporary. Final logs and wheel stay outside Git in
`$chat_artifacts` (`source-tests.log`, `wheel-build.log`, `wheel-install.log`,
`installed-tests.log`). `git diff --check` passes.

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
