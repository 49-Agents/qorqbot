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

### Daily reconcile

Every registered agent is enrolled once, by default, in one daily repeating reminder (browser cleanup). For the configured Ordubashy that occurrence is also the daily reconcile: his core rules (`ordubashy-core`) tell him to do it before he dismisses the occurrence, and a newly enrolled Ordubashy gets a default reminder text that names it. A stored reminder is never rewritten, so an already enrolled Ordubashy keeps his text and takes the reconcile from the core rules. An occurrence stays due until it is dismissed; if the reminder was cancelled, the core rules ask for the reconcile on the first idle stretch of the day.

The reconcile has two parts. His own Todo: read every page, remove Done memberships while retaining the underlying records and history, and reconcile stale focus and intent. The owner's standing daily-cleanup request supplies the explicit owner authorization required for Done removal. Then all open work, from `tasks_overview(view="holders")`: Tasks marked stale, unassigned Tasks and Tasks changed since the last reconcile are checked, longest untouched first, and each record is made to say what is true (state, who must act next, closed only on evidence). It stops after about forty Tasks and continues the next day. Unfinished commitments, unanswered questions and private messages retain their existing protections.

This replaces the earlier daily Todo adjustment invitation, which was a one-shot chat nudge on the first idle pass. The reminder itself never changes Tasks or Todo memberships.

## Recording on behalf of the owner

Task, Action and Table discussions, Signoff notes, private item conversations and agent messages accept attributed relays. `explicit_owner_statement` records a reported statement; `owner_intent` marks Ordubashy's interpretation; `agent_report` reports another registered agent. The actual writer remains recorded, and agent-message replies continue routing to the actual sender. Relayed owner text is right-aligned with agent colouring and a recorder label.

`actions_record` can record reported completed work with the real `performed_by` and an explicit statement or agent report. An interpretation does not establish completion. Ordubashy can correct or withdraw accessible Actions while preserving original attribution and immutable history. The Activity tab and manual Action-entry UI are removed; Action records and their linked readers remain.

`journal_entry_create`, `journal_entry_update` and `journal_entry_archive` let the configured coordinator maintain visible journal entries with owner attribution. Edits require current revisions and content hashes. Hidden entries, visibility controls and unrelated private material remain protected. Generic agent notes are a separate workspace.

Formal Signoff approvals and activation of standing instruction changes keep their existing approval process. A recorded preference or rule is not activation of an instruction change.

## Adoption and compatibility

Fresh Ordubashy profiles include `manage_ecosystem_agents`, which is required in addition to the role's tool grants to create and manage permanent workers. Existing installations must explicitly adopt the updated Agentic packages, role revision and profile capability; preserve their local instruction edits and policy overrides.

The attribution side table changes the composed schema. Existing databases require the explicit offline copy upgrade described in [CRM upgrade](CRM-UPGRADE.md); ordinary startup never alters a database. Frozen predecessor inventories cover the current public extension combinations. The upgrade retains old messages, records, Todo memberships, counters and files. Legacy messages display as authored by their original sender.

This source change does not deploy or alter a running installation.
