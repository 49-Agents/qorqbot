# Chat and server integration r1 evidence

Implements `CHAT-SERVER-INTEGRATION.md` on `codex/chat-server-integration-r1`, based
on `3e9ac2b1613550d81d5e24a554ad7858ebb5dad6` (the Manager merge of chat domain
`5a79f480b448c1eb3d4172e6e83947146015283d` and console hosting
`b51f3099c2cb4ed6690667882cd1bcad63cf1675`). Production code, migrations,
`pyproject.toml`, `requirements.lock` and docs other than this file are unchanged:
`git diff 3e9ac2b -- src pyproject.toml requirements.lock` is empty and the
`src` tree stays `57c8ef83e6db94c12fe750db419a03c954d07277`. The tested `tests`
tree is `e038e5f1238cd86366c98a16aadded808fcd51cd`. The bound completion report
records the final full commit SHA, push result and post-commit clean status. No
merge or deployment performed.

## What changed (tests only)

- `test_worker.py`: the synthetic `conversation_publish`/`message_delivery`
  handlers now replace only those two kinds in the fixture's local registry.
  All other handlers and every exclusion, finite-delay, recovery-hold and
  no-execution assertion are unchanged; the production duplicate-kind check is
  untouched. Before this correction both synthetic eligibility tests failed with
  `Duplicate fixed job handler` at the base.
- `messaging/test_transports.py`: before the cross-transport complete-view
  comparison, a bounded 20-second poll waits for the running server's real
  publication observation to reach `current`. Entry, authorship, handling and
  delivery comparisons are unchanged and no mutation is added to trigger
  publication. The race was not observed to fail in my runs; this removes the
  window in which the worker can change the observation between the two reads.
- `owned_server.py` / `support.py`: optional `--crash-point`, `--fault-record`,
  `--fault-exit-code` and `--claim-lease-seconds`. A matching fault point writes
  `{point, pid}` and `os._exit`s only that child; `Installation.crashed(point)`
  accepts only the fixed exit status together with a record of the requested
  point and the child's pid. `Jobs.run_next` is wrapped for the child alone to use
  the requested claim lease. Without these controls behavior is unchanged.
  Negative controls (scratch, not committed): an unreached point raises "did not
  reach its fault point", and an unrelated startup failure (exit 1) raises
  instead of being read as the crash.
- `test_chat_publication.py` (new, two flows):
  1. Real headless server (no UI root, normal entry point). Two `agents_create`
     VM agents are registered during setup and credentials are bound to their
     registered actors; onbash is retained. The worker, not the test, provisions
     all three workspaces; `lifecycle` stays `provisioning`. The owner signs in
     over the real cookie route, sends two equal-text messages with different
     keys (exact Unicode, tabs, newlines, U+2028) and replays one key; one agent
     sends to the other through the official MCP SDK; the owner messages onbash.
     Identities, bodies, entry targets, sequences and counts (3/1/1 entries,
     5 distinct entry IDs) and `pending/runtime_unavailable`, unhandled
     deliveries are asserted. After all clients close, read-only polling
     observes the recording-date `.jsonl` files; each parsed line equals
     `{"format_version": 1, **saved immutable entry}`, framing is LF-exact and no
     other name appears in `transcripts/`. A fresh sign-in sees every
     publication `current`, `pending_entries=0`, `error=null`, and complete
     pages equal the saved ones. After a normal restart the pages, file bytes and
     same-key replay are unchanged and lifecycle is still `provisioning`.
  2. A message is saved by the real application operation during setup. The
     owned child exits itself at `conversation_after_attach_commit`; the helper
     confirms the recorded point/pid/exit status. Before restart, durable state
     shows one staged receipt (`through_seq=0`, `pending_target_seq=1`), the
     publish job `running` attempt 1, the `file_install` job `queued` with that
     receipt, and no dated file. The unmodified normal entry point and real clock
     then restart with no message, `run_next` or reconcile call. Within the
     bounded wait the same publish job completes, the same receipt is installed
     (the only transcript receipt), `through_seq=1` with hash/length equal to the
     file and receipt, the single exact entry is published, readiness is true,
     the message count remains one and the recovery epoch is the original one.

