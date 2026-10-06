# Headless jobs and local browser sessions

Manager design, 2026-10-08, D35. Completes the hosting needed by the initial chat
UI without creating another backend. Scope is `transports/`, one small worker
module beside the job store, necessary job query helpers, focused tests and
evidence. Do not edit host migrations, application assembly, agent models or
the chat domain; those have a separate owner.

## Background work

`serve` explicitly starts one sequential job worker during ASGI lifespan,
after storage/credentials validate. Imports and `Application` construction
remain passive. Core serves with or without UI assets. Change D26's initial
no-job-start behavior explicitly: a running server now processes committed
typed jobs, including real workspace provisioning. A missing VM/runtime still
leaves its existing dependency hold; a file-ready directory is not a ready VM.

Use one dedicated thread and stop event, not a service or arbitrary executor.
Process `Jobs.run_next` serially using a stable worker UUID for that process;
when idle wait at most one second on the stop event. Bound repeated-error waits
and log only fixed codes/identifiers, never message/credential content. Keep
the worker alive after a recoverable job error. Do not swallow a lost lifetime
lock or a dead worker and continue advertising background readiness.

Expire old claims normally. Before queued work, reconcile a bounded batch of
eligible uncertain `file_install` and `conversation_publish` jobs from this
recovery epoch, through their existing handler inspector. Exclude
`recovery_review`, stale epochs and every other external-effect kind. Retry
eligible original-epoch receipt inspections on a finite delay, not a tight
loop. Missing handlers are not permission to execute another job kind. Expose
a narrow job-store query for this if needed; no transport-specific SQL/domain
logic. `message_delivery` dependencies remain waiting; no terminal replay.

Shutdown closes request admission, drains admitted operations, stops/joins the
worker after its in-flight durable effect settles, then closes the application
lock. HTTP cancellation cannot claim a running effect was rolled back. Test
startup failure and cancellation as well as normal shutdown. Health readiness
now checks the worker's functioning in addition to the existing config check;
this still does not certify every agent or native capture.

## Browser session routes

Keep existing bearer HTTP/MCP/operator behavior. Browser cookie authentication
applies only to `/api/v1` (never MCP). No new permission tiers or accounts.

| Route | Contract |
| --- | --- |
| `POST /api/v1/session` | JSON `{"token": string}`; validate the supplied configured bearer credential and registered actor; rotate any prior cookie; return `{"authenticated":true,"actor_id":uuid,"api_version":1}` |
| `GET /api/v1/session` | With a valid browser session return the same identity envelope; otherwise 401 |
| `DELETE /api/v1/session` | Revoke the presented session and clear its cookie; return `{"authenticated":false}`; safe to repeat |

The login token is entered once and is never echoed, persisted in browser
storage, sent in a URL or logged. Retain only a digest of a cryptographically
random session token in an in-memory bounded store (maximum 256 live sessions,
eight-hour absolute lifetime); evict expired sessions, reject new capacity
rather than displace another live user. Revoke the prior valid session on
successful sign-in. A server restart requires sign-in again; this has no effect
on agent Chrome profiles. Use an HttpOnly, SameSite=Strict, Path=/ cookie named
`qorqut_session`, Max-Age=28800. This loopback HTTP increment cannot use Secure;
wider TLS deployment remains outside its claimed environment.

Require one exact allowed Origin on login, logout and cookie-authenticated
operation POSTs; retain exact Host/Origin validation and reject duplicate or
ambiguous credential headers/cookies. GET with absent Origin is valid, but any
supplied Origin must pass. Reject simultaneous bearer and browser credentials
on protected API calls rather than guess an actor. MCP always requires bearer
and never consumes a browser cookie as authentication. Invalid cookies cannot
reach a handler. Keep existing framing/body limits, timeouts, no CORS, no
redirects, no actor override, and sanitized SDK logging. Login receives the
same bounded/sanitized input handling as other routes. Do not put tokens into
Pydantic errors or representations.

## Optional static UI

Add optional `--ui-root` to `serve` and an optional explicit absolute Path to
`create_app`/`serve`. Preserve existing call signatures with a default of None
and existing configuration-file contracts. Validate this root at explicit
startup; no symlinked root or missing index. Serve only `/` and `/assets/...`
from that build, using safe path resolution without following symlinks or
exposing arbitrary root files. No fallback over `/api`, `/mcp`, `/health`.
With no UI root, core remains entirely headless. UI assets contain no secrets;
session requirements protect API data, not the sign-in HTML/CSS/JS.

Use no-store for HTML/session/API responses, nosniff and frame-ancestors 'self'
with a same-origin CSP compatible with local assets. Do not add third-party
CDNs, permissive script policies or external proxy infrastructure. The current
one-page UI needs no client-side route fallback or query parameters.

## Acceptance

Run a few real isolated HTTP/SDK flows: sign-in/read/mutate/logout; Origin and
ambiguous-credential refusal without mutation; bearer/MCP parity preserved;
expiry/restart/rotation; no token leakage in responses/logs; optional static
path escape/symlink refusal and core without UI. Add worker checks for actual
workspace jobs, crash-receipt reconciliation, recovery holds, alive/error health
and shutdown drain. Use injected temporary handlers only to exercise worker
boundaries, explicitly distinguishing them from native runtime readiness.

The chat assignment's real publisher will be tested together after both
changes pass independent review. Do not fabricate that integration result in
this assignment. No global install, VM setup, deployment, browser control or
native provider turn is part of these checks.
