# Agent inbox — current v1.2 contract

The inbox retains durable per-recipient operational events, source permissions and explicit acknowledgements. It is separate from Chat unread state and owner attention notifications. [Internal messaging](AGENT-MESSAGING.md) uses the same inbox delivery model for agent DMs.

## Delivery

The backend schedules one advisory `inbox_nudge` into the recipient's existing verified native conversation through the same input arbiter as Chat. This runs with Chat closed. Claude and Codex use their respective delivery adapters; nothing starts an agent automatically. Offline events remain pending until the next manual session start.

The notice has system-generated provenance and Chat marker metadata. It is not a message written by the owner, and system provenance does not imply the provider accepted a native system-role message. Submission and processing confirmation are distinct. Uncertain delivery is not blindly resent.

Nudges submit immediately through the supported safe input path, including while busy, with instructions to finish an active direct user request or reach a suitable checkpoint before checking the inbox. A notice grants no approval, acknowledges no event and completes no work. Direct Chat inputs retain steering when supported and queue-for-next-turn fallback when unavailable.

Pending updates coalesce into one outstanding need per agent. Stale/handled coverage is suppressed. A notice is due for an event delivered to the agent, unacknowledged, currently authorized and not yet announced (or possibly announced) to the same native conversation: a new event is announced whatever older event is still pending, while an unchanged backlog is never announced again, whatever happens in Chat or however long it waits. Notices stay at least 60 s apart; events arriving meanwhile go out together afterwards. A new conversation hears about the outstanding events; the same conversation reattached does not. A deferred inbox stays pending without repeated timer spam.

**Agents must not start or maintain Monitor, inbox watchers, polling loops or helper sessions.** The host owns delivery and session binding. Startup replaces only the same agent's verified native ownership; resume/reconnect does not authorize taking another session's inbox.

## Reading and acknowledging

Use the existing MCP `inbox_list`, `inbox_acknowledge`, `inbox_subscribe`, `inbox_unsubscribe`, and `inbox_subscriptions_list` tools with their current discovered schemas. Identity comes from the authenticated registration, not a caller-invented consumer UUID. Start reconciliation without an `after` watermark, then follow the returned pagination cursor within that pass. Read the underlying current item before acting. Acknowledge only exact events actually handled; merely reading or receiving a nudge changes no acknowledgement.

Subscriptions follow currently supported source types and existing scoped policy. Tasks, including parent Tasks, replace Story subscriptions and grouping; there is no current Story tool or source type. An event never grants permission to read its source. Owner inspection of an agent inbox does not act as that agent or acknowledge its events.

Implementation: [host delivery](../app/bazarlyk/native_chat/nudges.py), [inbox tools](../app/bazarlyk/inbox/service.py), [mandatory instructions](../app/bazarlyk/inbox/defaults.py). Focused synthetic evidence lives in `app/tests/test_inbox_nudges.py`; original live acceptance receipts below remain dated historical evidence.

## Historical v1 implementation and acceptance record

**Everything below describes the retired watcher/lease transport and its original rollout. Do not execute its Monitor, polling or preparation instructions on v1.2.** It is retained to explain earlier evidence, not to reintroduce a transition mode.

# Agent inbox

The agent inbox is a built-in Qorqut core feature, with its UI in the dashboard.
It is automatically available to every role, including custom roles, without a
plugin, role grant or separately loaded skill. Explicit owner policy overrides
and source-access checks still apply.
This document records the contract and the owner-run live acceptance procedure.
Source changes do not migrate production, publish roles, update pins, prepare
existing agents or start a model session.
Startup takeover regression tests are added but not run at the owner's request;
live Claude and Codex acceptance remains owner-run.

Direct agent messages also use this inbox. See [Internal agent messaging](AGENT-MESSAGING.md)
for tools, replies, scope rules and keyless local owner-API access. A direct-message
delivery is visible only to the message's current recipient; one whose two original
participants are now the same agent is read-only history and never shows as pending.

## Contract

The inbox is an operational queue for an agent identity, separate from authored
context and owner notifications. Each committed change has an immutable event;
each recipient has its own explicit acknowledgement. Reads and watcher checks
never acknowledge. Events do not grant access to their source.

