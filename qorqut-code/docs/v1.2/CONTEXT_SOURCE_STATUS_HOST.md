# Context source status — host (H2-host)

Host implementation of the frozen contract `docs/v1.2/CONTEXT_SOURCE_UI.md` (planning `a5ec9b8`, amended by `5d3273d` after
question 2155, and its Host subsection of the paired implementation review at planning `75f5426`). Branch `v1.2/context-source-status`, from host `b9ce256`; read-only dependencies are Operations `2d697fa` and
Context `32431e1`. It changes host source only: no Operations or Context edit, no schema, no new table, writer, grant, Signoff or
routine behaviour, and no queue execution change. The accepted completion contract in `CONTRACTS.md` remains authoritative.

## One current-source filing presentation

`ContextFiling.document_filing(uow, ctx, row)` (`app/bazarlyk/domains/documents/filing.py`) is the only place a document's filing
is interpreted. It returns `(filing, status)` from the Operations completion read model (`completion.applicable`/`describe`)
over the document's *current* source digest. Every reader takes it from there:

| Reader | Where |
| --- | --- |
| Unscoped `context_list/get/search` | `DocumentOperations.present` |
| Scoped `context_list/get/search` (`ContextLibrary`) | `ContextLibrary.presented`, from the registered row (not the relationship-projected copy) |
| Note and journal snapshots (`GET …/notes`) | `DashboardCompatibility.page_view`; journal `filerSubmissions[].filing` |
| The file response (`GET …/context/file`) | the unscoped read, passed through |
| `context_dump` rows | the same projection (never retryable: nothing files, retries or retracts a dump) |

`filing` is the existing completion object (`filing_id, source_revision, input_id, job_id, state, published_commit,
completion_state, source_current, error, queue_position`) plus the read-only facts:

| Field | Meaning |
| --- | --- |
| `document_revision` | the registered document row revision (distinct from the immutable `source_revision`) |
| `operation` | the applicable accepted source operation `add`/`edit`/`remove`; null when unfiled |
| `status` | derived summary: `pending`, `processed`, `failed` or `retracted` |
| `attempts` | recorded attempts of the filing's input; zero when unfiled or never attempted |
| `job_state` | the actual bound registered job's state, or null |
| `retryable` | whether an explicit `context_retry` can meaningfully progress now; a hint, never authority |

A source no accepted filing matches reads the existing unfiled object (null identifiers, `state="unfiled"`, `status="pending"`,
`retryable` true for an unarchived upload): never the last successful filing, and a historical publication never marks newer
content processed. Existing agent redaction applies: an agent that did not initiate the filing reads `error` as `{code}` only.
A source whose file cannot be read right now has `filing: null` and `filing_status: null` on list/search (unavailable — not
unfiled, not any state); a read of the body itself fails with the library's own error. The summary `filing_status` and
`metadata.status` are derived from `filing`, never the reverse.

### Status mapping

| `filing.state` / `completion_state` | `status` |
| --- | --- |
| unfiled, queued, processing, blocked, discard_requested | `pending` (blocked keeps its exact state and error) |
| published / pending (or no receipt yet) | `pending` |
| published / applied | `processed` (`retracted` when `operation` is `remove`) |
| published / conflict | `failed` (the published commit stays on the filing) |
| discarded | `failed` |

### `retryable`

It is decided by the same `retry_branch` that `retry` dispatches on, so the hint cannot offer what the retry would refuse:
true for `admit` (unfiled source), `control` (blocked input with no pending retry control) and `requeue` (bound job failed before
its completion applied, program definition unchanged); false for a pending control, a discarded input, a cancelled/stopped job,
a changed program definition, an applied or published-and-waiting filing, archived documents and non-upload kinds. Backend
prepare/apply still decides and rechecks every request.

## Retry and retract

* `context_retry` and `context_retract` enforce a **supplied** `expected_revision` with `require_revision(row, …)` before they
  admit or change anything (decision on question 2155, option a). A mismatch is the existing `revision_conflict` with
  `current_revision` and no plan, effect or token replacement; omission keeps the existing permitted contract. Prepared-target
  checks at apply, current authority and ordinary idempotency replay are unchanged: an identical successful request replays its
  receipt even if the source has moved on since.
* The retained business `POST …/context/retry` route fills `expected_revision` from the document it has just read when the body
  gives none (unchanged); a body that supplies it is enforced as supplied.
* The Global adapter `POST /api/root/context-library/{id}/(retry|retract)` accepts exactly `expected_revision` and
  `idempotency_key`. The invalid `force` argument is gone; an unsupported field is `validation_error` with `field_errors`.
