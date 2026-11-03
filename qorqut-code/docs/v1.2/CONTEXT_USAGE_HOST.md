# B3 host side: Context usage collection and owner costs

The host collects Context usage receipts into the Operations usage ledger (`qorqut_operations.context_usage`) and
exposes owner-only costs. It reads the Context library's read-only `Pipeline.usage_receipts`. Importing
`bazarlyk.context_usage` (and so the host's domain registration) needs an Operations release that carries the usage
ledger; the gitlinks and the retired private foundation receipt name the foundation pins, and the receipt cases of
`test_host_foundation_engine` do not pass until the pins are advanced together.

Backend only. The dashboard's Global cost view and the Context cost reader, the Context driver loop and the graph-edit
driver are separate owners.

## Package `app/bazarlyk/context_usage/`

| Module | Purpose |
| --- | --- |
| `collector.py` | `ContextUsageCollector` (bounded step), the typed `UsageReader` / `UsageCollection` ports, `CollectionStep`, `CollectorStorageError` |
| `coverage.py` | `collection_coverage(connection, input_id=None)`: the inventory of reserved Context events left-joined to scan records; `incomplete(facts)` |
| `operations.py` | `ContextUsageOperations`: the single owner-only `context_filing_costs` read |
| `costs.py` | `context_charges(connection, today)`: observed charges for the Global overview, streamed from the usage tables |

## The collector

```python
ContextUsageCollector(database, reader, *, driver_id, lock_path, page_size=100, max_pages=5)
collector.collect_next() -> CollectionStep      # one bounded step
collector.diagnostics()  -> dict                # facts about the steps so far; reads and changes nothing
```

* `reader` is anything with `usage_receipts(event_id, *, limit, cursor)` (the library's `Pipeline`). It is a read-only receipt
  reader, not a runner: the collector never contacts a provider or model, starts or retries a Context input, changes queue
  state, emits an inbox event, acknowledges work or opens a thread, timer or job. `driver_id` is the configured backend `system`
  principal (the ledger checks it on every write). `lock_path` is an explicit absolute private path; `page_size` is 1..500 and
  `max_pages` 1..100.
* **Inventory.** Distinct `(input_id, context_event_id)` pairs from `context_input_attempts` joined to `context_inputs`, event IDs
  not null (so historical, failed, blocked, discarded and published inputs, and a retry attempt on one event, give one pair),
  ordered by input `sequence` then event ID. A library attempt number is never inferred from a queue attempt.
* **Traversal state is in memory only**: the current pair, its page cursor and set of cursors seen, and the last completed key. A
  step reads at most `max_pages` pages of `page_size` receipts of the current pair and returns; the next step continues that
  pair. After EOF or an unavailable result the collector advances to the next pair; at the end of the inventory it wraps to the
  start (counted in `traversals`) so started, missing or unavailable evidence can later advance. A process restart is a fresh
  traversal; call IDs are immutable and the ledger's ingest is replay safe, so a replay counts nothing twice. The page size is the
  same for a whole traversal.
* **One lock per installation.** Every step takes a nonblocking advisory `flock` on `lock_path` (`mkdir` 0700, file 0600,
  `O_NOFOLLOW`), which also excludes other processes; a concurrent step returns `busy` and does nothing. This is stronger than
  per-input serialization and stops a stale scan overwriting a later one.
* **No database write transaction across a filesystem read.** The reader is called first (the inventory read has already closed),
  the page is validated, then the page's items (`ingest`) and its sync status (`record_scan`) are saved in one unit of work.
* **Status.** A step's `status`: `busy`, `idle` (no reserved event), `partial` (the pair has a continuation, or the step's page
  budget ended first), `ready` (a traversal of the pair reached EOF) or `unavailable`. A scan record is `partial` while a
  continuation remains and `ready` only after EOF. An empty `ready` page proves only that no call receipt was observed, never zero
  usage or cost, and never removes or invents a call.
* **Page validation before anything is written.** The page must have exactly the keys `status event_id items next_cursor error`,
  the requested event, a list of at most `page_size` items each of that event, a continuation that is `null` or a non-empty
  string (not repeating the current or any earlier cursor; never on an empty page), and a status/error pair that agree
  (`ready` with no error; `unavailable` with empty items, no cursor and a bounded `{code <= 64, message <= 300}`).
* **Failures record a bounded unavailable scan and advance** (earlier imports stay as they were; a refused page is never partly
  written): a library `unavailable` page keeps its own code (`corrupt_evidence`, `unreadable_evidence`); the collector's own
  codes are `invalid_page`, `read_failed` (the reader raised; its text is not echoed), `invalid_receipt` and `receipt_conflict`
  (the ledger refused the receipts: validation, identity, outcome or evidence conflict) and `storage_error` (the page could not be
  saved). One pair's failure never blocks the others.
* **Losing the write database is an explicit error**, not a scan: if the unavailable record itself cannot be saved, a driver that
  is not the system principal is used, or the inventory cannot be read, `collect_next` raises `CollectorStorageError` (and counts
  it in `diagnostics()["storage_failures"]`). It is never turned into `idle` or `ready`.
* `diagnostics()` returns primitive values: `page_size`, `max_pages`, `steps` (counts of `busy idle partial ready unavailable`),
  `traversals`, `storage_failures`, `state` (the last step's status or `null`), `current` (`input_id`, `context_event_id`,
  `pages_read`, `continues`, or `null`), `last_completed`, `last` (the last step), `last_error` (bounded code and message with the
  pair) and `last_step_at`.

### Lifecycle hook (the final owner)

The collector adds no scheduler. The one Context lifecycle owner (final integration of `ContextDriverLoop` with the graph-edit
driver) builds it once and drives it from its single loop:

```python
collector = ContextUsageCollector(database, pipeline, driver_id=SYSTEM_PRINCIPAL_ID,
                                  lock_path=paths.locks / "context-usage.lock")
# in the one loop thread, around each driver step and on idle ticks:
collector.collect_next()          # before and after process_next(), and when process_next() reports idle
```

Call it on the loop thread so a model turn already delays it (the library keeps incremental receipts during the turn, so a long
turn only delays the ledger refresh). The loop treats an exception from the collector like one from the driver (catch, record,
retry with its bounded backoff); `CollectorStorageError` means the database must be looked at. The database and Context stay open
until the loop reports `stopped`; the collector closes nothing. Reads never call `collect_next`.

## `context_filing_costs` (the one owner-only read)

Registered by `ContextUsageOperations` (`bazarlyk.domains.register_business_domains`); the old descriptor, its read branch and
`ContextBaseService.filing_costs` (and the Luna/Jev/per-upload columns) are removed. Name, Global namespace, effect `read`,
`supports_signoff=False`, exposure `{dashboard, worker}` and the root allowlist entry are unchanged. It is `owner_only`: the
retained Application refuses agents and every other principal ("requires the owning human"; a Global graph-read grant adds
nothing), and the read repeats the real installation-owner check itself.

Request (closed): `input_id?`, inclusive UTC `start_date?` / `end_date?` (end not before start), `limit` 1..100 (default 50),
opaque `cursor?` (bound to the same filters and limit by the ledger). An unknown `input_id` is `not_found`.

```
{format: "qorqut-context-usage-v1", currency: "USD", calls: [UsageCall...], next_cursor, aggregates: {UsageAggregates...},
 coverage: {basis, input_id, reserved_events, ready_events, partial_events, unavailable_events, unscanned_events,
            ready_events_without_calls, unattempted_inputs, last_checked_at}, usage_totals_final: false, cost_is_known_subtotal: true}
```

`calls` and `aggregates` are the ledger's (selected call-start dates; exact decimal-text money or `null`; configured model, effort
and tier apart from the observed provider, model, thread and turn; started/failed states; library attempt sequence; stage;
bootstrap reference; limitations; the pair's last scan). `coverage` is a **separate inventory** of every reserved Context event
(of the selected input, or of the installation), counted from queue attempts left-joined to scan records, **not restricted to the
dates**, and counting an event that has no imported call at all as `unscanned_events` until a scan exists.
`ready_events_without_calls` counts reserved pairs (each once, whatever their queue attempts) whose last scan is `ready` but for
which the ledger holds no call (an `EXISTS` check, so a pair with several calls never inflates it): `ready` still means EOF, and an
empty scan proves only that no receipt was observed, never zero execution, usage or cost, so such an event stays visible as missing
evidence. `unattempted_inputs`
counts inputs whose attempts never reserved a Context event (never started, or stopped before an event existed); nothing can be
collected for them yet and they are not in `reserved_events`. `last_checked_at` is the newest recorded scan, not a claim that
every event was checked then. The ledger's `aggregates.events` counts events represented by the filtered calls and is a
different number. Nothing in the answer proves final totals.

The read touches only the operational database: it never invokes the collector, opens a Context file or contacts a provider, and
it shows the last recorded collection. On a database that has no usage tables the read is `operation_unavailable`, never empty.

## Global `costs_overview`

The `filing` source is now the usage ledger's observed charges (no `context_base` dependency; `CostOperations(*, clock)`). Other
sources and the owner check are unchanged. For `filing`: `basis` is `observed` when any charge is known and `none` otherwise
(`unavailable` when the ledger cannot be read), `month_usd` / `total_usd` are `null` when that period has no observed charge (never
`0.0000`), plus `known_subtotal: true`, `unknown_calls` (calls with no recorded charge, never priced), `unfinished_calls` (a started
call keeps its observed charge and is counted unfinished) and the installation `coverage` inventory. Charges are summed exactly
per UTC start day from a streamed cursor, then shown to four decimals once. The overall `total` gains `known_subtotal: true` and
`incomplete_sources` (`["filing"]` when the source is unavailable; `month_usd` or `total_usd` is unestablished, that is the current
month or the whole period has no recorded charge; a call is unknown or unfinished; or coverage shows an event not read to EOF
(partial, unavailable, unscanned) or read to EOF with no call (`ready_events_without_calls`), even when other events carry known
charges). The known subtotal values themselves are unchanged by that marker. A failure leaves every other source readable and sets
`context_usage_error: {code: "context_usage_unavailable", message: "... (<ExceptionType>)"}` (otherwise `null`); the raw exception
text is never exposed. Only the two recognized completion-only definitions are no longer counted as background AI, so nothing
is counted twice: a job whose immutable `kind` and frozen `definition_ref_json` are `context.file` with integer version `2`, or
`context.instruct` with integer version `1`, and whose sole output is `context_filing_receipt` (`background/context_filing.py`). The match is NULL safe and independent of any name prefix or of the program currently installed:
a missing or different definition (an older version, another output set, another `context.*` program such as `context.audit`)
keeps its recorded background-call accounting, and no row is dropped because a JSON field is absent.

## Integration notes

* Host composition adds `context_usage.storage_contribution()` (owner `context-usage`; it requires the queue tables) to
  `host_contributions` with an Operations pin that carries the usage ledger. Build the collector with the Context `Pipeline`, the
  backend system principal ID and `paths.locks / "context-usage.lock"` and call `collect_next()` from the single Context loop
  (above). Tests compose the contribution through `tests/host_foundation_support.build_instance(..., extra=(...))`.
* **Dashboard:** read `context_filing_costs` as above and the Global `filing` fields: `basis` `observed|none|unavailable`,
  nullable amounts, `known_subtotal`, `unknown_calls`, `unfinished_calls`, `coverage`, `total.known_subtotal`,
  `total.incomplete_sources` and `context_usage_error`.

## Checks

`app/tests/test_host_context_usage_collector.py`, `test_host_context_usage_endpoint.py` and `test_host_costs_overview.py`
(support: `context_usage_support.py`) use a disposable instance, a real `qorqut_context` store and `Pipeline` with a scripted
injected runner (no model), the real queue transitions and the real usage tables. Run from `app/`:

```
QORQUT_TEST_WORKSPACE_ROOT=/path/to/workspace python -m pytest tests/test_host_context_usage_collector.py \
    tests/test_host_context_usage_endpoint.py tests/test_host_costs_overview.py -q -p no:cacheprovider
```

They need installed Operations and Context packages that carry the usage ledger and receipts, and the workspace's `qorqut-agentic/src/qorqut_agentic/resources/defaults` (the business template component was retired in v1.3). A missing
source is an actionable **failure** (`require_synthetic_sources()`), never a skip; this is local to these cases.