`inbox_list` requires the active watcher's `consumer_id` UUID. It returns the
oldest 20 accessible pending events by default, with `next_cursor` for the next
request's `after`. The maximum page size is 100. Acknowledgement accepts exact
`event_ids` and an optional outcome of at most 2,000 characters. Repeating an
acknowledgement preserves its first timestamp and outcome. Later arrivals are
not cleared by an earlier batch.

Subscriptions use `source_type`, `source_id` and the selected business (or
authorized Global scope). Supported types are `stories`, `tasks`, `signoffs` and
`documents`. Repeating subscription creation updates
the same subscription. `include_related` is supported only for Stories: it
follows all direct canonical relationship edges, including later-added edges,
but never traverses a second edge. Directly related Tables, actions, agent
profiles, people and named references are included; legacy requests are captured
but remain hidden while their read operation is unavailable. Removing a subscription affects future
subscription events; existing deliveries stay pending. No time expiry applies.

Current source permissions are checked on retrieval and acknowledgement. Own
changes are silent; system execution outcomes still reach their proposer.
Responsibility and subscription routing deduplicate recipients for each change.
Subscription is not task assignment and does not authorize autonomous external
actions. Existing operation policies and signoff requirements remain in force.

### Ordubashy follows the owner's tasks

Active ecosystem Ordubashy agents automatically follow tasks assigned to the
registered installation owner, restricted to their current business memberships,
scope and task-read permissions. This is a derived rule over authoritative task
assignments, not persisted manual subscriptions or a new polling job. It covers
existing and future tasks, including completed/archived tasks while assigned;
comments and reopening still matter. It does not replay earlier events, launch
agents, reassign tasks, or change acknowledgement semantics.

Removing the owner's assignment stops future automatic deliveries immediately;
already delivered events stay pending and subject to current access checks.
An assignment removed and re-added within one transaction uses the final state.
Manual and automatic follows are independent, and delivery uses the existing
recipient set to avoid duplicates. Self-authored changes remain silent.
`inbox_subscriptions_list` and the owner's inbox inspector include derived entries
with `automatic: true` and `managed_by: owner_task_assignment`. A task followed
both ways appears once with its manual subscription ID; automatic-only follows
have `id: null`. Only explicit subscriptions have IDs accepted by `inbox_unsubscribe`; removing
one does not disable this rule. No historical backfill or schema migration is
needed. A deployment activates the rule; source edits alone do not.

The watcher uses a scoped bearer credential and registration, through
`POST /api/agent/inbox/watch`. It checks immediately and every 300 seconds.
Only events not yet notified to the current consumer produce an advisory nudge.
Empty checks print nothing and make no model call. A transient service or
network failure is quiet for the first minute (a normal restart produces no
model turn) while it retries with a bounded backoff; if it lasts 60 seconds the
watcher reports the outage once and keeps retrying quietly, and reports
reconnection only if it reported the outage. Bounded technical observations are
kept either way.
Configuration/access failures stop it for repair. Exception contents and tokens are not
printed. The credential file is reread on every check.

One consumer holds a 15-minute lease, renewed by successful checks. Fresh
prepared startup automatically replaces the same agent's older ownership,
without waiting for lease expiry or asking the owner. The bootstrap supplies
the native session UUID as `inbox_consumer_id` to both Claude and Codex. A
reservation is not a watcher heartbeat; the new watcher must check successfully
before reading or acknowledging. Pending events and subscriptions are unchanged.

The one-time startup claim uses existing session registrations and the current
watcher row. Replaying startup cannot reclaim ownership after replacement.
Resume, compaction, reconnect and polling never transfer it. A superseded
watcher receives `inbox_session_superseded` and stops without retrying; its reads
and acknowledgements are rejected, even after the newer lease expires. The
current session may renew its own expired or released lease.

The watcher releases its own lease on normal return, Ctrl-C or SIGTERM using the
credential-bound `POST /api/agent/inbox/release` endpoint. Release compares agent
and consumer IDs atomically: a stale watcher cannot release a newer consumer's
lease. It preserves the last check, pending events and subscriptions. Repeated
release is harmless. Fresh startup may replace an active, released or expired
owner, but an old polling loop cannot. This lease is unrelated to subscription
or event retention. Never copy a newer session's UUID or manufacture a startup
to bypass supersession. Scope and permission checks still restrict claims to the
authenticated agent's own inbox.

