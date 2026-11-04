# Agent management and completion checks

Source changes do not change live role pins, agent registrations, the installation
coordinator, or deployment.

## Management without impersonation

Business agents use `management_message_send` for durable management requests.
An owner explicitly selects the installation's active Ordubashy with
`management_coordinator_set`. The service routes to this exact principal; it
never chooses a similarly named agent or gives it the owner's identity.

`management_list/get` read canonical message/input sources. `management_route`
changes attention destination with a revision check and an escalation reason;
`management_resolve` resolves message intake only. Signoffs retain their own
lifecycle. Current business membership, scope and operation policy remain
authoritative. Scope revocation removes access to the corresponding records.

New ordinary question forms default to `response_policy=management_allowed`.
An eligible business-agent question automatically enters management intake when
the exact coordinator is configured and in scope. An offline but configured
coordinator can receive durable requests; the UI reports observed availability
separately. Missing/inactive configuration is explicit and never implies a reply.

`signoffs_input_answer_delegated` permits that coordinator to answer an eligible
pending question with his own attribution, exact proposal/hash/status binding,
and the normal application idempotency key. It cannot approve a platform action,
produce an application receipt, answer owner-only input, or act outside current
scope. Authentication, payments and other owner intervention use owner-only
requests. Existing input without the policy field remains owner-only.

Owner attention, badges, notifications and the default Signoffs list exclude
eligible pending inputs routed to management. Protected approvals are never
hidden by an ineligible routing row. Management's All items view and
`signoffs_list(include_managed=true)` preserve inspection. Explicit owner
escalation returns an item to owner attention.

## Current work and the Codex stop check

Presence, the current-work declaration, its last explicit review, and task
progress have separate timestamps. `agent_work_update` can confirm the same
items, including several simultaneous tasks or an unchanged wait. Confirmation
does not append a progress note or create an inbox event. Task notes remain
brief, server-timestamped and append-only; task transitions remain explicit.

The prepared native hook records a small local action watermark before tool
execution. It excludes known read/bookkeeping tools and understands the explicit
ordered `operations_batch` schema. Put the work review after substantive actions
in that batch. Opaque shell/JavaScript/browser actions require a review; the hook
does not parse source code or infer which task an action completed.

At Stop the helper makes at most one bounded metadata request, with a one-second
timeout. It checks up to ten listed work items and finds only proven omissions:
an unreviewed action watermark, a finished/archived task still listed, or an
inaccessible task reference. Outcome notes and intended handoffs without reliable
structured evidence remain instructions, not automatically inferred violations.

Two receipts enforce the no-loop rule: a private, fsynced native turn receipt
and an atomic server claim for the finding episode. There is at most one grouped
correction per original (root) turn and one automatic presentation per finding
episode. `stop_hook_active=true` always forbids another correction. Task and
owner-inbox findings keep their episode across turns: restarting a helper,
changing a timestamp, or receiving an unrelated inbox event does not reset an
unresolved episode. The work-review reminder is one episode per substantive root
turn: repeated Stop events, continuations and saves made in response never
produce a second one, while a later substantive turn may be reminded again even
if an earlier reminder was ignored. An ignored reminder stays inspectable in
Fleet/startup until reviewed or replaced by a later turn's reminder. A lost
response can lose a reminder; it cannot gain another automatic continuation.
Unknown lineage or unavailable storage allows stopping.

The work-review text asks the agent to bring its existing current-work entry,
working memory, and relevant task or business notes in line with completed work,
decisions received and remaining blockers, changing only what changed and never
recording a proposal as an approved decision. Saves made in response
(`agent_work_update`, `working_memory_update`, own `notes_create`/`notes_update`,
`tasks_update`, `tasks_comment`, `inbox_acknowledge`) are bookkeeping and do not
make the turn dirty again. The registration lists the agent's exposed read
operations, which never count; neither do Claude's native read, search, planning,
Monitor or subagent-launch tools. A greeting or read-only turn gets no reminder.
The hook writes no note, memory, commit or transcript copy of its own.

Enablement is recorded per client as `checkpoint_protocol` in the prepared local
registration. The actual `codex-cli 0.154.0` and `0.156.1` contracts were verified with an isolated
loopback provider and MCP fixture: native Bash and MCP Pre/Post hooks, Stop, one
corrective continuation, unchanged native turn ID, and the second Stop's active
flag. Only those verified Codex versions enable protocol 1; other supported Codex
versions preserve observation/legacy behavior until verified. Claude Code enables
protocol 1 for every version that meets the preparation baseline
(`compatibility_report` `supported`, whose required features now name
`stop_hook_correction`): Claude's Stop `decision: block` and `stop_hook_active`
contract predates that baseline. Claude hook payloads carry no turn ID, so the
hook mints a UUID turn marker at `UserPromptSubmit`, or at the first action after
a finished Stop (a notification-started turn), stores it in the session's activity
checkpoint and uses it at Stop; a correction continues under the same marker.
The Claude path is covered by fixtures only; a live session check belongs to the
owner. Protocol 1 disables the older periodic mid-turn reminder for that client.
No personal Claude/Codex configuration or production terminal is involved.

The reminder improves update discipline; it provides no continuity. A crash,
forced termination, killed hook or lost service response can skip it, and work not
saved before then may be lost. Working memory, activity and transcript snapshots
keep their existing limits and semantics.

## Inbox notices

New v3 notices identify exact event IDs and a server-issued notification UUID
bound to the authenticated agent and current inbox consumer. Issuing/notifying
never acknowledges work. At prompt submission a notice is cancelled only if
its provenance is recognized and all its exact events are already handled or
no longer accessible. A newer arrival cannot revive an old notice.

