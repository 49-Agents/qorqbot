# MCP batching and model round trips

## What agents already do

A domain operation, an MCP HTTP request, an agent tool-call output, and a model
generation are different units. Ten separate MCP requests do not prove ten model
round trips. Clients can submit multiple native tool calls in one assistant
response, or execute several MCP calls inside one model-authored program.

Clients that support multiple native tool calls per assistant response, or program-style
execution, can already issue several independent MCP calls without a model decision between
each. Sequential awaits inside one program do not require a model decision between inner
operations, and long executions may still yield and require wait calls. Message and outer-call
identifiers establish explicit groupings, not total provider request counts, cached-token
charges or semantic task boundaries; `scripts/audit_mcp_context.py` reports this grouping
separately from result sizes. See
[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)
for the supported multiple-function-calls-per-response contract; it does not establish how
every installed client schedules or charges requests.

## Implementation scope

`operations_batch` is a transport-only MCP tool dispatching 1–20 existing
operations in order, with one labeled response envelope. Every inner call uses
the same application invocation path as an individual call. No second writer,
new data schema, model runner, generic scripting language or background job is
introduced. Conditional reads and briefing redesign remain separate work.

Each entry has `id`, `operation`, and `arguments` (the ordinary MCP arguments,
including its own idempotency key and scope selector when needed). IDs are unique
within the request. The batch envelope has no idempotency key: retries reuse the
same per-write keys. Already-completed writes replay their stored results. Reads
run again. Do not reorder/rekey writes or claim the batch is an atomic transaction.

Default `on_non_ok: stop` stops after an error, required approval, or accepted
asynchronous operation. `on_non_ok: continue` is explicitly for independently
justified entries: an approval submission is not an applied action, and later
messages must not claim that it was applied. Each attempted result retains its
status, errors, signoff/job references, warnings and compact/full presentation.
Unattempted entries are marked `not_executed`. Successful earlier operations are
not rolled back. Authentication/outer batch permission loss always stops execution.

The result's `data.results` preserves input order, with each entry's `id`,
`operation`, and either `state: returned` plus its ordinary `result`, or
`state: not_executed` plus a `reason`. Summary fields are `attempted`, `total`,
`all_ok`, `stopped_after`, and `stop_reason`. The outer status is `error` with
`batch_incomplete` if any child errors or is skipped; successful child receipts
remain in `data`. If all entries returned but some need approval or async work,
the outer status is `ok` with a warning and `all_ok: false`. Always inspect child
statuses: outer `ok` alone does not mean all requested effects have been applied.

For example, known independent work can be submitted as:

```json
{
  "operations": [
    {"id": "task_a", "operation": "tasks_create", "arguments": {"title": "First task", "idempotency_key": "story-work-task-a"}},
    {"id": "task_b", "operation": "tasks_create", "arguments": {"title": "Second task", "idempotency_key": "story-work-task-b"}},
    {"id": "config", "operation": "agent_config_get", "arguments": {}}
  ]
}
```

Use genuinely unique, stable keys for the intended writes; example labels are
not shared production keys. A disconnected/timed-out caller must not assume
rollback or retry with fresh keys. Replay the same writes to recover receipts.

Envelope validation precedes execution. Inner validation, authorization and domain
failures remain per-entry results. Reject nested batches. Use a bounded start
budget so a long batch does not keep starting additional calls indefinitely;
never interrupt an already-running mutation merely to hit that budget.

All current agents can discover batching by core default, with an explicit owner
override to deny it. Batching grants no inner operation, plugin, business access
or owner authority. It cannot itself be approved as a blanket Signoff. Every
entry retains its own approval gate. The tool is not a dashboard/worker mutation
and cannot bypass the MCP dispatcher through direct application invocation.

For ten tasks in an existing Story, use ten `tasks_create` entries with the same
known `story_ids`, rather than ten creates followed by ten separate links. Ordered
execution avoids preparing concurrent changes against the same Story revision.
For a new Story whose ID is not yet known, first create/read that Story, then batch
its tasks. Result references, branching and concurrency are not needed in v1.

## Where savings do and do not come from

- If the agent was issuing ten separate model/tool cycles, batching can replace
  those with one submission/result cycle (excluding waits/approval/recovery).
- If a client already groups the ten calls in one assistant output, batching
  primarily saves MCP HTTP envelopes and execution-orchestration code, not ten
  model generations. Sequential server execution can be slower than independent
  parallel reads; native grouping remains appropriate for those reads.
- A successful batch still returns useful per-entry outcomes. It does not make
  ten requested records cost the same as one record. Compact results remain useful.
- Guidance should encourage grouping fully specified work, not guessing arguments
  before reading necessary evidence or reporting success without checking results.

## Verification gates

Test ten Story-linked task creations in one MCP call; mixed tasks, owner-input
Signoff and agent message; compact/full replay without duplicate effects; partial
failures and stop/continue semantics; nested/oversized/duplicate-ID rejection;
wrapper denial, inner denial, cross-business and owner-only refusal; per-entry
authentication/policy refresh; and truthful budget-excluded results. Exercise the
installed MCP client against a temporary service. No live agent mutations or
provider/model calls are part of the implementation tests.

Deployment must refresh prepared tool configurations and reconnect clients for
the new schema. This branch is not merged or deployed without explicit approval.

### Verified on the implementation branch

The 82-test regression run passed (batches, installed SDK over temporary HTTP,
compact presentation, grouping audit, application/contracts and policy). Follow-up
checks passed for global-agent routing, worker-only refusal, slow authorization,
root/manager configuration, alias grants and transport-only descriptor invariants:
90 distinct tests passed across these runs. The only warning was the existing
Starlette/httpx test-client deprecation. Tests used isolated databases and prepared
workspaces with release-pinned role/template assets; no live service or agent data
was changed. The shared primary checkout's existing edits were preserved.
