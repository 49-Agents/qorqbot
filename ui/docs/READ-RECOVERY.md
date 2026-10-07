# Preserve a healthy Terminal through failed status reads

Manager D49, 2026-10-09. The actual connected Codex run had ten operation-capacity
503s. A failed `terminal_get` clears Store's view, so TerminalClient.observe(null)
closes a healthy connection. This correction distinguishes a failed read from a
positive terminal-state change. Core's bounded admission fix runs independently.

Keep the last validated runtime/terminal observation on transient reads such as
`unavailable`, network failure or a 5xx transport refusal, and retain a visible
error/stale label. Do not call a retained runtime view current/connected without
its observation qualification. Continue normal polling and allow manual retry;
successful reads clear the error. Preserve the existing generation/selection and
recovery-epoch guards and memory-only drafts.

An already connected Terminal must keep its WebSocket, viewer ID, acknowledged
control epoch, queued/in-flight frame handling and displayed output when only
the HTTP status read fails. In this case do not call observe(null) or feed a
stale HTTP control snapshot into the client. Show a concise observation warning.
The socket's normal valid hello/control/frame observations still apply, and the
server continues to validate every input. A failed read grants no new control.

Do not open a new/reconnected socket from a retained stale view while its status
read is in error. A successful current read is required. Positive unavailable,
stopped or changed-terminal responses, malformed responses, auth expiry,
selection/recovery changes and actual WebSocket failure keep existing teardown
and unsent-queue-clearing behavior. Never replay input, take control automatically,
or change provider/session management. Invalid observations are not transient.

Scope: `src/store.ts`, `src/terminal.tsx`, and only if needed the current runtime
inspector presentation. TerminalClient's established valid-identity and input
receipt rules remain unchanged unless a minimal explicit observation-error
signal is necessary. No schema, dependency, icon/style overhaul, unrelated status
rewrite or example. Add compact cases to the existing owning Playwright suite:

- Inject one transient runtime/terminal read error into an established Terminal;
  warning appears, the same socket/control survives, no frame is replayed, and
  a later valid poll clears the error.
- A failed first/reconnect read does not open a socket from a stale cache; a
  positive identity change or malformed observation still closes the old socket.

Run those focused Chrome cases plus the production build. Do not repeat the full
native-provider flow or broad suites. Preserve original synthetic failures and
label them as protocol fixtures. Manager will test the combined real-core change.
Commit a short evidence file with exact commands/results and limitations.