## Verification

Python 3.12.3. Both environments are pinned exactly to the unchanged
`requirements.lock` (`pip freeze` identical; `pip check` clean). Private artifacts
are outside Git; `$RUN` abbreviates the private run directory and `$WT` this
checkout. Tested content was the staged tree `05380c93b38dc359309911070af8bbc9c19a40f9`
(`src` `57c8ef83…`, `tests` `e038e5f1…`).

| Actual check | Result |
| --- | --- |
| `TMPDIR=$RUN/tmp PYTHONPATH=src:tests $RUN/test-venv/bin/python -m unittest discover -s tests/transports -v` | 30 passed, 83.733 s (28 prior + 2 new) |
| `TMPDIR=$RUN/tmp PYTHONPATH=src:tests $RUN/test-venv/bin/python -m unittest messaging.test_transports -v` (once, final source) | 1 passed, 9.197 s |
| Stability, source: flow 1 five times, flow 2 twice, separate processes | 7/7 passed (9.9–10.9 s; 32.06, 32.11 s) |
| Wheel built from `git archive` of the staged tree: `pip wheel --no-build-isolation --no-deps` | `qorqut_core-0.1.0-py3-none-any.whl`, SHA-256 `106159ad2d02baf3211fd95ac37e01dd227763218b5396778e8df0ff79c907e7` |
| From `$RUN/outside`, separate pinned venv with only that wheel: `PYTHONPATH=$WT/tests $RUN/installed-venv/bin/python -m unittest -v transports.test_chat_publication transports.test_worker.WorkerRecovery.test_bounded_original_epoch_inspectors_delay_and_exclusions transports.test_worker.WorkerRecovery.test_inspector_exception_and_missing_handler_remain_delayed_without_execution messaging.test_transports` | 5 passed, 52.584 s |
| Same two flows from `$RUN/outside` with `PYTHONVERBOSE=1` in every child (`verbose_runner.py`, outside Git) | 2 passed; four child servers (two normal entry points, one crash child, one restart) each loaded `qorqut_core` modules (36 distinct, including `transports/server.py` and `conversation/publication.py`) only from the installed venv; no `$WT/src` path appeared |
| `provenance.py` comparing all 56 `src/qorqut_core` Git blobs with installed bytes | 56 matched, 0 mismatched, 0 extra installed files |
| `git diff --check`; dependency/production diff against the base | Clean; empty |

The installed runs use the generated `qorqut-core` console script for the normal
server. The crash child necessarily uses the test-only `owned_server.py`, which
imports the installed product through the fixture's `PYTHONPATH`.

## Observations and limitations

- The child's one-second `run_next` lease governs only the initial claim.
  Publication staging calls `Jobs.progress` with its production 30-second default,
  so at the crash the publish job's `lease_until` is its last progress time plus
  30 s (observed `phase=staged`, `updated_at` 14:23:33.679, `lease_until`
  14:24:03.679). The restarted worker therefore expires the claim about 30 s
  after the crash, and flow 2 takes about 32 s with a 90-second bound. Shortening
  progress renewal in the child would be an additional test-helper choice; it was
  not made.
- This proves automatic publication of recorded platform messages by the running
  headless server and receipt-boundary recovery. It does not prove native
  assistant ingestion, terminal submission or delivery (deliveries remain pending
  with `runtime_unavailable`), VM/runtime or browser readiness, UI behavior,
  deployment or other release gates.
- Recording-date partitions are derived from each saved entry's `recorded_at`,
  so a run crossing UTC midnight is handled; the crash flow derives its date from
  the receipt target and cross-checks the entry.
- Files are observed by read-only polling; there is no live filesystem integrity
  scan, and the publication state remains a saved observation.
- IsolatedAsyncioTestCase prints "Executing ... took N seconds" for the
  synchronous setup/inspection calls inside the async tests; these are not
  failures.
