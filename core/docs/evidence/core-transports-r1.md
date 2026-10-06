# Core transports r1 evidence

Date: 2026-10-07. Branch: `codex/core-transports-r1`. Clean starting base:
`ef18e59e2df372963f5fba222156fe6b6e50195f`. Manager contract revision:
`d1887c81585fa0496df1eb6945e20e961ef4e256`, read-only `CORE-API.md`.
Run: `649fed24-a7af-40d7-9d41-382ab248483a`; worker:
`b4c42006-a7de-40ff-a2c2-16519fe8e7b5`; bound executor conversation:
`01a11897-7320-72b0-b3ba-d9951479f2e7`. No delegation/internal reviewers.

This implements the bounded first transport increment for Manager review. It
does not complete M10 or G0–G6. Foundation implementation and plans are unchanged.

## Manager amendment

Message 61, `MANAGER_DECISION — core transport amendment 1`, required covering
MCP 1.30.0's direct root-logger validation calls as well as named SDK loggers.
The selected idempotent LogRecordFactory wrapper is installed at explicit
application assembly, never import. It matches SDK names and installed source
paths, clears payload-bearing message/arguments/exception/stack text before
handlers, and preserves levels, handlers, safe metadata and unrelated factory
behavior. Normal service startup/shutdown diagnostics remain available.

The existing real-wire check includes both `tools/call` with string arguments
and `notifications/cancelled` with invalid `requestId` and a synthetic secret
reason. Complete service stdout/stderr capture contains sanitized diagnostics
and no sentinel/token; protocol errors retain codes/statuses, the notification
returns 202, and normal operations and unrelated custom-factory logs still work.
The SDK and Pydantic pins were unchanged. Message 61 was acknowledged and its
inbox drained; no interim ACLA report was sent.

## Manager review revision 1

Message 70 required correcting local operator URL errors and strengthening the
existing response-limit evidence. Starting revision:
`d096f39f4a8f03da71623ef19b6668d8b3b8752f`. Only `transports/operator.py`, its
owning transport test file and this evidence changed in this revision.

The complete operation endpoint now passes HTTPX's URL parser before a client
opens. Existing origin/name/userinfo/path/query/fragment/port/control-character
restrictions remain. HTTPX `InvalidURL` and local value/Unicode refusals become
the existing generic `InputError`; arbitrary programmer errors are not caught.
The real module and installed console commands reject `http://\u2603.invalid`
with exit 2, empty stdout and the exact bounded local-input message, without
hostname/token/traceback output or an execution-uncertainty claim. A client-opening
trap proves rejection before network dispatch. Valid calls and conservative
post-dispatch errors remain covered by the same flow.

The production 32 MiB response guard is unchanged. Its existing fixture now sends
complete valid `OperationResult` envelopes of exactly 33,554,431 and 33,554,433
bytes without a declared length. The first succeeds with the exact result; the
second fails with bounded generic output. For an oversized declared length, an
explicit gate holds the body and connection open until the client has refused
the headers. The gate's 30-second failure guard exceeds the command's 15-second
guard, preventing early EOF from making a missing header check pass. Cleanup
releases the gate in `finally` and joins the owned server/handler threads; socket
writes are bounded. All cases retain one-request/no-retry assertions. These
replace the earlier malformed-body/immediate-EOF checks, whose refusal alone did
not establish the size limits.

Before the production fix, the two revised owning checks reproduced exit 1 for
the malformed origin; the strengthened size-limit check passed. After the fix:

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_transports.py -k test_cli_http_path -k test_redirect_disconnect -v
  Before fix: 2 checks, 1 expected regression failure (local URL exit 1).
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_transports.py -k test_cli_http_path -k OperatorBoundary -v
  After fix: 4 passed in 11.731 seconds.
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_transports.py -v
  15 passed in 23.631 seconds.
.venv/bin/python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir dist .
.venv/install-check/bin/python -m pip install --no-cache-dir --no-deps --force-reinstall dist/qorqut_core-0.1.0-py3-none-any.whl
  Rebuilt and installed successfully in the existing disposable task-local venv.
