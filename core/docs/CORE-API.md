# Core HTTP/MCP contract: first increment

Manager contract, 2026-10-07. The reviewed application baseline is
`ef18e59e2df372963f5fba222156fe6b6e50195f`. This document specifies the next
increment; it is not evidence that the server or native MCP clients have passed.
The final domain inventory remains in CONTRACTS and M10.

## Composition and configuration

One host process opens the explicitly initialized installation, holds its
existing lifetime lock, and serves HTTP plus official SDK Streamable HTTP MCP.
Use one ASGI worker without reload. Run blocking application calls outside the
async event loop, with bounded concurrency. Importing transport/client modules
does not open storage, start processes or bind sockets. Core needs no UI assets.

For this local increment, bind only `127.0.0.1` at the explicit installation
`listen_port`; reject another configured listen address. Wider private/TLS
deployment and browser sessions follow their own integration work. Do not
silently initialize or migrate storage, activate recovery, register actors,
start jobs, or start agents/browsers while serving.

The command entry points are `qorqut-core serve` and `qorqut-core call`, with an
equivalent `python -m qorqut_core` entry point. Serve requires `--installation`
and `--transport-config` file paths. Installation settings use the accepted
`load_settings`/`Application.open` interfaces. Transport configuration contains
only an absolute `credentials_file` and UUID `readiness_actor_id`.

The private credentials JSON contains `actors`, each with a distinct `token`
of at least 32 characters and UUID `actor_id`. Reject unknown fields, duplicate
tokens/actor IDs, symlinks, nonregular files and nonprivate credential-file
permissions. Bound configuration reads to 1 MiB. No credentials are issued or
rotated by this increment; callers explicitly provision registered identities.
The readiness actor must be one of those configured actors. Validate bindings
through the existing application `config_get`, without transport SQL queries.

## Attribution

Authenticate every API and MCP request, including initialization and discovery,
from an Authorization bearer token. Derive `Invocation.actor_id` from the fixed
credential binding; never take it from operation arguments, URLs or a caller's
`X-Qorqut-Actor` override. Reject that override. All registered actors have the
same operation inventory and access under `allow_all`.

Use stateless MCP HTTP so one session cannot retain another caller's identity.
Reject unexpected Host/Origin values; this increment accepts the configured
loopback origin and its localhost equivalent only. Nonbrowser clients may omit
Origin. Enable no permissive CORS or cookie authentication. Preserve SDK
transport checks. Missing/invalid credentials return HTTP 401 with a bearer
challenge and a bounded generic authentication message, before dispatch.
Transport-level Host/Origin and framing errors also occur before application
dispatch; they do not fabricate an operation receipt.

Keep tokens, Authorization headers and submitted content out of logs, exception
messages, representations and schemas. Native-client discovery, owner cookie
sessions, WebSocket origins and credential issuance remain later acceptance.

This includes the SDK's malformed-request and malformed-notification validators,
which can log raw input through the root logger. Sanitize SDK-origin records
before handlers emit them, without muting unrelated diagnostics or adding import
side effects. Actual HTTP/SDK checks cover both paths as well as client responses.

## Routes and results

| Route | Behavior |
| --- | --- |
| `POST /api/v1/operations/<name>` | Parse a JSON object and call `Application.invoke(name, authenticated_context, arguments)` once; return its complete `OperationResult`. |
| `GET /api/v1/schema` | API version 1, the shared result schema, and each actual registered operation's name, mutation flag, input schema and output-data schema. Derive models from the immutable application registry. |
| `/mcp` | Official SDK Streamable HTTP; expose the same actual operation names and model-derived argument schemas. |
| `GET /health/live` | Unauthenticated, minimal process liveness only; no paths, identities or secrets. |
| `GET /health/ready` | Unauthenticated, minimal readiness for the currently registered synchronous operations. Use `config_get` under the configured registered readiness identity; failure returns 503. Do not certify workers, resources or fleet/capture health. |

The only implemented operations at this base are `config_get`, `config_update`,
`operations_get` and `events_list`. Do not register future agent/message/file,
browser, recovery, viewer or job-dispatch tools. `events_list` is bounded durable
pagination; `/api/v1/events` SSE and all WS/artifact routes remain absent.
Registry discovery must not be a dynamic plugin/registration API.

Successful application results use HTTP 200, including a future accepted result.
Application errors retain their existing typed result envelope and map as follows:
invalid argument 400; not found 404; revision/idempotency/transition/target/uncertain
conflicts 409; unsupported capability 422; not ready/storage blocked/unavailable/
unsupported schema 503. Unsupported API versions must fail clearly without
falling through to another route/version. Unexpected failures return bounded
generic errors, never tracebacks or echoed input.

MCP calls return the same result as structured content, with compatible text
content; `isError` reflects application failure. Application argument validation
uses the same Pydantic models and produces the same domain error semantics;
malformed MCP protocol messages retain SDK protocol errors. Do not implement
domain validation, revision checks, SQL access or mutation replay in transports.

Limit incoming bodies to 1 MiB including chunked requests, before buffering the
whole request. Current model/page limits bound successful results; do not truncate
them. Retain finite I/O timeouts and a 32 MiB operator response-read ceiling.
Request cancellation/disconnection cannot turn possibly committed work into a
safe retry or cancel a running thread and claim rollback. Original idempotency
keys and historical results remain authoritative.

## Operator client and acceptance

`qorqut-core call NAME` accepts an explicit HTTP/HTTPS base `--url`, a private
`--token-file`, optional `--arguments-file` containing a JSON object, and `--json`.
Missing arguments mean an empty object. It calls HTTP only, never local SQLite.
Do not inherit proxies, follow redirects, disable HTTPS certificate verification,
retry mutations automatically or put credentials in URLs/argv. JSON output
preserves the result; concise text distinguishes accepted work from completion.
Exit 0 for success, 1 for application/transport failure and 2 for local argument
errors. No follow/poll loop, init/migrate or unimplemented command is advertised.

Use a real isolated server and official MCP client, synthetic initialized data,
registered owner/agent identities and private test credentials. One shared flow
must prove HTTP/MCP schema/result parity, concurrent identity isolation,
cross-transport identical-key replay/conflict, revision behavior, event cursor
continuation/recovery rejection and operation lookup. Add focused transport
checks for authentication/Host/Origin rejection, body bounds, disconnect/retry,
unknown names/versions, headless startup/shutdown and the operator HTTP path.
Reuse foundation fixtures where appropriate; do not duplicate its crash matrix.
Build/install the wheel with pinned tested SDK/server/client dependencies and
check installed entry points/import isolation. No examples or UI are required.

These checks establish actual HTTP/SDK behavior. Real Claude/Codex discovery,
browser handoff, asynchronous workers, agent/runtime integration, sessions,
streams and G0–G6 remain unaccepted by this increment.
