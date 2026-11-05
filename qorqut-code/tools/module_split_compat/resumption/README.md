# Baseline-resumption runner (module-split plan 11.1, S1-S4)

Creates pending work with the **baseline** code, closes it as a checkpoint, and continues it again later with whatever code
the import options select. The same checkpoint, the same continuation and the same assertions serve the baseline control and
a candidate, so "the candidate continues baseline-created work" is a comparison of two evidence files, not a claim.

| Case | Checkpoint created by baseline code | Continuation entry point |
| --- | --- | --- |
| `S1` | approved Signoff (global table archive), apply receipt `prepared`, worker not run | `SignoffService.run_pending` |
| `S2` | allowed file application stopped after the verified write: receipt `applying`, progress recorded, claim held, document `applying` | `PreparedFileExecutor.recover_pending` |
| `S3a` | two table revisions with captured snapshots, publication jobs queued, nothing in Git | `BackgroundService.run_one` |
| `S3b` | version 2 already committed to the table-history branch, SQL receipt missing, job `running` | `BackgroundService.run_one` |
| `S4a` | Context input queued with its bound filing job, no model call | `ContextServices.recover/step` + job |
| `S4b` | Context event already in Git, `record_published` failed, input `processing`, no completion | `ContextServices.recover` + job |

Variants: `none` (plain continuation), `revoked` (the saved authority is withdrawn through the real operation, then continued:
`permissions_set` deny for S1/S2, `agents_deactivate` of the coordinator for S4; S3 has no operation that retires a system
worker, so the one explicit mutation is `principals.active=0` of the `tables.version` worker, as the baseline tests do),
`conflict` (S2 only: the managed file is changed outside Qorqut first; it must not be overwritten).

Nothing here modifies baseline code, rewrites saved identities, payload hashes, operation versions, paths or authority
bindings, or builds a checkpoint by SQL. Evidence is read with plain read-only SQLite and Git, independent of code layout.

## Files

* `resumption.py` - the command line (`capture`, `control`, `resume`, `restore`, `check`, `info`).
* `resumption_cases.py` - the six cases: capture, preconditions, variants, continuation, expectations, fact reader.
* `resumption_host.py` - the **only** module importing host/Operations/Context code. `open_instance` is the single adapter
  point if the candidate composes its runtime differently; `Opened` lists the real worker entry points the cases use.
* `resumption_support.py` - import-origin control, closed-checkpoint snapshot/restore, read-only readers, evidence scrubbing.
* `test_resumption_runner.py` - focused checks of the machinery (`pytest tools/module_split_compat/resumption`).

## Layout of the artifacts directory (`--artifacts`, never committed)

```
fixtures/<case>/{instance,workspace}      the live fixture root; ALWAYS the same absolute path (saved state stores it)
checkpoints/<case>/closed.tar, closed.json  the closed checkpoint: sha256 of archive and manifest, capture facts, pins
evidence/<label>/<case>-<variant>.json    one run: imports, pins, steps, effects, before/after facts, checks, comparable digest
```

## Environment

Use a minimal interpreter that holds **third-party libraries only** (never the first-party packages). First-party code is
selected explicitly per run, so the wrong code base cannot be imported by accident:

```
G=<group root>; A=$G/artifacts/compatibility/resumption
PYV=$A/env/third-party-venv/bin/python            # pinned third-party set: env/third-party-requirements.txt
export PYTHONDONTWRITEBYTECODE=1; unset PYTHONPATH
RUN=$G/qorqut-code/tools/module_split_compat/resumption/resumption.py
BASE="--baseline-checkout $G/qorqut-code --expect-origin-under $G/qorqut-code --expect-origin-under $G/dependencies/document-history"
```

## Baseline capture and control

```
$PYV $RUN capture --artifacts $A --case S1 $BASE --tests-dir $G/qorqut-code/app/tests          # once per case, --replace to redo
$PYV $RUN control --artifacts $A --case S1 --variant none    $BASE                               # evidence/baseline-control/
$PYV $RUN control --artifacts $A --case S1 --variant revoked $BASE
```

`capture` imports the baseline checkout's real stock-host test fixtures (needs `pytest` in the interpreter); it closes every
store, snapshots the closed fixture and refuses to overwrite an existing checkpoint. `control` verifies that the fixture
equals the closed manifest, continues it, then closes and reopens the runtime and database **in the same Python process** and
repeats the completed continuation (it must change nothing and call no model), writes the evidence and restores the closed
checkpoint at the same root afterwards. Independent replay comes from running `control` again under another `--label`: each
invocation is its own subprocess.

## Candidate continuation (later)

```
$PYV $RUN restore --artifacts $A --case S1                                    # fixture == closed checkpoint again
CAND=<candidate checkout>
$PYV $RUN resume --artifacts $A --case S1 --variant none --label candidate \
  --code-path $CAND/<import-root> ... --component app=... --component operations_engine=... \
  --component context_engine=... --component dashboard=... \
  --expect-origin-under $CAND --forbid-origin-under $G/qorqut-code --forbid-origin-under $G/dependencies/document-history
$PYV $RUN check --artifacts $A --baseline-label baseline-control --candidate-label candidate     # per case/variant
```

`resume` refuses (exit 2) when the fixture is not the closed checkpoint, or when any tracked package (`bazarlyk`,
`qorqut_operations`, `qorqut_context`, `qorqut_business_docs_versioning`) would import from a forbidden or unexpected root,
and re-checks the origins after the run. `check` requires, per evidence file: candidate and control passed, same closed
manifest, forbidden baseline roots declared and different import origins, and an identical `comparable_digest` (saved-row
state by id, new rows by projection, counts, files, table-history commit subjects, effects, steps; timestamps and run-generated
identifiers scrubbed) **plus an unscrubbed `exact` part**: for table history, the ordered per-commit content manifest (parents
and, for every path in the tree, the SHA-256 of the blob bytes); for S4b (work already published at the checkpoint), the saved
event id, attempt and filing/completion bindings, and the exact Context HEAD, tree, refs and history. The case assertions
also tie published content to the saved snapshots, and the S4b completion to the original event and its publication commit.
`check --self-test` compares two baseline runs (reproducibility of the evidence itself).

Exit codes: `0` pass, `1` a check failed, `2` refused (setup, integrity or import origins).

## Adapting to a candidate

The candidate must keep the saved-state contract the readers use (table and column names listed in `resumption_cases.py`
`IDENTITY`/`STATE`, the Git refs `refs/heads/qorqut-tables` and the Context repository) - that is the compatibility being
tested. Only `resumption_host.open_instance`/`Opened` should need edits for renamed composition entry points; record any such
edit with the evidence.
