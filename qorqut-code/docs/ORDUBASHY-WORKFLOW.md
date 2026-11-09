# Ordubashy work cycle

Ordubashy coordinates work, owner attention, operational records, useful context and permanent agents. For each new input or result he first reconciles the relevant records and threads, updates Todo, then delegates independent execution or continues work that needs direct owner input. Useful context goes to the context filer. Multiple incoming requests remain recorded while earlier work continues.

The canonical rules are the Agentic runtime instruction `ordubashy-core`. Topic skills describe the tools and retained boundaries. Generated workspace documents begin with these rules. Startup composes core and topic instructions before identity, assignments and contextual data. The existing prompt hook adds `operational-intake` once per prompt; its delivery does not depend on whether the skills changed. No extra monitoring agent or polling loop is added.

## Todo and Focus

Todo is the existing persistent Attention list under its current name. `todo_get`, `todo_add` and `todo_remove` use the same memberships, records and revision checks as their retained `attention_*` counterparts. Both names respect existing operation policy. Tasks, Stories, Customers and existing email Tasks can be listed. Other operational records are linked to their concrete next-step Task rather than copied into another record system.

The same list shows work for the owner and agents, with Everyone, You and Agents filters based on the current Worker set. Delegation does not remove the coordinator's commitment. Done and blocked records retain the existing removal protections. Focus keeps its existing curated-priorities purpose and tools; presenting a reference does not automatically add a Focus card.

### Shared Todo and focus reminders

Ordubashy and enrolled permanent workers use the same reminder clock. An unfocused
agent with actionable board tasks is nudged after three minutes without activity;
a board containing only blocked, waiting or needs-input tasks is reassessed every
30 minutes. Empty and Done/Cancelled-only boards schedule no ordinary board nudge.

A focused agent receives a progress reminder through its next eligible hook every
five minutes. If no hook delivers it, the existing chat fallback becomes eligible
three minutes after that deadline, including when the connected agent is idle.
It does not start a stopped client or resume a paused session. The 15-minute buffer
after an owner message still applies to chat delivery, not the focused hook.
Existing persisted deadlines are retained until their normal clock transition or
delivery; installing a new interval does not replay missed reminders.

### Daily Todo adjustment

The existing inactivity polling pass invites the configured Ordubashy to run a careful Todo list adjustment at most once per elapsed 24 hours. The first eligible pass sends the invitation; it is not a calendar-day schedule and missed days do not accumulate. Eligibility requires a live, idle native session, at least 15 minutes since session observation and agent activity, and at least 15 minutes since the last owner message. Paused or stopped sessions, pending native input and revoked session authority prevent delivery. A stale Focus does not prevent housekeeping when the session is actually idle. Done-only lists qualify; empty lists do not.

The authored `todo-adjustment` instruction asks Ordubashy to read every page, remove Done memberships while retaining the underlying records and history, reconcile states, Workers and stale focus, and update the appropriate threads with meaningful progress. The owner's standing daily-cleanup request supplies the explicit owner authorization required for Done removal. Unfinished commitments, unanswered questions and private messages retain their existing protections. The invitation itself never changes Tasks or Todo memberships.

Native input receipts preserve the interval across host restarts and session/provider changes without a schema change. A known-not-typed attempt can retry with its original text; an uncertain delivery consumes the interval conservatively and is not replayed. After 24 hours, the next eligible invitation is a new occurrence, independent of any earlier uncertain receipt. The daily invitation takes precedence over an ordinary Attention nudge and suppresses another ordinary chat reminder for 15 minutes. It uses the existing idle-only input arbiter and never launches or resumes an agent.

Existing installations need both the host change and adoption of the Agentic instruction. Until `todo-adjustment` is available in the active instruction catalog, ordinary nudges continue without the daily invitation.

## Recording on behalf of the owner

Task, Action and Table discussions, Signoff notes, private item conversations and agent messages accept attributed relays. `explicit_owner_statement` records a reported statement; `owner_intent` marks Ordubashy's interpretation; `agent_report` reports another registered agent. The actual writer remains recorded, and agent-message replies continue routing to the actual sender. Relayed owner text is right-aligned with agent colouring and a recorder label.

`actions_record` can record reported completed work with the real `performed_by` and an explicit statement or agent report. An interpretation does not establish completion. Ordubashy can correct or withdraw accessible Actions while preserving original attribution and immutable history. The Activity tab and manual Action-entry UI are removed; Action records and their linked readers remain.

`journal_entry_create`, `journal_entry_update` and `journal_entry_archive` let the configured coordinator maintain visible journal entries with owner attribution. Edits require current revisions and content hashes. Hidden entries, visibility controls and unrelated private material remain protected. Generic agent notes are a separate workspace.

Formal Signoff approvals and activation of standing instruction changes keep their existing approval process. A recorded preference or rule is not activation of an instruction change.

## Adoption and compatibility

Fresh Ordubashy profiles include `manage_ecosystem_agents`, which is required in addition to the role's tool grants to create and manage permanent workers. Existing installations must explicitly adopt the updated Agentic packages, role revision and profile capability; preserve their local instruction edits and policy overrides.

The attribution side table changes the composed schema. Existing databases require the explicit offline copy upgrade described in [CRM upgrade](CRM-UPGRADE.md); ordinary startup never alters a database. Frozen predecessor inventories cover the current public extension combinations. The upgrade retains old messages, records, Todo memberships, counters and files. Legacy messages display as authored by their original sender.

This source change does not deploy or alter a running installation.