env -u PYTHONPATH .venv/install-check/bin/python -I -m unittest discover -s tests -v
  41 passed in 26.413 seconds (26 unchanged foundation, 15 transport).
.venv/bin/python -m pip check
.venv/install-check/bin/python -m pip check
  Both passed.
env -u PYTHONPATH .venv/install-check/bin/python -I -m qorqut_core --help
env -u PYTHONPATH .venv/install-check/bin/qorqut-core --help
  Both expose only serve/call; the installed suite also executes real serve/call.
git diff --check
  Passed.
```

The installed package/dependency/NOTICE probe passed again. No pins, foundation
code/tests/timeouts or test counts changed. Prior HTTP/MCP, privacy, logging,
replay and cancellation checks passed in both full transport runs. The earlier
foundation busy-timeout observation below remains a recorded limitation.
Task-local logs: `.venv/operator-revision-before.log`,
`.venv/operator-revision-tests.log`, `.venv/transport-revision-tests.log`, and
`.venv/installed-operator-revision-tests.log`.

## Delivered boundary

One explicit Starlette/Uvicorn lifespan opens the initialized installation using
`load_settings`/`Application.open`, validates every configured actor through
`config_get`, retains host ownership through admitted work, and closes it on
shutdown or cancelled startup. No initialization, migration, recovery activation,
actor registration, jobs, agents, browsers or UI assets are started by serving.
The command binds only `127.0.0.1` at the configured port, one worker, no reload,
proxy-header trust or WebSockets. Imports do not open storage/listeners/processes.

HTTP operation dispatch and schema discovery use the actual immutable registry:
`config_get`, `config_update`, `operations_get`, `events_list`. Application models,
results, validation, replay, revisions and cursors remain in `Application.invoke`.
There is no transport SQL or additional domain inventory. Health responses contain
only `live` or `ready`; readiness uses the configured registered identity and
does not represent workers, fleet, resources or capture.

Official MCP SDK Streamable HTTP runs at `/mcp`, stateless with JSON responses.
Each request carries its own bearer attribution, including initialization and
discovery. Tool arguments come from application models; results contain the exact
application envelope in both structured and text content. SDK protocol validation
is retained; SDK error codes/IDs/statuses survive diagnostic redaction. Its
input-echoing diagnostics are sanitized by the assembly logging boundary. The optional
GET stream returns 405; no stateful session, resources or browser tools are added.

Private credential readers reject duplicate bindings, unknown fields, short or
invalid bearer syntax, symlink components, nonregular files, other ownership and
group/other permission bits. They retain token digests, with private plaintext
token files for `call`. All input files/bodies are bounded to 1 MiB; foundation's
installation reader retains its stricter 16 KiB ceiling. Host/Origin, bearer,
identity-override, query and framing refusals precede dispatch and create no
operation receipt. No CORS or cookie authentication is enabled.

There are at most four admitted blocking invocations, without an executor queue;
32 ASGI requests and 64 Uvicorn connections/tasks bound network admission. Body
reads have a 10-second total deadline, sends a 30-second I/O timeout, and idle
connections a five-second timeout. Cancelled/disconnected waiters retain their
invocation slot until the thread finishes. Shutdown drains admitted work before
closing the application; raw asyncio cancellation is covered as well.

Both `qorqut-core` and `python -m qorqut_core` expose only `serve` and `call`.
The operator uses HTTP with explicit files, verified TLS, no environment proxies
or CA overrides, no redirects/retries, finite I/O timeouts and a 32 MiB response
ceiling. JSON preserves application results; text distinguishes accepted from
completed. Unknown delivery outcomes retain uncertainty and the original key.

## Checks and artifacts

All fixtures used explicit temporary synthetic installations, registered owner
and agent actors, private credentials, loopback sockets and positively owned
children. No native CLI stores, personal histories, real accounts/model calls,
live Qorqut services, shared processes, browsers, VMs or system installs were used.

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_transports.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_transports.py' -k test_strict_private_bounded_credentials_and_configuration -v
.venv/bin/python -m pip check
.venv/bin/python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir dist .
.venv/bin/python -m venv .venv/install-check
.venv/install-check/bin/python -m pip install --no-cache-dir -r requirements.lock
.venv/install-check/bin/python -m pip install --no-cache-dir --no-deps --force-reinstall dist/qorqut_core-0.1.0-py3-none-any.whl
env -u PYTHONPATH .venv/install-check/bin/python -I -m unittest discover -s tests -v
.venv/install-check/bin/python -m pip check
env -u PYTHONPATH .venv/install-check/bin/python -I -m qorqut_core --help
env -u PYTHONPATH .venv/install-check/bin/qorqut-core --help
git diff --check
```

