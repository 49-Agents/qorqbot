# Local access without authentication

Decision D55, 2026-10-09. The owner explicitly removed application authentication:
no console login, tokens, passwords, cookies, expiry or sign-out. This supersedes
the authentication parts of D26/D35 and the transport/console plans. All existing
application operations remain unrestricted. Actor identity remains attribution,
not an authorization boundary. Native CLI/provider account login is independent.

Core continues binding to loopback. Retain ordinary Host/Origin validation,
request framing/size bounds and terminal control ownership; they introduce no
login step. Do not add authentication modes, proxy token injection or automatic
sign-in to retain the old auth system underneath.

## Core changes

At explicit server startup resolve the installation's sole registered owner via
a small application read method. Zero or multiple owner identities is a clear
configuration error rather than silently selecting another account. This existing
installation has one owner. No schema, recovery-epoch or public-operation change.

HTTP, MCP and browser WebSocket calls default to that local owner. An optional
`X-Qorqut-Actor` UUID selects an existing actor for accurate agent attribution;
it is public, self-declared identity, not a secret. Validate a single UUID value
and retain existing registered-actor/target checks. Malformed identity must not
silently become the owner. Ignore obsolete Authorization and browser cookies:
neither grants nor limits access. No bearer/cookie credential validation remains.

Keep `GET /api/v1/session` as the small local identity discovery endpoint, returning
exactly `{ "actor_id": "<owner UUID>", "api_version": 1 }` for the default browser
request, without Set-Cookie. Remove its POST/DELETE sign-in/sign-out operations.
Terminal sockets use the attributed actor and existing viewer/control epochs;
remove cookie expiry/revocation checks while preserving teardown, input ordering,
recording, takeover and recovery behavior.

Remove the credential store and session manager from production composition.
`create_app(settings, ui_root=None)` and `serve(settings, ui_root=None)` no longer
accept credentials. `serve` CLI needs only `--installation` and optional `--ui-root`;
remove the transport credential file requirement. Operator `call` uses optional
`--actor-id` instead of `--token-file`; its callable is
`call(url, name, arguments, actor_id=None)`. Preserve its HTTP bounds/no-retry behavior.

NativeSetup generates `X-Qorqut-Actor` with the agent actor UUID for core MCP/hooks,
using existing managed-file replacement to update previously generated bindings.
Remove per-agent token generation and credential-registration callbacks. Native
hook requests use the same attribution path without a separate bearer gate.
Do not modify native/provider authentication, user-authored config, process/session
ownership, native capture or dispatch readiness. Existing credential files may
remain unused on disk; their contents must never enter Git or reports.

## UI changes

Open directly via GET identity -> schema -> current workspace. Remove token form,
Sign in/Sign out controls, cookie expiry states, login/logout methods and related
CSS. Keep a small connecting/error panel with Retry connection. An unexpected
401 is an ordinary refused connection, never a request for a password. Use the
new identity shape with no authenticated field. Retain actor identity on pending
mutations to prevent replay under a different installation/owner; explain a
mismatch as a different workspace identity. Preserve drafts, idempotency keys,
uncertain-mutation handling and generation/epoch fences through connection retry.

## Work and validation

Two independent Astra MAX workers implement Core and UI on isolated worktrees.
Manager reviews the results, merges the pair and updates the existing owner test
instance without replacing its data, agent IDs, tmux session or user CLI.

Adjust owning test fixtures for the new contract and remove obsolete auth tests;
do not retain an optional auth mode to keep old tests passing. Use a few grouped
checks: unauthenticated identity/HTTP/MCP with correct actor attribution, actual
cookie-free Terminal control, managed MCP/hook identity replacement, and direct
UI opening/retry with drafts preserved and no login controls. Keep existing
non-auth behavioral assertions. No broad new suite, dependency or service.
Manager performs one real browser smoke through the existing test URL, including
Terminal input and saved Chat. No model call or native CLI relaunch is required.