Older exact-event v2 text needs saved delivery provenance. Count-only v1 text
cannot prove its source and is not automatically cancelled; quoted text and an
unknown UUID cannot cancel an ordinary user prompt. The five-minute polling
cadence is unchanged. Watcher version 3 replaces an older helper on normal
re-preparation without requiring a terminal restart. Native hook configuration
changes may still require the CLI's normal reload/restart behavior.

## Model-free incident reporting

The application dispatcher, authenticated context/preparation paths, checkpoint
and inbox service boundaries, and background job outcomes observe typed faults.
The reporter neither calls a model nor retries a business/provider action.
Unexpected software faults and missing registered internal workers are actionable
immediately. Repeated technical transients need three failed attempts spanning
ten minutes; successful probes reset that transient run. Ordinary permission,
validation, login/setup, approval, hold and revision outcomes are not bug tickets.
Unknown error codes are not promoted to software faults by guessing from prose.

Failures append sanitized codes and trusted IDs to a private durable spool;
raw error text, arguments, credentials and transcripts are discarded. The service
drains at most 100 observations per minute into one System Dev ticket per stable
fault signature and business scope. Repeated faults update counts and bounded
evidence. Different businesses are intentionally separate privacy boundaries.
Context failures before scope resolution record the authenticated agent without
inventing a business or task association.

A successful operation is an availability probe, not proof of repair. Recovery
is tracked per affected agent/episode; one healthy agent cannot clear another's
failure. Recovery notifications reuse Inbox and recheck recipient access. Task
associations arise from an explicit `tasks_comment` reference of
`{"type":"dev_ticket","id":"…"}`; the service never guesses a task from a
multi-item work list. Existing dependencies and subscriptions carry normal
handoffs. Recovery does not finish tasks, remove owner holds or authorize sends.

Closing an automatic incident through the canonical ticket requires resolution
and verification evidence. An optional deployment reference is retained with
the outcome. Closure notifies affected agents and currently linked task assignees
once. A later genuine recurrence reopens the same ticket while preserving repair
history and gives the coordinator one new notice. Repeated failures while it is
open do not repeat that notice. An ordinary success probe cannot reopen a verified closure.

The reporter is an attributed system principal, registered lazily by the service
worker, with no human approval or business-send powers. Assembly starts no worker,
performs no migration and writes no live configuration. Observer failure preserves
the original operation result. Ingestion failure retains its spool for retry and
does not report itself recursively. Reporting health is visible in System Dev.

## Bounds and UI

No new dependency, message broker, database, model supervisor or per-action HTTP
request is introduced. Existing activity sampling and visible Fleet refresh stay
at one minute; manual refresh remains available. Startup summaries are bounded
and include cursors for further inspection.

The spool is capped at 16 MiB, each observation at 4 KiB, and each incident's
recent history at 100 entries. Overflow increments an observable durable dropped
count. Active fault/probe indexes are capped at 4,096 entries. Delivery receipts
and ingestion deduplication receipts retain seven days; resolved checkpoint
findings expire after seven days, while open findings keep their suppression.
An already initialized empty reporter consumes no periodic SQLite writer slot.

Management and System Dev use paginated compact lists. Incident evidence loads
on Inspect, not once per list row. Fleet separates runtime presence, work review
age and unresolved completion findings. UI components/styles/icons are reused.
No capacity guarantee for hundreds of concurrently finishing agents follows from
these bounds alone; full production load measurement remains a rollout check.

## Verification and adoption

Ordubashy runs on Codex or Claude Code. Every prepared workspace carries both
clients' hooks and instructions, so the owner starts whichever CLI they choose in
Ordubashy's own terminal, and management intake, delegated answers and the Focus
board support that session. Automatic durable-worker execution still requires
a Codex parent session. The profile `runtime` is only the
default: the CLI that terminal control starts and that Chat names first. Change it
with `agents_update` and `changes={"runtime":"codex"}` or `{"runtime":"claude-code"}`;
the agent identity and records stay the same in both directions.

Focused isolated coverage exercises authoritative migrations, management delivery
and delegated answers through real HTTP/MCP, policy/membership revocation, exact
inbox receipts, native no-loop durability, concurrent finding claims, task links,
verified closure, per-agent recovery, spool commit failure and fault storms.
The native fixture uses the installed CLI with only loopback fake providers.
The dashboard browser fixture opens canonical sources and incident detail,
checks blank closure evidence, and submits populated evidence with the canonical
ticket revision. Role policy regressions preserve protected approvals.

Rollout is a separate authorized release: back up using the existing release process,
apply the additive migrations (management routing, checkpoint metadata, incidents,
notice receipts), deploy matching core and dashboard, publish and adopt the intended
role pins preserving explicit overrides and scopes, then configure the exact Codex
Ordubashy through `management_coordinator_set` and run supported profile preparation.
Verify coordinator identity, role/runtime, current memberships, protocol version and
system reporter health by readback, and check one agent's management/current-work/inbox
workflow before broader adoption. A source merge alone is not evidence of adoption.

Observable technical failures are the detection boundary. Absence, idle terminals,
or a stale heartbeat cannot distinguish a native crash from normal exit, so this
release does not auto-file native-crash tickets. Failures before authentication,
host loss and simultaneous database/spool unavailability cannot guarantee durable
reports. No silent wrong-answer detector or automatic code-repair agent is added.


Focus-only completion mode: prepared Focus curators use protocol 2 when the exact
native executable has the required correction evidence. This mode supplies the
complete board once per original turn; it does not enable general work or inbox
corrections. See [Owner attention](OWNER-ATTENTION.md#full-board-completion-review).
