# Tool-call latency investigation and changes — 2026-11-10

Tool latency comes from CPU contention, repeated configuration/instruction work,
long database transactions, and background polling. The underlying domain
operation is frequently a small part of the wait. This branch addresses the
measured code bottlenecks; production improvement requires a separately approved
release and measurement under comparable load.

## Production evidence

Read-only observations of the running service and host:

- Eight CPU cores were saturated, with load around 44, 50–90 runnable processes,
  and roughly 90% CPU pressure. There were 278 Chrome processes. IO pressure was
  low; full swap alone did not establish active swapping as the cause.
- In a 30-minute window, 303 logged slow MCP calls had median/p95 total latency
  of 1.86/8.80 seconds, maximum 25.8 seconds. Configuration median/p95 was
  1.19/4.69 seconds; execution was 0.32/3.86 seconds. These are **slow-call-only**
  distributions, not percentiles over all requests. One `tasks_get` spent
  6.55 seconds configuring and 0.21 seconds executing.
- Storage logs recorded busy failures and multi-second held units. A held-unit
  warning includes reads, writes and finalization; it is not itself evidence
  that the SQLite writer lock was held for that whole duration.
- Active-thread and GIL-held profiles repeatedly reached instruction inventory,
  configuration resolution, transcript archiving and Paper selection. Thread
  samples include waits, and inclusive stack categories overlap.
- A read-only recovery lookup took about 1.02 seconds to return no pending work
  from 4,425 receipts. Its plan searched the nullable unique `signoff_id` index
  and inspected historical rows. File recovery runs every **five seconds**;
  the approval worker normally polls every **0.5 seconds**.
- Browser health polling created a default urllib opener, including an unused
  HTTPS context and certificate-store loading, for a loopback HTTP request.

Raw profiles and operational records are not included in this repository.

## Implemented optimizations

| Area | Change and retained contract |
| --- | --- |
| Instruction inventory | Read files through already-open confined parent descriptors. Every file is still opened with no symlink following and freshly hashed; size/path/type checks remain. |
| Instruction projection | Share one verified outer snapshot across nested projections; reuse an exactly matching parsed catalog; cache runtime prose by its content revision and return private documents. Entry/exit inventories, recovery checks and bounded conflict retries remain. |
| Configuration | Return the recursively frozen cached configuration for ordinary invocations. Diagnostic preparation and session-delivery projections still run when requested; current profile, scope and source checks remain. |
| Transaction duration | Load checkpoint prose once before database units. Recheck session ownership and current source state inside the claim transaction. Render task/attention guidance after commit, outside the writer; skip unchanged clock updates. |
| MCP responsiveness | Offload validation-guidance reads from the event loop. Advisory failures preserve committed results or the original validation error. |
| Durable queues | Add partial pending-receipt and approved-Signoff indexes, plus `resource_claims(apply_id)`. Preserve recovery eligibility, ordering and limits. An explicit offline migration advances the schema identity atomically. |
| Browser polling | Use a bounded direct loopback HTTP probe, with no proxy, redirects or TLS setup. Keep the existing timeout and debugger-URL health criterion. |

Attention/Todo guidance is now optional top-level MCP `agent_hint`, consistent
with task-update guidance. It is no longer embedded in newly persisted mutation
receipts. Domain receipt data continues to identify the saved membership/focus
and revision; advisory failure cannot turn that successful mutation into an error.

## Evaluation

The same benchmark script ran on base `b8ff8b7` and the candidate, in the order
base/candidate/candidate/base, with 11 measured rounds per case per run after
warmup. The instruction fixture contains 228 bundled files; configuration uses
400 synthetic tools. Reported CPU figures are medians of the two run medians.
Every case produced matching output hashes across revisions.

| Isolated operation | Before CPU | After CPU | Reduction |
| --- | ---: | ---: | ---: |
| Fresh instruction inventory | 19.337 ms | 13.829 ms | 28.5% |
| Verified catalog snapshot | 49.489 ms | 35.362 ms | 28.5% |
| Runtime instruction rendering | 241.504 ms | 33.573 ms | 86.1% |
| Cached configuration value access/copy | 8.945 ms | 0.003 ms | >99% |
| Local browser health probe | 46.532 ms | 1.026 ms | 97.8% |

Runtime-instruction wall medians were 280–300 ms before and 40–41 ms after.
Its warm-call work fell from three full tree inventories and one catalog parse
to two inventories and no parse. Cached configuration measures the copying
portion only, **not** complete configuration resolution or tool latency.
Browser measurements include a synthetic local HTTP server in the same process.

