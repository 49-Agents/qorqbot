# Native file capture and local Archive transfer

D39 implementation against `205ad76d811ff1e6233c568b19b2f813b36b3560`,
2026-10-08. This is the capture callable, not connected manual-CLI acceptance.

`NativeCapture(database, roots)` uses the explicit runner SQLite file and a
separate thread-owned catalog connection with WAL, FULL synchronization and
foreign keys. Only new `native_*` tables are added. Existing spool and host
schemas, recording behavior and application assembly are unchanged.

- `register(NativeSource, RunBinding)` rechecks managed path, device/inode and
  adapter ownership spans, then verifies any captured prefix. Source UUIDs are
  stable for installation/agent/provider/native UUID/relative path. Epoch/link
  intent precedes idempotent spool registration; construction finishes interrupted
  local registrations. A verified unchanged resume links the new run without
  recapturing old bytes. Only explicit registration can admit a divergent epoch.
- `poll(source_id, max_bytes=1048576)` preserves partial and giant records as
  raw bytes in chunks of at most 256 KiB. It verifies the full captured prefix
  using receipt hashes, including after payload reclamation. Missing/replaced
  paths remain explicit gaps while a retained original descriptor can drain
  bounded late bytes. Truncation, ownership or prefix changes stop that epoch's
  capture until fresh admission. All previous bytes/epochs remain available.
- `checkpoint(source_id)` saves a finite prefix/tail hash and stat proof, plus
  the observed gap. It neither drains an outstanding suffix nor closes future
  append admission. `state(source_id, source_epoch=None)` reads current or
  historical progress. `list_registered()` returns frozen current registrations
  for every saved source under these roots; it does not scan native stores.
- `transfer(source_id, archive, context, max_chunks=16)` visits saved epochs in
  order, registers their run links, and persists exact chunk/binding/recovery
  intent before host intake. Only a returned durable receipt matching the exact
  run, source, epoch, host recovery epoch and chunk end authorizes ACK. ACK
  precedes reclaim. Interrupted transfer remains visible and resumes only on an
  explicit transfer call. Receipt hashes survive reclamation. The existing local
  host lifetime/effects guards cover registration through ACK/reclaim/intent
  settlement. Recovery/conflict holds and saved-intent recovery mismatches stop
  cleanup with a fixed observable error, preserving retained payload and intent.

Validation: after the Manager-requested recovery correction, the following
owning check passed **3 grouped tests in 7.619s**:

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 \
  "$TEST_PYTHON" -m unittest discover -s tests/transcripts -p test_native_capture.py -v
```

`TEST_PYTHON` was the existing isolated `console-test-venv/bin/python` under this
build run's private state directory. Sources and tests came from this worktree.
The tests used disposable initialized host/runner databases and synthetic
managed HOME/workspace files, with the real adapter discovery, spool, prefix
verifier and local host Archive. They asserted exact archived bytes and both
watermarks, including a >9 MiB partial/giant stream, reopen/resume, crashes before
and after spool registration, truncation/prefix/ownership/inode divergence,
unlinked-descriptor draining, finite checkpoints, lost host receipts, mismatched
receipt bindings/coordinates, and interruption before/after ACK and after reclaim.
The third group also verifies post-ACK/pre-reclaim recovery and actual Archive
conflict holds, both with saved intent and with an empty pending page after
deferred reclamation. Payloads and intent remain intact, errors are observable,
and changed host recovery identity cannot authorize saved-ACK cleanup. A bounded
guard-order check covers host registration/intake and runner ACK/reclaim outside
host SQL transactions while retaining the one-chunk transfer limit.
No unrelated terminal, adapter or browser suites were rerun.

Manager review of `a0ad716` identified that the earlier saved-ACK and empty-page
cleanup paths checked host holds too late. This revision moves the checks before
those paths and extends the existing host guards across their local effects;
the spool, Archive and migrations are unchanged.

Limits: full-prefix verification on each poll is the accepted v1 cost. File
checks are finite observations, not snapshots; unseen overwritten bytes and an
unlinked descriptor lost across process exit cannot be recovered. Capture
rechecks the supplied ownership evidence but does not admit processes or parse
provider records. Every resumed run is linked; transfer receipts use the epoch's
first admitted run, not an inferred per-byte native writer. Root composition,
hook installation, periodic polling, native projection, VM/network collection,
child/attachment completeness and G3 acceptance remain outside this lane. No
live owner/native stores, services, VMs or browser sources were used.