Initial source full suite: **41 passed** (26 foundation, 15 transport). Initial
focused transport suite: **15 passed**; symlink/parent-traversal reader check:
**1 passed**. Initial installed-wheel suite: **41 passed**. The final revision's
checks are recorded above.
Both dependency checks passed. Installed tests exercise the generated console
script for actual serve/call, while source tests exercise the module command.
Import audit tests forbid writes, SQLite opens, processes, sockets and reads from
the upstream Qorqut runtime. A separate isolated probe verified the installed
package path, console script, dependency versions and packaged existing NOTICE.

One earlier installed run had one foundation concurrency assertion failure: one
of its 16 calls returned the documented `storage_blocked` result with that
fixture's 70 ms busy timeout. No foundation code/test or timeout was changed.
The exact test passed in isolation, then the complete installed suite passed
unchanged. This timing-sensitive failure remains recorded, not hidden:

```text
env -u PYTHONPATH .venv/install-check/bin/python -I -m unittest discover -s tests -p test_foundation.py -k test_parallel_identical_config_calls_commit_once_and_replay_history -v
```

The shared real HTTP/SDK flow proves schema/result parity, concurrent owner/agent
isolation, changed credentials on one initialized SDK client, cross-transport
normalized replay/conflict, historical results after later revisions, operation
lookup and durable event continuation/recovery rejection. Real HTTP and MCP
disconnects before commit each replay one resulting receipt via the official
client. Focused checks cover authentication/Host/Origin/body boundaries, protocol
privacy, passive imports, startup/shutdown locks, cancelled startup/drain,
readiness failure, client exit codes, TLS rejection, redirect/drop no-retry and
declared/streamed 32 MiB response ceilings.

Observed: Python **3.12.3**, SQLite **3.45.1**, Pydantic **2.12.3**, MCP **1.30.0**,
Starlette **1.7.0**, Uvicorn **0.54.0**, HTTPX **0.28.1**, AnyIO **4.15.1**. All
31 runtime/build distributions match `requirements.lock`; installations stayed
inside the disposable task-local virtualenvs.

Revision 1 wheel SHA-256:
`c10bb5ad99698a7a2cfc1e0c56535b1be30398cb25d7f903ce19d47a1b5ebc2b`.
Preserved packaged `storage/NOTICE` SHA-256:
`c9a80f7b863ca7ca6a1d640a5ad0cb72ca991eec41d862741038ef40a859ef0d`.

## Provenance and unaccepted integration

P14 informed this fresh official-SDK composition. Read-only upstream core
revision `e71fd1e527368bfd41b9815126f4bf33452aeebb` files inspected for request
binding, lifespan and failure behavior:

```text
bazarlyk/transport/mcp.py  ed8be79a7f7dbd9e359bfa6c042ea7bc212857ecefa45a0ab6656daf50ac90be
bazarlyk/service/web.py    f44d5ab0d5981dac15198ecd81f7de193224d94b4ad9d347e87edf646080e0fb
```

No substantial source block or policy/business stack was copied. P16's UI/viewer
code and a stdio bridge were not needed. No Qorqut runtime/configuration import
or other builder's implementation was used.

This is local HTTP/official-SDK evidence, not real Claude/Codex MCP discovery.
Native clients, browser handoff, asynchronous workers, agents/runtime, sessions,
streams, UI/VM integration and G0–G6 remain unaccepted. Network limits are bounds,
not capacity measurements; an OS/storage stall can still delay thread drainage.
No PR, merge, deployment or release is included. Final commit and dedicated-branch
push verification are reported through ACLA.
