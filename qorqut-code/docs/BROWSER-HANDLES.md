# Short browser handles

`browser_turn` version 2 keeps reservation credentials on the server. This is the
MCP path used by Ordubashy, explicit native Codex helpers and durable workers;
the business agent's shell launcher is unchanged.

A native caller acquires once:

```json
{"action":"acquire","protocol":"handles"}
```

`consumer_id` (a session UUID) is optional; without it the handle names the agent's default session. The browser skills
tell agents to leave it out.

Native handles name the caller's own lease-free configured browser session. They
are derived from the agent and session, stored nowhere, never expire and are never
superseded; see [browser command recovery](BROWSER-COMMAND-RECOVERY.md). The
reservation rules below apply to durable workers.

Durable workers use `{"action":"acquire","protocol":"handles"}`; their authenticated assignment
already fixes the browser scope. An owning response returns a short opaque
`handle`, such as `b0123456789ab`. A waiting response returns queue state and no
handle. Durable waiting/resumption remains dispatcher-driven, without parent
model polling. After resumption, acquire in the new generation.

```json
{"action":"command","handle":"b0123456789ab","action_id":"<unique-action-UUID>","command":["observe","overview"]}
{"action":"release","handle":"b0123456789ab"}
```

All follow-ups use `handle`: status, command, checkpoint, renew, artifact,
reconcile and release. No consumer UUID, turn ID, lease ID, capability token or
browser target needs copying after acquire. Repeating an acquire supersedes its
previous handle; it does not authorize replaying an uncertain action.

Routine command replies contain the command output, exit code, action ID/state,
and nonempty artifacts/errors. They do not echo the handle, turn/lease IDs,
target, expiry or checkpoint. If command completion changes ownership, the new
state remains visible. Acquire/status expose relevant queue/checkpoint state;
renew returns the expiry; checkpoint/release return state. Full reservation and
action records remain on the server. An action ID is a retry/reconciliation
reference, not browser ownership bookkeeping.

## Files a command wrote

A command reply lists `artifacts` only for the files that command created or changed, and leaves the key out when there
are none. The service reads the command's work folder before and after the command and lists a file when it is new or its
size or change time differs. `command.lock`, `agent-browser.json` and `*.meta.json` are bookkeeping and are never listed.

Earlier files are not gone. They stay readable through `action: "artifact"` with their `artifact_handle`; the handle format
is unchanged. This matters because an agent's own browser has a handle that never expires, so its folder only grows: listing
all of it put the same 64 entries (about 20,000 characters) on every reply, even for a click that prints "Done", and being
sorted by name it often left out the file the command had just made. One reply lists at most 64 files, sorted by path;
a file over 32 MB is not listed. A delegated assignment's command is listed the same way, and a batch (below) is listed once
for the whole run.

## Several commands in one call

`action: "command"` takes `commands`, a list of 1 to 10 command arrays, in place of `command`. Give exactly one of the two.

```json
{"action":"command","handle":"b0123456789ab","commands":[["fill","e1","Ada"],["fill","e2","Lovelace"],["press","Enter"]]}
```

- The commands run in order under one use lock and one read of the target, so a switch between the agent's own browser and
  a Shared one cannot fall between them. Every command is validated before the first one runs; a refused command (the reply
  names it as `commands[N]`), a browser that is not ready, or a first command the runner cannot start raises as a single
  command does, with nothing run. `captcha` and `tab-reference` are refused inside a batch (`commands[N]: captcha goes in a
  call of its own, as command.`), so they always run alone.
- The run stops at the first command that exits non-zero, times out or fails; the later ones are not run.
- Only the lease-free path takes `commands`: an agent's own browser, or a worker's browser selected by Ordubashy. A delegated
  assignment keeps one command per call, because each action there has a durable record that can be reconciled; `commands`
  on it is refused with `validation_error`.
- `action_id` (or `idempotency_key`) names the whole batch. `action.state` is `succeeded` when every command ran with exit
  code 0, `uncertain` after a timeout and `failed` after any other stop.

Reply:

```json
{"results":[{"command":["fill","e1","Ada"],"exit_code":0,"stdout":"Done"},
            {"command":["fill","e2","Lovelace"],"exit_code":1,"stdout":"","stderr":"e2 not found"}],
 "exit_code":1,
 "stopped":{"index":1,"reason":"exit_code","not_run":1,
            "message":"Command 1 exited with code 1. The later commands were not run."}}
```

- `results` has one entry per command that finished: `command` (as sent), `exit_code`, `stdout`, and `stderr` when not
  empty. `exit_code` at the top is that of the last command that finished, and is left out when none did.
- `stopped` is present when the run did not finish cleanly, including when the last command is the one that failed.
  `index` is the zero-based command that stopped it, `reason` is `exit_code`, `browser_timeout`, `browser_failed` or
  `time_budget`, and `not_run` counts the commands that never started. A failed command is in `results` at `index`; a
  timed-out one is not, because its output is lost; a `browser_failed` or `time_budget` command never started.
- A timeout inside a batch does not raise. The agent has to see what already ran before it repeats anything, so the reply
  carries the earlier results and `stopped`. A first command that cannot start raises, as above; a later command that the
  runner refuses to start (for example another command holds the browser) is reported as `browser_failed`.
