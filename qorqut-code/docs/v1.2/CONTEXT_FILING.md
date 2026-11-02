# Context completion for uploads and registered jobs

Branch `v1.2/context-completion`, on top of the accepted P4B writer checkpoint (host `33a0763`, Operations `5faa01e`). The
Operations half (source versions, filings, the terminal-outcome outbox and the completion queries) lives in
`qorqut_operations/context_filing/` and is described in the Operations `docs/EXTRACTION.md` section "Source versions, filings
and terminal outcomes". This document is the host half: what the upload, note, journal, accepted-learning and instruction
paths now do, and what is deliberately left.

Dependencies, unchanged and read-only: Context library `2e59b525ddc0cedba3bc342d901520b0643a7c54`; the P6 host
`f0cff1138fc600cda62878ddd812edc33d0bc75d` and Operations `3f7c3cb7964901892b22564219ddeb3d21020443` were not edited. No pin,
receipt or role source changed.

## The path

```
upload / note / journal / learning / instruction
  -> one prepared operation (DocumentOperations -> context_filing.admit):
       frozen source version  +  filing binding  +  registered job (context.file v2 | context.instruct v1)  +  GENERAL queue input
  -> the Context queue driver (alone) runs propagation under the ordered single head
  -> terminal outcome recorded by the same transaction that records published/discarded (FilingOutcomes.record)
  -> the registered job sees the stored receipt, freezes one output (context_filing_receipt), and settles
```

A job never calls a model, reads a document body or creates a queue input. While its input is queued, processing, blocked or
waiting for a discard (or the terminal receipt is not yet recorded) it returns `ProgramPending(retry_after=15)`, which defers
it and releases the serial executor, so unrelated registered programs run meanwhile. Blocked attempts are nonterminal.

## Source envelope (`qorqut-source-v1`)

One canonical UTF-8 JSON document: the exact body plus an explicit, allowlisted `metadata` object. The allowlist is closed and
is documented where it is enforced, in the module docstring of `app/bazarlyk/domains/documents/filing.py`. Bookkeeping fields
(status, attempts, filed_by, results, receipts, row revisions, timestamps) are excluded, so a bookkeeping-only change never
creates a new source version. A title or participant change does. A source over the Context library's source-byte limit is
refused before admission with an error that names the limit.

Controlled paths: `foundation/documents/{document-id}.txt` and `foundation/instructions/{job-id}.txt`. A retraction is a
distinct GENERAL removal input (not a rewrite) and is only available through `context_retract`.

## Terminal receipt, status and cancellation

* `filing = {filing_id, source_revision, input_id, job_id, state, published_commit, completion_state, source_current, error,
  queue_position}` is added to document and job reads; a document with no filing reads `state=unfiled`. Document reads (scoped
  and unscoped `context_list/get/search`, note and journal snapshots and the file response) add the read-only display facts
  `document_revision`, `operation`, `status`, `attempts`, `job_state` and `retryable` to the same object; see
  [CONTEXT_SOURCE_STATUS_HOST.md](CONTEXT_SOURCE_STATUS_HOST.md).
* `source_current` is false when the document moved on after that version was frozen; the older publication is never hidden.
  It is decided the same way for a retraction: a removal's frozen digest names the exact published content it removes, so a
  body or selected-metadata change, or missing or archived content, makes it not current. The document then reads
  `unfiled`/pending, the historical removal still records its real published outcome and settles, and its completion does not
  rewrite the newer document. An explicit `context_retry` admits the changed content as the next version (an `add` after the
  accepted removal) with a new filing, job and input; nothing is refiled automatically. An unchanged document still becomes
  `retracted`.
* Document status is derived from the applicable current filing: unfiled, queued, processing, blocked and discard requested
  are pending (a blocked input keeps its exact state and error); discarded is failed; published and applied is processed
  (retracted for a removal); published but not yet applied is pending; published with an apply conflict is failed (the
  publication stays visible through `filing.published_commit`).
* `context_retry` on a discarded filing or a cancelled job is `invalid_transition`; an explicit retry of a blocked input goes
  through the queue's own retry control.
* **Cancellation.** A cancelled bound job never conceals a publication. The driver's `reconcile()` (run at the start of every
  step) calls `completion.reconcile_stopped`, which uses the queue's own controls with the job state and error as evidence.
  The driver is the recorded requester; the canceller's identity is not recorded on the job row and is not invented, and no
  existing API cancels a non-routine job, so tests set `jobs.state='cancelled'` directly; that seam is disclosed rather than
  designed around.
  * An unstarted input is discarded at once (its receipt records the evidence).
  * A blocked input gets a pending discard whose `result_json.request_evidence` freezes `{reason: job_cancelled, job_id,
    job_state, job_error, requested_by}` at creation. When the driver resolves it, that object and the control ID are kept in
    the control result, the discarded input result and the terminal receipt (`reason_json`); if publication wins the race the
    rejected control keeps them and the input stays published. A restart between the request and its resolution changes
    nothing and starts no model call. An ordinary explicit control carries no `request_evidence` and behaves as before.
  * A retry still pending on that blocked input is rejected in the same transaction (`superseded_by_cancellation`, with the
    evidence and the discard control's ID) and the discard is created, so cancellation never starts another attempt or model
    call. A `processing` input is still reconciled for a possible publication, never presented as rolled back.

## What was removed

`context_summarize`, `context_map_generate` and every old Context filing/summarize/map program and mutation handler
(`bazarlyk/background/context.py`, `contextbase.py`). `context_upload_map` and `context_filings` are owner-only projections.
New filing no longer runs Story-background synthesis. The compatibility transport keeps
only the `retry` action. The Context process view is `{context_queue: {...}}`, a read of the Operations queue's own rows
(see [CONTEXT_SOURCE_STATUS_HOST.md](CONTEXT_SOURCE_STATUS_HOST.md)); there is no `summarizing`, `filing` or mapping object.

## Composition boundary

The real host runtime does not compose the queue/filing tables or the driver, and `register_business_domains` is not given a
`filing` port by it, so filing is unavailable in a stock runtime until that composition lands. The acceptance fixture
(`app/tests/context_completion_support.py`) composes the real host policy, application, file effects, `DocumentOperations`,
registered programs and `BackgroundService` with the Operations queue and filing storage, a real Context in a disposable
directory and an injected runner. It needs two stand-ins that composition must replace: a test-only
`inbox_deliveries.required` column (the engine's discussion notices need it; the host table lacks it) and an `action_revisions`
table (the queue's Action input trigger). The host and engine schemas also collide on Tasks, inbox and Actions tables.

## Role policy fixture

Role sources that still name `context_summarize` and `context_map_generate` in their default tool policies are rejected by the
catalog, which correctly rejects unknown operations. The test workspace copies the role source into a disposable directory and
removes only those two policy lines (`drop_removed_operations`) without weakening catalog validation. Tests that use it prove
the changed Context path under that fixture. They do not prove that the role package publishes unchanged, nor that an installed
host/role combination is ready.

## Focused checks

`tests/test_context_completion_acceptance.py` runs the real queue/UoW, `DocumentOperations`, the registered job executor, a
disposable Context and an injected runner. Not separately exercised: the accepted-learning and journal admission paths beyond the
note provenance path. The synthetic assembled component is not a stock installed-host acceptance claim; the source-receipt/pin
checks in `tests/test_host_foundation_engine.py` archive a `qorqut-operations` submodule checkout and need it present.
