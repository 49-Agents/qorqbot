# Own work on startup

Startup now embeds a compact index of the agent's own unfinished assigned tasks,
requested Signoffs and pending inbox changes. These queues are separate from the
10-item working-attention sample. Manager overview tasks cannot displace own work.
Snoozed tasks remain visible and marked deferred; closed/archived tasks are excluded.
Signoffs include current requested proposals and outcomes, including historical
terminal states. Revisions and pending inbox events expose changes without claiming
a new persisted “since last session” baseline.

Business agents load their business queue. Global agents traverse Global and their
currently authorized business scopes, preserving each record's business ID. All
task/Signoff reads use canonical operation authorization and pagination. Inbox
summaries use the existing recipient/source visibility checks. No foreign-agent
inbox, global grant expansion, watcher claim, acknowledgement or source mutation
is introduced. The owner can inspect a target agent's inventory, under that agent's
current permissions rather than the owner's broader grants.

The embedded page contains up to five records per category. `complete` and
`next_cursor` explicitly identify unfinished pagination. Before work, the shared
core role instructions require following the cursor with:

```json
{"inventory": true, "inventory_after": "<next_cursor>"}
```

Use `agent_context_get` for that request. Without a cursor, `inventory=true`
refreshes from the beginning. Tool reads default to 25 records per category;
`inventory_limit` accepts 1–50. Cursors bind the target agent and selected scope
directory; changed access invalidates the cursor or reports unavailable queues.
An unavailable queue is not an empty queue. Pagination reads live state, not a
frozen snapshot; refresh after concurrent work changes when reconciling an action.

The index contains short titles, state/revision, scope, deferral/blocking flags,
Signoff decision/execution/attention state, and inbox source/event identifiers.
Long titles and event summaries are explicitly marked shortened. It does not
repeat task notes, full messages, proposal bodies or decision evidence. Read those
through their canonical scoped tools before acting or acknowledging an event.
Loading a page never acknowledges any event or claims a watcher lease.

When startup's remaining context budget cannot hold the index, the shared startup
instructions require an explicit inventory read and full pagination before work.
Saved working memory remains available through ordinary `agent_context_get`.
No claim is made that arbitrary queue sizes fit inside one model context window
or that model compliance follows merely from delivering instructions.

Deployment requires core installation and re-preparation of affected agents to
refresh the shared `/role` / `$role` procedure and bootstrap integration. No role
package republishing, database migration or automatic session restart is needed.
This branch does not deploy or change live agent profiles.

## Verification

40 distinct targeted tests passed across runs: startup inventory, working memory,
startup reminders, shared role workflow and Codex environment checks. Coverage
includes complete multi-page queues, scope binding/revocation, another agent's
cursor rejection, explicit denied queues, answered Signoff revisions and pending
changes, unchanged inbox acknowledgements/leases, budget fallback, saved handoff
preservation and the installed MCP test transport. The opt-in real Codex CLI
acceptance test was skipped. Fixtures used isolated temporary installations;
no live agent prompts, provider calls or production data writes were performed.
