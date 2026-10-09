# D55 local access — Core implementation

Implemented the frozen [local access contract](../LOCAL-ACCESS.md) on
`codex/build-local-access-core-r1`, based on `8c940bfdd0767e2f6b9ea77dc2f8947c40aab656`.

Explicit server startup resolves the sole registered owner through
`Application.local_owner()`. Zero or multiple owners stop startup with a fixed
configuration diagnostic and release the installation lock. HTTP, MCP and Terminal
default to that owner; the optional single `X-Qorqut-Actor` UUID supplies public
attribution. Invalid explicit values never fall back to the owner. Application
actor and target checks remain in place.

`GET /api/v1/session` returns only `actor_id` and `api_version: 1`, without a
cookie. POST/DELETE session routes, bearer validation, browser session storage,
expiry, revocation and credential configuration are removed. Obsolete
Authorization/Cookie headers are ignored. Loopback binding, Host/Origin checks,
request bounds, operation receipts and Terminal control/sequence/teardown remain.

`create_app(settings, ui_root=None)` and `serve(settings, ui_root=None)` accept no
credentials. The server CLI requires only `--installation`, with optional
`--ui-root`. Operator calls accept optional `--actor-id`, with callable signature
`call(url, name, arguments, actor_id=None)` and the existing bounds/no-retry behavior.

Native setup generates agent actor headers for MCP and hooks without generating
or registering tokens. Managed replacement updates prior token bindings,
including the exact generated Codex TOML block, while preserving surrounding
native preferences and trust entries. Legacy credential files remain untouched
and unused. Authored/conflicting managed edits still refuse replacement.

## Focused verification

Used Python 3.12 from a completed test venv with this worktree's `src:tests` on
`PYTHONPATH`; all seven runtime dependencies matched `pyproject.toml` pins.
All fixtures used temporary installations, isolated ports and owned disposable
Bash/tmux/processes. Native program fixtures were synthetic, with no provider call.

48 distinct existing checks passed across grouped runs and targeted corrections:

| Owning checks (`python -m unittest -v ...`) | Count | Coverage |
| --- | ---: | --- |
| `transports.test_console transports.test_transports` | 24 | Direct identity/HTTP/MCP, actor attribution and refusal, ignored legacy headers, startup owner cardinality, CLI, bounds, no retry, lifecycle |
| `transports.test_terminal runtime.test_native_setup runtime.test_native_hooks` | 17 | Actual cookie-free WebSocket control/input/resize/takeover, recording/restart/recovery, managed token-to-actor replacement, hook attribution and teardown |
| `runtime.test_monitor.MonitorLifetime runtime.test_transports messaging.test_transports transports.test_chat_publication` | 7 | Dependent composition, messaging/file references, dated Chat publication and interrupted-publication recovery |

Initial failures and reruns are retained in the private workstream evidence.
Corrections covered removed-route status expectations, explicit cancellation,
an inadvertently removed test import, and stale helpers: D50 grouped archive
ranges, the existing 17-operation inventory, advancing observation timestamps,
and D53's Chat-only `inbox_reference` field. Archive/history/runtime production
semantics were not changed. `git diff --check` and changed Python syntax checks
passed. Schema 9, 17 public operations and five job handlers remain unchanged.

No UI edits, live-instance changes, service changes, provider/browser calls, merge
or deployment were performed. Paired UI integration and the owner test-instance
browser smoke remain Manager work. Generated-file migration was exercised;
reloading configuration inside an already-running native CLI was not exercised.
