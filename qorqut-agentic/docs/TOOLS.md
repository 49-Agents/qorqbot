# Custom tool execution

The module runs a custom tool only through an explicitly registered runtime.
Importing a package, loading its catalog, or importing this library does not run
custom code. Runtime aliases are trusted caller configuration; authored files
cannot choose shell commands or Python imports inside the host process.

```python
import sys
from qorqut_agentic import AgenticLayer
from qorqut_agentic.runtime import LocalProcessRuntime

runtime = LocalProcessRuntime(
    {"python": sys.executable},
    env={"LANG": "C.UTF-8"},
    max_timeout_seconds=20,
    max_output_bytes=65536,
    max_stderr_bytes=8192,
)
layer = AgenticLayer.open(root, state, runtimes={"python": runtime})
actor = {"actor_id": "example-owner", "actor_kind": "owner"}
result = layer.tools.invoke(
    "example-project-workflow/normalize-results",
    {"rows": [{"name": " Alpha ", "score": 1.234}]},
    "my-explicit-invocation-id", actor,
    selection={"package_ids": ["example-project-workflow"]},
)
receipt = result["receipt"]
```

`root` and `state` above are explicit caller-owned paths to an already initialized
layer. State, receipts and scratch files belong outside authored Git content.

## Python interface

```python
invoke(ref, arguments, invocation_id, actor, selection=None,
       expected_definition_revision=None, expected_code_revision=None)
receipt(invocation_id, *, actor=None, selection=None)
reconcile(invocation_id, resolution, note, actor, *, selection=None)
test(ref, actor, cases=None, selection=None)
```

`invoke`, `receipt` and `reconcile` return `{receipt: ...}`. `actor` contains
`actor_id` and `actor_kind`. The host derives both actor and allowed package
selection from its own authority. Caller-provided identity is not authenticated
by this module. Omitted receipt actor and selection are trusted standalone use;
they must not be exposed as an unauthenticated host endpoint.

A supplied receipt actor must match the recorded owner. The same requirement
always applies to invocation retries and reconciliation. A supplied package
selection must include the receipt's recorded package, otherwise `unavailable`
is returned. Receipt reads and retries do not require the tool to remain in the
catalog: deleting or updating it does not erase evidence. Scope labels such as
project/work type are routing metadata, not permission grants.

Inputs are checked against the tool's JSON Schema before dispatch. The code and
declared resources are copied into a unique directory per execution candidate;
the code revision hashes the exact staged bytes. A durable `in_flight` receipt
is inserted before calling the runtime. Racing requests with the same invocation
ID cannot replace one another's staged code or dispatch twice.

The fingerprint includes the tool reference, arguments, normalized actor and
both expected revision pins. A matching retry returns the known receipt. Changed
request content or pins produce `operation_conflict`. A new execution with a
changed pinned definition or resource digest produces `revision_conflict`
before dispatch. Missing runtime produces `runtime_unavailable` without a receipt.
An unfinished authoring journal similarly rejects new invocations and explicit
tool-test catalog reads with `recovery_required`, before a receipt or runtime
call. Matching receipt replay, reads and reconciliation remain available during
recovery. Complete explicit authoring recovery before starting new execution.

## Receipt and reconciliation

A receipt includes `invocation_id`, `tool_ref`, `purpose`, `actor`,
`definition_revision`, `code_revision`, `started_at`, `ended_at`, `status`,
`failure_code`, `effect_state`, `exit_code`, `output`, `diagnostics`, `limits`,
`reconciliation` and `replayed`.

| Status | Meaning |
| --- | --- |
| `in_flight` | Dispatch reserved; its process may still be executing. |
| `succeeded` | One JSON value was returned and passed the output schema. |
| `failed` | Execution or output validation failed; an external effect may still have happened. |
| `indeterminate` | The recorded local runner is known to have exited without a final receipt. |
| `reconciled` | The caller supplied an explicit conclusion about the effect. |

Failure codes include `spawn_failed`, `runtime_error`, `nonzero_exit`, `timeout`,
`output_overflow`, `invalid_output`, and `output_schema_violation`. Empty output,
multiple JSON values, invalid UTF-8, and nonfinite JSON numbers are invalid.
Decoder, schema-validation and serialization failures (including excessive
nesting or text that cannot be stored as UTF-8) finalize a failed `invalid_output`
receipt with bounded diagnostics. A process-crash `BaseException` is not converted
into an ordinary output error; its receipt remains conservatively in flight.
Diagnostics contain bounded stderr, a bounded stdout excerpt on failure,
validation issues, and a message. Successful output is the validated JSON value.

`reconcile` accepts `effect_confirmed` or `no_effect_confirmed` and a nonempty
note explaining the evidence. It can resolve an indeterminate or stuck in-flight
receipt, but cannot create a successful tool output. Reconciliation never
reruns code. A new explicit invocation ID is necessary for another attempt.
Exactly-once effects are not promised. On systems where process liveness cannot
be established, in-flight state remains conservative until explicit reconciliation.
A process crash can leave isolated scratch artifacts in the explicit state path;
normal returns clean up their own staging directories.

## Process boundary and limits

`LocalProcessRuntime` is an opt-in process adapter, **not an OS security sandbox**.
The code can access resources available to the OS user, including the network.
Qorqut must provide its authorized native execution adapter; it must not fall
back to this subprocess adapter inside a privileged server.

The local adapter uses `[absolute_interpreter, staged_code_path]`, without a
shell. It creates a process group, uses an empty working directory, and passes
only the configured environment plus `QORQUT_AGENTIC_PACKAGE_DIR` and
`QORQUT_AGENTIC_SCRATCH_DIR`. It does not inherit service environment variables.
Both output streams are bounded while reading. Timeout or overflow of either
stream terminates the process group. Host maxima cap authored limits. Defaults
are 60 seconds, 1 MiB stdout and 64 KiB stderr for the adapter; tool definitions
may request lower limits. Zero stderr bytes means any stderr output overflows.

A custom runtime implements `execute(ExecutionRequest) -> ExecutionResult`.
Requests carry invocation ID, runtime alias, staged paths, JSON stdin bytes, and
limits. Adapters must honor host limits and report the actual limits. The module
also rejects over-limit returned streams and invalid runtime result shapes.

## Explicit fixture tests

`tools.test` runs the definition's `tests`, or an explicit `cases` list. Every
case has `name`, `input`, and `expect`; expectations name `status` (`succeeded`,
`failed`, or `rejected`) and optionally `output`, `failure_code`, or `error_code`.
The whole fixture structure is validated before executing any case. Results
include per-case comparisons and invocation IDs with `purpose: test`.
Tests are never run automatically by catalog reads or package import.