* Diagnostic fact payloads `context_filings` (`filings[]`) and `context_upload_map` (`{upload_id, versions[], published[]}`) and
  every mutation authority are unchanged. `jobs_get` keeps the completion-only filing fields (no display facts).

## Namespace-limited queue status

`app/bazarlyk/domains/documents/queue_status.py::context_queue_summary(connection, business_id)` reads the Operations queue's own
rows and returns `{queued, processing, blocked, discard_requested, publication_pending_completion, completion_conflict,
queue_blocked, blocked_input_id}`:

* the first four are the exact unfinished input states of this namespace (a business, or Global when `business_id` is None);
* the next two join this namespace's *published filings* with pending (or not yet recorded) and conflict receipts — an applied
  receipt counts for neither, and a graph or Action publication with no filing is never fabricated as pending;
* `queue_blocked` is the shared queue's fact across all namespaces; `blocked_input_id` is the blocked input's ID only when it
  belongs to this namespace;
* nothing is derived from generic job activity, nothing starts, initializes or writes, and a database without the queue/filing
  tables is an explicit `operation_unavailable`, never an empty summary.

Routes: `GET /api/biz/{slug}/context?view=processes` returns `{"context_queue": {…}}` and the full view carries the same object
under `context_queue`; **new** `GET /api/root/context?view=processes` is the owner's read of Global-owned work (not a union of
businesses). It refuses an agent or non-owner (`forbidden`), a selected business (`scope_required`) and any other view or query
field (`validation_error`). It is deliberately **uncached** (`Cache-Control: no-store`, no ETag), so it cannot serve a stale 304.

## Change-feed invalidation

* `context_queue` is a new coarse, **global-only** kind (`GLOBAL_ONLY_KINDS`): a blocked head affects every namespace; payload
  reads stay authorized and namespace-limited. It is **additive** — a matching operation bumps it in addition to `jobs_routines`,
  `learnings`, `signoffs` or `business_setup`, never instead of one.
* Mapped by committed operation-name prefix: `context_`, `actions_`, `notes_upload`, `journal_upload_to_filer`, and the `job`,
  `learning` and `signoff` families (conservative extra bumps are acceptable).
* The three accepted queue-driver transactions are tagged: `_write` → `context_queue_driver_write`, `reconcile` →
  `context_queue_driver_reconcile`, `_start` → `context_queue_driver_start` (bodies, boundaries and order unchanged).
* The existing gates are intact: only a successful commit that changed a row publishes, so an empty driver tick, a read and a
  rolled-back write do not advance it; no trigger, schema change or observer service was added.
* Cacheable routes now depend on it: business `…/context` (graph/default/processes variants) and `…/notes` (also `root/notes`).

## Body cursor

A scoped `context_get` body cursor is bound to the document identity (id, business, kind), the query, the caller's scope
(business, allowed businesses, global capabilities), the body text hash and the selected range. Row revision, stored metadata and
the file digest are not part of it, so a filing completion (which rewrites them) does not strand a reader in an unchanged body;
a body change, another document or another scope still invalidates it.

## Interpretations (disclosed, none changes a contract field)

* Journal `filerSubmissions` entries carry exactly `uploadId, sourceRevision, sourceHash, sourceDate, createdAt,
  changedSinceSubmission, filing`; the old model-summary, partial-filing and previous-receipt fields are gone.
* `context_item` no longer carries top-level `failed/retryable/attempts/error` duplicates; the old receipt interpretation and
  `filing_status()` were removed.
* A published filing with no recorded receipt (`completion_state` null) counts as `publication_pending_completion`, and reads `status` pending.

## Global source operations (root dispatcher)

`HTTPAPI` already dispatches `POST /api/root/operations/{name}` for an allowlist of owner operations. Exactly eight names are added to
it (`app/bazarlyk/transport/http.py`): `context_list`, `context_get`, `context_search` (raw Global sources, `scope: shared`),
`context_retry`, `context_retract` and `context_queue_get`, `context_queue_retry`, `context_queue_discard` (the exact-input commands).
Nothing else changes in the dispatcher:

* it is still the owner's HTTP identity (`Authentication.installation_owner`), the registered schema, policy and prepared-application
  path, and the flat splitting of `idempotency_key`/`expected_revision` from the body;
* a `business_id` or `review_business_id` in a root body is refused (`forbidden`) before anything runs, for every one of the eight;
* there is no generic catalog dispatch, no new grant, and no queue `submit` or `list`: `context_queue_list`, `context_queue_submit`
  and other neighbours stay `not_found` at the root;