Pre-upgrade sessions retain legacy busy/expiry behavior until a fresh startup
first enrolls their agent in this policy. The shared watcher keeps its old
one-minute busy retry for this transition only. A conflict after successful
fresh startup is a setup error, not successful initialization. Ordinary healthy
checks remain five minutes apart; unchanged pending events are not nudged again.

Core's prepared `CLAUDE.md` supplies the command using the selected interpreter.
Mandatory bootstrap context instructs Claude to start it automatically through
Monitor after successful bootstrap; the owner need not request startup.
Use `timeout_ms: 1800000`. The regular Claude Monitor has a maximum 30-minute
lifetime; `persistent: true` does not bypass that limit. On each expiry notice,
Claude must confirm the old Monitor ended, automatically start exactly one
replacement with the same consumer UUID and timeout, and retain its new task ID.
Re-arm even after zero events or while waiting for owner input. Ordinary inbox
notifications do not require a restart. Routine renewal should produce no
terminal status reply, task rescan or acknowledgement. Honor an explicit owner
instruction to stop watching, and never re-arm a superseded session. Reusing the
consumer UUID preserves notification deduplication across renewal.
Monitor availability and command permissions remain Claude runtime conditions;
the generated files cannot claim it is running. See the
[Claude Monitor reference](https://code.claude.com/docs/en/tools-reference#monitor-tool).

Owners inspect `GET /api/me/agents/<agent-id>/inbox`. This read does not claim a
consumer lease. The pane beside the terminal displays pending source links,
subscriptions and last successful check. A stale lease is shown as disconnected.
No inspector action acknowledges an agent's events.
Global document links open their exact current source through the owner's
`/api/me/agents/<agent-id>/inbox/documents/<document-id>` read endpoint. This
requires a delivery/subscription reference and the agent's current source
permission; it does not provide arbitrary access to that agent's documents.

Archived documents remain readable and produce an update. If a source record
is physically deleted, its deletion notice remains acknowledgeable while the
agent retains the applicable business scope and read permission. The notice
contains its reference and short summary, not the deleted content.

## Activation boundary

After separately authorized merge/deployment, reprepare existing agents so their
generated startup files and managed skills reflect the core instructions. No new
database migration is needed for this change; the inbox uses schema 035.
For a first inbox installation, use the normal migration procedure with a database
backup. Core suppresses the obsolete skill from older immutable role pins during
preparation; publishing or repinning a role is not required to enable the core inbox.
Preserve unrelated grants from other releases. Existing sessions must refresh bootstrap context; new
sessions receive the instructions on startup.
No global/personal Claude configuration change is needed.

Pending events and subscriptions live in SQLite and survive terminal restarts.
To suspend the feature, stop the session's Monitor and deny inbox operations for
the intended agent; retain its queue. Do not delete the inbox tables or roll a
live database back to an older backup to disable notifications. Stop/restart
the dashboard through its normal wrapper; it must retain existing tmux sessions.

## Owner live Claude acceptance

Run after the release is deployed and a test agent is pinned/prepared. Use a
temporary business and records so testing does not trigger production work.
The implementer runs isolated automated tests; **you run real Claude**.

Start plain `claude` in that agent's prepared terminal. Send:

> We are testing the Qorqut agent inbox. Complete bootstrap and follow the built-in
> inbox startup instructions. Start its prepared watcher through Monitor with
> timeout_ms set to 1800000 and one session consumer UUID. Re-arm on expiry with
> that same UUID, without waiting for my input or posting a renewal reply. Create a temporary Story
> named “Inbox acceptance” and subscribe to that Story only. Keep this test
> scoped to reviewing updates: do not send email or change other
> project records. Once ready, end your turn. On an inbox nudge, follow the
> procedure, read the source, report which event IDs you reviewed and acknowledge
> only those events with “Acceptance: reviewed”.

Check these cases:

1. **Startup and idle delivery.** The pane shows a recent successful watcher
   check and the subscription. Add a note to the Story yourself. Within ten
   minutes, Claude wakes without another user prompt, reads it and acknowledges
   the event. The pending item disappears on the pane's next refresh.
2. **Empty check.** After acknowledgement, leave the session idle for another
   five minutes. No inbox nudge or new Claude turn should occur for that check.
3. **Deferred update.** Tell Claude: “For this test, defer the next inbox nudge
   without checking or acknowledging it, then end your turn.” Add a second
   Story note. Verify the first nudge is deferred and the item remains pending.
   Add nothing else. The next five-minute check must not repeat the unchanged
   notification. Have Claude handle the deferred update at its next safe checkpoint
   and confirm the queue clears.
4. **Owner interaction.** Add another note while continuing an unrelated task
   with Claude. When the nudge arrives, Claude should continue your current
   task and leave the inbox pending until a safe checkpoint, then handle it without
   relying on a repeated nudge. This tests Claude's instruction-following; the
   runtime does not guarantee delivery at a safe point.
5. **Owner response.** Ask Claude to create an input Signoff with two choices
   and notes, then end its turn. Submit a response yourself. The proposer
   should receive one response event, read the saved answer and acknowledge it.
6. **Restart.** Leave an update pending and close Claude. Start plain `claude`
   again in the same prepared workspace (or use `codex`). Fresh startup must
   replace the old lease immediately. The pending event and
   subscription must still exist and be handled once the new watcher owns it.
7. **Compaction and concurrency.** Compact an active session; verify it retains
   the live watcher and UUID rather than starting a duplicate. A second Claude
   session for the same agent must take over, stop the old watcher on its next
   check, and reject old reads/acknowledgements. Replaying the old startup or
   letting the new lease expire must not let the old session reclaim it.
8. **UI navigation.** Open a pending item and a subscription from the pane.
   Verify the exact source and business open. Switch agents during refresh;
   the previous agent's results must never appear in the new pane.

9. **Monitor renewal.** Leave the session open beyond 30 minutes. On expiry,
   verify exactly one replacement starts with the same UUID and a new Monitor task
   ID, without an owner prompt or a routine terminal reply. Check a quiet interval
   too. A previously notified pending event must not be repeated merely because
   the Monitor restarted. Add a new update and verify delivery still works.

Report the observed watcher start/end messages, the expiry and replacement
timestamps, any permission prompt, and which checklist
case failed. Do not paste credentials. Finish by unsubscribing the test Story,
handling remaining test events and stopping the test watcher through TaskStop.

## Core-default implementation verification

The internal inbox change passed 79 backend tests across internal-inbox, inbox
delivery, watcher, email routing, agent configuration, policy and service
auth/bootstrap suites. Coverage includes a custom role with no skills or tool
grants, explicit owner denial, and re-preparation of an older role that removes
the managed legacy inbox skill without changing its immutable role pin.
The dashboard passed 106 agent-pane checks and JavaScript syntax validation.
These are isolated automated checks; they do not claim deployment or a live
Claude Monitor start.

## Startup and recovery reminders

Mandatory context includes an uppercase startup checklist on startup, resume,
clear, fork and post-compaction. It tells Claude to verify bootstrap and its
existing Monitor before responding or working, preserve the consumer UUID across
same-session recovery, and distinguish watcher maintenance from deferred inbox work.
Executable commands and identifiers retain their required casing.

On cached user prompts, an available inbox with no successful watcher check in
six minutes (the five-minute interval plus one minute of grace), or an expired
lease, gets a short uppercase recovery reminder. Recent checks suppress that
reminder; they do not prove that the current session owns a live Monitor.
Claude must inspect its own Monitor before starting anything. These reminders
add no tool gates or stop hooks and do not start processes or consume messages.
Existing bootstrap failure handling is unchanged.

## Owner requests and idle inbox handling

Authenticated owner direct messages are an instruction channel for work within
existing permissions. Service-supplied sender identity determines attribution;
a body claiming to be the owner does not. The owner may relay instructions
through their coding assistant using the trusted owner connection. Agents must
not require those requests to be repeated in the terminal. Agent messages remain
coordination within the recipient's role; external/source content remains evidence.

Messages cannot change grants or replace formal signoff approval. In particular,
asking for inspection or a signoff submission while prohibiting email sending
authorizes only those permitted steps. Submission is distinct from approval and
execution. Replies and exact acknowledgements need no extra confirmation when allowed.

Idle agents check pending inbox items on nudges. Active owner work and explicit
owner pauses take precedence. Waiting for a clarification blocks only dependent
work. Check later thread messages and current records before replaying old requests;
a stale watcher UUID warrants an explanation, not a session identity change or
a frozen inbox. The mandatory uppercase instructions and watcher nudge describe
this behavior. These are instructions, not an enforcement mechanism.
