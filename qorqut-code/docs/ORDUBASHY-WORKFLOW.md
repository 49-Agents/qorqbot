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

### Day review

When the owner asks what was done, whether everything was done or whether anything was dropped, Ordubashy answers from two Global reads that only the installation owner and the configured Ordubashy in person may call (a worker and a delegated identity are refused), and that write nothing:

- `owner_inputs_list` returns the owner's exact messages, oldest first, for a period (`hours`, `since`/`until`) or the newest `last_n`: every agent's chat (`chat`), the private owner threads including side chats (`thread`) and the owner's own notes on Tasks, Tables, Signoffs and Focus cards (`note`). Each item carries `followed_by`: the reply that came after it and up to five record changes the addressed agent wrote before the owner's next message there. That is matched by time and place, not by meaning. `owner_input_get` returns one item in full.
- `work_review_get` returns what the records say was done in the same period: Tasks created, closed, reopened and handed to other Workers, Actions recorded, Signoffs proposed and decided, counts of owner-facing notes and replies per agent, and how much open work is unassigned, stale or waiting on the owner now. The list of open work stays `tasks_overview(view="holders")`. A section the caller cannot read is returned as `unavailable`; the others still answer.

Chat text has one copy, the transcript archive. `native_owner_turns` is an append-only index of where each person-typed turn starts; it holds no text. The transcript indexer fills it, so chat messages written before the table existed are not listed, and the read says so (`chat_covered_from`, and a `chat_not_indexed_before` gap). A turn is matched to the delivered owner input that typed it by a text digest kept on the input; an unmatched turn is `typed` (entered in the agent's terminal). A turn that may be a private input is indexed without its length and is returned without text. The table is added by the offline `python -m qorqut.migration.upgrade` transform `owner_turns`.

The procedure is the `Day review` section of `ordubashy-core`: read both, list every ask in the owner's quoted words, match each to a record and read it, sort into done, in progress, done differently, forgotten and waiting on the owner, create or reopen the Task for a forgotten ask, then reply with forgotten and changed items first. A short form, `owner_inputs_list(last_n=20)`, runs after a restart or compaction and before reporting a batch of requests as finished. Nothing runs on a schedule; the review happens when the owner asks.

### Unreachable workers

Inbox notices are typed into an agent's session. When a permanent worker has pending inbox items and every notice for at least 20 minutes was definitely not typed (its input box holds unsent text, its screen shows something else or a dialog, or it has no session), the backend pass sends the configured Ordubashy one `unreachable` inbox event for that worker, and again after six hours while it lasts (`qorqut/agents/unreachable.py`). A worker in a running turn is not reported. The event names the worker, since when, why and how many inbox items wait, and his core rules tell him to repair it himself with `agent_terminal_status` and `agent_terminal_restart`, then to reassign the worker's open Tasks and tell the owner if two restarts do not hold. The pass reads the nudge attempts; it touches no session and adds no table. Learning Applier is not a worker and is not reported here.

### Worker messages

A permanent worker's message to Ordubashy is at most 1,200 characters; a longer one is refused whole with where the detail belongs (the Task's result, a thread note or a linked file). Finished work is reported by closing the Task with its result, and progress on a longer goal is the Task's `status_line`; neither needs a message. Ordubashy writes no note to accept a closed Task.

## Recording on behalf of the owner

Task, Action and Table discussions, Signoff notes, private item conversations and agent messages accept attributed relays. `explicit_owner_statement` records a reported statement; `owner_intent` marks Ordubashy's interpretation; `agent_report` reports another registered agent. The actual writer remains recorded, and agent-message replies continue routing to the actual sender. Relayed owner text is right-aligned with agent colouring and a recorder label.

`actions_record` can record reported completed work with the real `performed_by` and an explicit statement or agent report. An interpretation does not establish completion. Ordubashy can correct or withdraw accessible Actions while preserving original attribution and immutable history. The Activity tab and manual Action-entry UI are removed; Action records and their linked readers remain.

`journal_entry_create`, `journal_entry_update` and `journal_entry_archive` let the configured coordinator maintain visible journal entries with owner attribution. Edits require current revisions and content hashes. Hidden entries, visibility controls and unrelated private material remain protected. Generic agent notes are a separate workspace.

Formal Signoff approvals and activation of standing instruction changes keep their existing approval process. A recorded preference or rule is not activation of an instruction change.

## Adoption and compatibility

Fresh Ordubashy profiles include `manage_ecosystem_agents`, which is required in addition to the role's tool grants to create and manage permanent workers. Existing installations must explicitly adopt the updated Agentic packages, role revision and profile capability; preserve their local instruction edits and policy overrides.

The attribution side table changes the composed schema. Existing databases require the explicit offline copy upgrade described in [CRM upgrade](CRM-UPGRADE.md); ordinary startup never alters a database. Frozen predecessor inventories cover the current public extension combinations. The upgrade retains old messages, records, Todo memberships, counters and files. Legacy messages display as authored by their original sender.

This source change does not deploy or alter a running installation.
