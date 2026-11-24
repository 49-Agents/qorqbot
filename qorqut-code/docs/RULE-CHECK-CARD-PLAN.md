# Rule check card: plan

Status: **built on the local branch `feature/rule-check-card`, not pushed, not merged, not deployed** (2026-11-24; see the
work log at the end). The design was settled with the owner on 2026-11-23 and 2026-11-24 from a mockup made with the
dashboard's own stylesheets and Signoff markup. An item without a "Verified" line is not done.

## Why

Every agent files rules and corrections the owner gives in chat into the Learning Applier's queue with
`experience_observation_file` (see [EXPERIENCE-WORKFLOWS.md](EXPERIENCE-WORKFLOWS.md)). The agent writes the rule in its
own words, and those words can drift from what the owner said: an agent can add an exception or a condition the owner
never gave. The owner wants one quick look at the wording while the moment is fresh, without approving anything in chat.

The card is a check of the wording. It is not an approval to change an instruction: that stays the Learning Applier's
later step, with its own delta proposal.

## What the owner sees

1. **A chip on the attention queue in Focus**, with an unread dot and a graduation cap icon (the Learning Applier's
   mark). A card shown in chat does not count as seen.
2. **The card**, a normal Approve/Reject Signoff with the same footer and styling as an email Signoff:
   - a short line: which agent filed the rule, that it is already in the Learning Applier's queue, and that no
     instruction changes until the weekly review;
   - the rule as filed, in a text box the owner can type into, the same way an email body is edited;
   - who the rule applies to;
   - a note box, **Reject** and **Approve**. A note makes Reject the main button, as on an email.
3. The owner's original message is not shown on the card (owner's choice, 2026-11-24).

## What each answer does

| Answer | Effect |
| --- | --- |
| Approve | The rule is marked approved by the owner in the queue. If the owner edited the text, the edited text replaces the agent's wording. |
| Reject | The rule is removed from the queue. The owner's note, if any, goes back to the agent that filed it. |
| No answer | The rule stays in the queue, marked as not checked. |

No answer changes an instruction, a skill or a tool.

## What gets a card

Only a filing whose kind says the owner said it (`owner_complaint`: a rule, a correction or a complaint from the owner).
A lesson from an agent's own mistake, a tool problem and a tool pattern are filed as today, with no card.

## Build

### 1. Server

- A mark on a filed observation: not checked, approved by the owner. A rejected one is deleted. This is a new column on
  `experience_observations`, so it needs a transform in `migration/upgrade.py`, a classification in
  `operations_history/catalog.py` and the matching change in `test_migration_upgrade.py`.
- The filing writes the observation at once, as today, and in the same transaction creates the check as an operation
  Signoff of a new internal operation (working name `experience_rule_check`): `supports_signoff`, held for the owner by
  default, `editable_fields=("text",)` with a `revise` step, the same mechanism the WhatsApp and email sending
  operations use for edit-in-place.
- Applying the approved operation sets the mark and stores the text as approved. A rejection is followed through the
  Signoff status observer (`SignoffDomains.observe`, as `signoff_updated` does for delta proposals): it deletes the
  observation and passes the owner's note to the filing agent.
- The filing tool's result tells the agent that the owner was asked to check the wording, so it does not ask in chat.
- A filing refused as a repeat stays refused; an observation already linked to a delta proposal cannot be deleted by a
  late rejection (the rejection then marks it instead; to settle in this step).

### 2. Dashboard

- A Signoff renderer for the operation (`registerSignoffRenderer`): the rule text as the one editable field, the
  "applies to" row, the notice line.
- The graduation cap in `attention-queue.js` (`signoffIcon`) for this operation.
- The mark on the Learning Applier's Observations page (`overseer.js`).
- The compact card in chat opens the full card in Focus, as other Signoffs do.

### 3. Learning Applier

- `experience_cluster_get` and the weekly review's reading of the standing cluster show the mark, so the Learning
  Applier can tell an owner-approved rule from one the owner has not looked at.
- It still does not run when something is filed.

### 4. Instruction texts

- The filing tool's description and Ordubashy's brief: a rule from the owner produces a card for the owner; do not also
  ask the owner in chat, and follow the rule from then on.
- Role versions raised for every role whose text changes. Rehearse the instruction update `plan` on a copy before any
  deploy.

### 5. Tests and checks

- Server: a card for an owner filing and none for the other kinds; approve; approve after an edit; reject with and
  without a note; no answer; a repeat filing; the upgrade transform.
- Dashboard: the renderer, the icon, the footer and the edit flow.
- A real browser check on a fresh instance: an agent files a rule, the chip appears, edit and approve one, reject
  another, and read the Observations page afterwards.
- The secret scanner on the branch before it is pushed.

## Choices made while planning (not rulings of the owner)

- Reject deletes the rule outright; no "rejected" record is kept.
- An edited rule replaces the agent's wording; the agent's original wording is not kept.
- One card per rule; no grouping when several arrive close together.
- Rules that an owner accepted through the retired per-instruction Signoff and that still wait in the old application
  queue are moved into the Learning Applier's queue as approved, as a one-off at deploy time.