On a separate disposable SQLite database with 5,000 completed receipts and
16 KiB synthetic plans, the empty recovery query fell from 51.934 ms to
0.032 ms median wall time (46.564 to 0.030 ms CPU). SQLite VM steps fell from
40,018 to 49. Tests verify that SQLite actually chooses the new indexes without
requiring `ANALYZE`; merely adding an index with different leading columns did
not achieve this. The pending candidates still receive the original global sort.

[Machine-readable results](toolcall-eval-2026-11-10.json) contain only synthetic
inputs and aggregate timings. Host scheduling varies, so these measurements
establish reduced work, not a promised production latency percentile.

148 distinct focused tests passed across the instruction, configuration, MCP,
attention, checkpoints, browser and migration checks. Coverage includes
same-size/same-mtime edits, concurrent snapshots, symlink swaps, recovery failures,
nested snapshot conflicts, immutable cache isolation, an event-loop progress
barrier, claim deduplication, session supersession, query-selection equivalence,
extension/data preservation, partial-DDL rollback and exclusive migration ownership.

From the repository root, using a prepared Python environment with the app's
dependencies:

```sh
PYTHON_BIN=/absolute/path/to/prepared/venv/bin/python
"$PYTHON_BIN" qorqut-code/app/scripts/measure_toolcall_latency.py --rounds 11
"$PYTHON_BIN" qorqut-code/app/scripts/measure_recovery_queue.py --rounds 11

DISPLAY=:99 PYTHONPATH=qorqut-code/app:qorqut-code/app/tests:qorqut-code/qorqut-operations:qorqut-code/qorqut-context:qorqut-agentic/src \
  "$PYTHON_BIN" -m unittest \
  test_instruction_read_performance test_mcp_catalog_cache test_mcp_resolution_efficiency \
  test_mcp_context_fastpath test_mcp_bootstrap_query test_mcp_discovery_cache \
  test_attention_simplification test_checkpoint_transaction_cost \
  test_mcp_advisory_performance test_mcp_phase_timing test_browser_health_probe \
  test_browser_setup test_browser_sign_in test_browser_cleanup_defaults test_recovery_queue_indexes
```

The existing sign-in tests mock process launch but require a nonempty `DISPLAY`;
the sentinel above does not start an X server or browser. No live agents, model
providers, messages or operational records are exercised by these checks.

## Release requirement

The three indexes change the composed schema fingerprint. Include
`qorqut.migration.queue_indexes` in the approved installed release-worker migration
plan with the exact extension selection. Its `inspect` command is read-only;
`apply` requires exclusive instance ownership and refuses a running owner, an
unknown schema or a different extension selection. Normal startup never migrates.

Using the target release environment, the command interface is:

```sh
python -m qorqut.migration.queue_indexes inspect --instance-root /path/to/instance --extensions-config /path/to/selected-extensions.json
python -m qorqut.migration.queue_indexes apply --instance-root /path/to/instance --extensions-config /path/to/selected-extensions.json
```

Omit `--extensions-config` only for an instance with no selected extensions.
The recognized predecessor is the base composition plus those extensions;
earlier schema transitions must already be complete. A downgrade also requires
schema reconciliation because the old application rejects the new fingerprint.
No production migration, deployment, restart or process cleanup was performed.

## Next areas, in priority order

1. **Host resource budget.** Identify which browser fleets, native agents and
   development services must run concurrently; measure per-service CPU and cap or
   isolate background work from interactive requests. Reclaiming CPU headroom is
   likely necessary to turn these code savings into consistently low latency.
2. **Remaining configuration work.** Profile the full `_snapshot` path after this
   release. Precompute immutable registry metadata and pinned artifact manifests;
   retain per-request authority and source-change checks. A future immutable
   instruction-generation protocol could remove whole-tree inventories, but
   needs explicit invalidation, recovery and filesystem-change guarantees first.
3. **Background fan-out and transcript history.** Paper selection repeatedly
   traverses agents/projects; batch shared selection work per pass. Evaluate
   incremental archive verification with correct append, replacement and
   truncation handling. These paths were observed but not changed here.
4. **Production comparison.** Under comparable concurrency, compare per-operation
   configuration/execution/total latency, busy errors, writer duration, CPU
   pressure and event-loop delay. Separate worker-pool waiting and advisory time
   from domain execution in future timing instrumentation. Recheck both cold
   instruction loads and warm calls; the benchmark gains above concern warm calls.