- `artifacts` covers the whole batch: the folder is read before the first command and after the last.
- `stdout` keeps its first 24,576 characters per command (above the largest `read` or `observe` budget), and the batch
  keeps 65,536 characters of `stdout` in all. An entry cut for either limit keeps its start, where `read`, `observe` and
  `snapshot` put the address, title and first refs, and carries `truncated: true`.
- Any other exception after the first command also ends the batch as `browser_failed`, with the earlier results kept and
  a message that the command may have reached the page. On the first command it raises. `stderr` keeps 16,384 per command with no batch
  limit.

**Time.** An MCP request lives 60 seconds on the agent's side: the stdio bridge (`qorqut/bootstrap/mcp_reconnect.py`) gives
each request one `RETRY_WINDOW` and each HTTP request one `HTTP_REQUEST_TIMEOUT`, both 60 seconds. The tool timeouts of the
CLIs (`tool_timeout_sec` and `.mcp.json` `timeout`) are 240 seconds, so the bridge binds first. After 60 seconds it answers
a write as uncertain and the agent loses the whole reply. The runner lets one command take 45 seconds, so before each command
after the first the elapsed time is checked, and when 45 seconds from now would end after 55 seconds (60 less 5 kept for the
bridge, the hop to the service and the reply) the run stops with `time_budget`. In practice a batch of quick commands (the
median is 0.9 s) is not affected; one slow command can end it early. The first command is never refused for time.
The constants are in `qorqut/browser/access.py`, and a test fails if the bridge's window changes.

## Explicit Global selection (configured Ordubashy only)

Ordubashy alone may add `browser_id` to a handles `acquire`: a managed Global desktop's surface id, or an active permanent
worker's agent ID for that worker's own browser, both from `action: "list"`. Omitting it keeps Ordubashy's own configured
browser. The handle names that exact browser, so later calls use
only the handle, and a different `browser_id` is refused. Projects have no browsers: `business_id` is not accepted. Identity,
role, the browser-control skill and the operation permission are rechecked on every call. See
[browser desktops](BROWSER-DESKTOPS.md) and [Ordubashy using a worker's browser](BROWSER-MANAGEMENT.md#ordubashy-using-a-workers-browser).

## Ownership and recovery

Migration 088 adds private `browser_handles` records linking the short ID to
its exact original turn, lease, token, authenticated identity and worker
generation. They survive process restarts; they grant no new authority and are
not portable between caller registrations/credentials. Native helpers sharing
parent credentials remain within that parent's trust boundary, as before.

Every ordinary use checks the saved binding and current reservation before the
existing browser operation rechecks scope and ownership. Release, expiry,
cancellation, lease replacement and worker replacement invalidate execution.
Never resolve a stale handle to the latest lease or silently retry a command.
The old lower-level `worker` method remains an internal execution adapter; MCP
uses `tool`, which resolves the handle before calling it.

Only reconciliation has a deliberate generation exception: the authenticated
current generation of the same assignment may use its old handle to settle an
uncertain action. The existing reconciliation rules still check assignment
ownership and running state. This permits no command/renew/release with an old
handle. Native reconciliation is a no-op; configured browsers keep no fence.
Action intents, idempotency, FIFO handoff, uncertain-command fences and artifact
access remain in the existing backend. Handles retain recovery references;
released records are history, not active browser owners or running sessions.

## Release boundary

Core may deploy before role publication/adoption. The compatibility default
retains v1 acquire responses and raw follow-up inputs for existing instructions.
New callers explicitly acquire with `protocol: "handles"`; follow-up calls need
only the handle. Raw fields remain labeled legacy in discovery during transition.
A handle never falls back to the legacy path. Ordubashy 1.37.1 selects the compact
protocol when separately published/adopted and prepared. Do not replay an existing
v1 action through a fresh handle. Preserve configured
Chrome, its tabs/profile, operational data and agent terminals. No provider trial,
role adoption, merge or deployment is performed by this source change.

The reviewed release manifest must include the exact commits and migration 088.
A pre-088 binary refuses the newer schema; recovery requires a reviewed compatible
forward release or separately approved database recovery, not an automatic
switch to the old binary or an automatic database restore.

## Focused evidence

`tests/test_browser_short_handles.py` adds ten focused cases for handle-only
execution/MCP schema, database reopen, action replay, caller/selector isolation,
lease expiry/replacement, durable waiting/resumption/cancellation, private
business gateway tokens, artifact delivery, migration from 087, cross-generation
uncertainty reconciliation, delayed lease replacement, and compact receipts.
All ten passed in targeted runs; only failed new fixtures were corrected and
retried. Previously accepted tests were not rerun. Existing transport doubles
were adjusted to the new entry point without rerunning their suites.

These checks use a real temporary SQLite database, queue and browser adapter,
with the external browser command runner stubbed. MCP coverage exercises real
binding/dispatch with a fixture caller/application authorization layer. They are
not a new live Chrome/provider trial or a measurement of model token savings.

An additional compatibility check passed for unrefreshed v1 acquisition, command
and release alongside explicit handle opt-in, including rejection of stale-handle
downgrades. The ten previously accepted checks were not rerun.

`tests/test_browser_result_artifacts.py` covers the file listing on the own-browser path, a worker's browser selected by
Ordubashy and the leased path. `tests/test_browser_command_batch.py` covers each rule of a batch, the handle layer's compact
reply, the MCP envelope and the `commands` schema. Both use a stand-in command runner and a temporary folder; no live Chrome,
real MCP bridge or provider is involved, so the 60-second figure above is read from the code, not measured.