## Needs the owner's word

- The pull request and the merge into `dev`.
- The deploy to a live instance.

## Work log (2026-11-24)

Done, as local commits on `feature/rule-check-card` (dev merged in first):

1. Server. `experience_rule_check` in `experience/operations.py`; the filing stages it in its own transaction for
   `owner_complaint` and returns `owner_check` and a note that the owner was asked. Three nullable columns on
   `experience_observations` (`owner_check`, `check_signoff_id`, `checked_at`) and the table made editable, added to an
   existing store by the first transform of the ordered upgrade, `migration/rule_checks.py`. A rejection deletes the
   rule through the Signoff status observer; `settle_checks` on the inbox reconcile pass follows a rejection or an edit
   the observer missed. `experience_cluster_get` shows the mark. `operations_history/catalog.py` needed no change: the
   whole table is already excluded from history.
   Verified: `test_experience.py` (30 tests, 11 new in `RuleCheckTests`), `test_migration_upgrade.py` (9),
   `test_compaction_handoff.py` (12), `test_experience_runs.py`, `test_timeline.py`, `test_timeline_review.py`,
   `test_signoff_sources.py`, `test_signoff_acknowledge.py`, `test_browser_action_signoff.py`, `test_focus_exposure.py`,
   `test_agent_creation_signoff.py`, `test_task_closure_actions.py`, `test_tool_call_recovery.py`,
   `test_viewbox_tool_retired.py`, `test_mcp_phase_timing.py`, `test_mcp_discovery_cache.py`,
   `test_ordubashy_owner_parity.py`, `test_agent_terminal_tools.py`, `test_history_unchanged_tables.py`: all pass.
2. Dashboard. Renderer in `signoffs.js` (notice line, "Rule as filed" as the one editable field, "Applies to", the
   standard footer), the graduation cap in `attention-queue.js`, the mark on the Observations page in `overseer.js`. The
   chat card stays compact (the renderer has no `message` flag) and opens in Focus.
   Verified: `dashboard/tests/signoff-rule-check.test.js` (5 tests, three in headless Chrome on the fixture
   `fixtures/signoff-rule-check.html`), `signoff-attention-pill`, `timeline-filing-review`, `signoff-browser-action`,
   `signoff-focus-layout`, `signoff-chat-reference`, `signoff-chip-click`: all pass.
3. Learning Applier. `experience_cluster_get` description and the `delta-proposals` skill explain `owner_check`.
4. Instruction texts. The filing tool's description, Ordubashy's brief and core rules, the global worker's brief,
   `agentic-management` bootstrap and manage skill, `record-direct-instruction.md`, the `delta-proposals` skill and
   `EXPERIENCE-WORKFLOWS.md`. Role versions: Ordubashy 1.89.0, global worker 1.16.0, Learning Applier 1.27.0.
   Verified: `test_agentic_change_tools_retired.py`, `test_claude_startup_context.py`,
   `test_instruction_read_performance.py`, `test_ordubashy_any_runtime.py`, `test_qorqut_rename.py`: all pass.
5. Secret scanner: gitleaks 8.30.1 over `origin/dev..HEAD`, no leaks found.

Settled in this step:

- A rejection of a rule that a delta proposal already names marks it `rejected` and keeps it, so the proposal's link
  stays whole; any other rejected rule is deleted. The compaction handoff no longer lists a rule marked `rejected`.
- The owner may reject this card without a note, as the plan's table says ("the owner's note, if any"). The engine
  otherwise asks for a note on every rejection, so the operation descriptor gained `rejection_note_optional`, read at the
  three places that decide `rejection_requires_message` and at the rejection itself.
- The rule check is always held for the owner: no role file and no override can allow or deny it
  (`composition/policy.py`). It is offered to no agent in discovery and refused over agent MCP
  (`HOST_STAGED_OPERATIONS`); a direct call is also refused by its own prepare step. The engine still needs `mcp` in its
  exposure, because an approved Signoff applies under the proposing agent's context.
- If the policy in force does not hold the check, the filing of an owner rule is refused as a whole (nothing filed),
  as `timeline_file` does when it cannot stage its Signoff.
- A delegated worker's binding is left off the card's proposer context, as every proposal's proposer is rebuilt without
  one at approval; the card changes only the rule that agent itself just filed.
- The rule text on the card is 1 to 2000 characters after trimming, as at filing.
- Like the other experience operations, the new name is registered directly and not declared in `composition/catalog.py`.

Not done:

- The one-off move of old accepted rules at deploy time (skipped by instruction).
- The real browser check on a fresh instance (skipped by instruction; the fixture browser test above stands in).
- Rehearsing the instruction update `plan` on a copy of the live store, and the upgrade step on a copy: both belong to a
  deploy. A store opened by this build must first take the `rule_checks` step of the ordered upgrade.
- An owner edit adds the engine's own note "Edited the proposed message. Approval is still required." to the card; the
  word "message" is the engine's shared text and was left as it is.