* the library adapters `POST /api/root/context-library/{id}/(retry|retract)` keep working and share the same revision enforcement.

Observed refusals of another namespace's records (existing behaviour, recorded, not changed): from Global a business source or
input ID is `not_found` for `context_get`, `context_retry`, `context_retract` and for each queue command; a Global input is
`not_found` from a business dispatcher. A business dispatcher's `context_retry` of a Global source reaches the prepared-target check
and is `revision_conflict` (409), and `context_retract` of it is `forbidden` (403), with no effects in either case; the unscoped read
of a Global document by a business owner is existing access and is allowed. `state_conflict`, `control_pending` and
`invalid_transition` are 422 (the HTTP layer's fallback), `revision_conflict` and `idempotency_conflict` are 409.

## Captures, generator and checks

`app/tests/context_source_status_capture.py` is a deterministic generator; `app/tests/fixtures/context_source_status/captures.json`
is its output: `provenance`, `limitations`, `normalization` and `captures` (name, principal, transport, request, response). It covers
the business source in every state (queued, processing, blocked with owner and other-agent redaction, retry with stale and correct
revisions, published, applied, retraction, changed after publication, discard requested/discarded, completion conflict, stopped job,
notes and journal snapshots, namespace queue summaries, ETag 200/304/changed, the Global adapter's errors, and a business body cursor
dying with a real body change) and, from a second fresh composition, the Global operation calls: raw list/get/search at
`/api/root/operations/*` with `scope: shared`, long-body paging whose cursor survives the filing completion and is refused by another
document, `context_queue_get` for a queued, blocked, retry-pending (with its pending control), published and never-started-discarded
input, a refused wrong-state command, a refused second command with `control_pending` and the idempotent replay, source commands with
stale, correct and replayed revisions, selector refusal, foreign-scope refusals and list paging with a cursor that belongs to another
query.

Every entry names its **transport**:

| Transport | What it is |
| --- | --- |
| `http-dispatcher` | the real `HTTPAPI` over a synthetic in-process Starlette request: origin/CSRF/JSON checks, operation and route dispatch, option splitting, selector refusal, `failure` mapping, the real single-owner principal resolution. Only the socket and an unused signing key are faked. |
| `application-invoke` | an agent principal calling `Application.invoke`; the HTTP layer's agent session resolution is not run, so this is **not** HTTP evidence. |
| `route-handler` | one retained route handler called directly for an agent principal (the owner-only Global process view), errors mapped with the HTTP layer's own `failure()`. |

Normalization is typed and applies only to the captured exchanges: provenance (the exact dependency commits) is never passed through it.
UUIDs are real version 4 values (entropy is deterministic during generation, so id-ordered pages repeat); timestamps are kept in two
domains (the injected queue/executor clock `2026-01-01T12:00:00Z + advances` verbatim, wall-clock instants remapped onto an ordered
`2026-01-01T00:00:00Z + n seconds` timeline, instants within 10 ms sharing a value; cross-domain comparisons are not meaningful);
dates other than `2026-01-01` collapse to it; content hashes, commit ids, ETags and the Context's five-character event/source tokens
become stable synthetic values of the same shape. Opaque cursors are decoded, normalized and re-encoded with the library's own encoder
so a request cursor still equals the response cursor it came from; **a normalized cursor is not valid against a live host.**

```
cd app
export QORQUT_TEST_WORKSPACE_ROOT=… QORQUT_TEST_TEMPLATE_ROOT=…     # as for the other host tests
PYTHONPATH=.:tests python tests/context_source_status_capture.py --write      # regenerate
python -m pytest -q tests/test_host_context_source_http.py                     # the real-dispatcher cases
python -m pytest -q tests/test_host_context_source_status.py -k "captures or normalizer"   # drift, types and relationships
```

Limitations: an isolated synthetic composition (injected model runner and queue/executor clock, no server, TLS, session cookie,
provider, native session or operational data), fixture provenance and not installed-product proof: the stock runtime still does not
compose the queue/filing tables or driver (see `CONTEXT_FILING.md`), and this branch needs the Operations and Context commits above
while the host's recorded dependency pins are unchanged. Mapping-level change-feed evidence for producer families this fixture does not
run (`actions_*`, Signoff apply, routines) is by operation name only. `tests/test_change_feed.py` and several older service tests cannot
be collected at the host base (they import the removed `bazarlyk.contracts`); its one `jobs_get` event expectation was updated for the
additive kind but not run.
