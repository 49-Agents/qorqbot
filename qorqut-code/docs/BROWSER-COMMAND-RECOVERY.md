# Browser command recovery

## Configured browsers are lease-free

An agent's own configured Chrome (`configured:<agent>`) takes no turn, lease,
command fence or durable action intent for native `browser_turn` callers or the
main-session launcher. `command` runs at once, with or without a handle.
`acquire` returns a stateless handle immediately; the handle is bound to the
agent and never expires. `status`, `renew`, `checkpoint` and `reconcile` are
instant no-ops, and `release` succeeds without changing anything.

A timed-out or failed command returns an error and leaves nothing blocked. The
CLI is killed at its timeout, but the agent-browser daemon and page scripts may
still finish the work, so inspect the page before repeating a state-changing
action. The main session and native helpers may interleave on the same Chrome.
Each caller session gets its own agent-browser session, and a second command in
the same session fails fast instead of waiting.

Native turns left from the leased model no longer gate anything. Once expired,
they are released at the next queue check, including startup recovery, and
their uncertain actions stay as history. Durable assignment workers, including
those scoped to a configured browser, keep the exclusive-turn model below.

## Native JavaScript dialogs

A native alert, confirm or prompt can block page commands, including `press Escape`.
Send `command: ["dialog", "dismiss"]` through the same `browser_turn` handle to cancel,
or `["dialog", "accept", "text"]` to accept a prompt with a value. Acceptance without
a value uses `["dialog", "accept"]`. The existing `dialog-dismiss` and `dialog-accept`
spellings remain supported, including on hosts predating this correction.

Both spellings pass the same command validation and target/session restrictions.
Resolve the dialog according to the authorized task and inspect the page afterward;
do not reload or close an unsaved form to remove the prompt. No migration is needed.
The core correction works with existing role pins; companion role instructions need
separate publication/adoption. The disposable-Chrome check in
`app/tests/test_agent_browser_backend.py` verifies prompt recovery preserves form values.

## Rejected commands on leased turns

Validate commands before persisting an action intent or command fence. Action
identities are bounded strings, including the existing idempotency-key fallback;
reconciliation must reuse that exact identity, not invent a replacement UUID.

Legacy uncertain actions can be recovered by their exact turn owner and lease
only when the stored command still fails pre-execution validation and the saved
error matches that rejection. Release performs this narrow recovery automatically,
even after lease expiry. Potentially executed actions remain fenced until the
current assignment generation reconciles them; caller claims are not evidence
that execution did not happen. No browser tabs are closed.

No migration, role adoption, or agent re-preparation is required. Test rejection,
expired leases, foreign owners and uncertain executable commands with fake
browser targets.
