# Runtime instructions

This is the authoritative Agentic package for native hook, startup, Attention, completion and recovery prose. The host reads this package through the managed catalog. Prepared local snapshots are generated copies for service outages, never a second authored source.

The consolidation preserves existing wording, triggers and delivery order. Conditional messages are selected by runtime and role. Legacy and retired entries are retained for inspection and compatibility; their presence does not activate them.

| Instruction | Delivery path | Status |
| --- | --- | --- |
| [activity-reminder](messages/activity-reminder.md) | activity hook | conditional |
| [attention-board](messages/attention-board.md) | attention prompt | conditional |
| [attention-board-transition](messages/attention-board-transition.md) | agents/attention.py | conditional |
| [attention-candidates](messages/attention-candidates.md) | attention prompt | conditional |
| [attention-continue](messages/attention-continue.md) | attention prompt | conditional |
| [attention-data](messages/attention-data.md) | attention prompt | conditional |
| [attention-detach](messages/attention-detach.md) | attention prompt | conditional |
| [attention-empty](messages/attention-empty.md) | attention prompt | conditional |
| [attention-focus](messages/attention-focus.md) | attention prompt | conditional |
| [attention-focus-transition](messages/attention-focus-transition.md) | agents/attention.py | conditional |
| [attention-more](messages/attention-more.md) | attention prompt | conditional |
| [attention-next-actor](messages/attention-next-actor.md) | agents/attention.py | conditional |
| [attention-unavailable](messages/attention-unavailable.md) | attention prompt | conditional |
| [background-recovered](messages/background-recovered.md) | activity context | conditional |
| [briefing-assignment-edges](messages/briefing-assignment-edges.md) | agents/briefing.py | conditional |
| [briefing-evidence](messages/briefing-evidence.md) | briefing | legacy |
| [briefing-evidence-review](messages/briefing-evidence-review.md) | agents/briefing.py | conditional |
| [briefing-excerpt](messages/briefing-excerpt.md) | agents/briefing.py | conditional |
| [briefing-grants](messages/briefing-grants.md) | agents/briefing.py | conditional |
| [briefing-neighborhood-limit](messages/briefing-neighborhood-limit.md) | agents/briefing.py | conditional |
| [briefing-omitted](messages/briefing-omitted.md) | briefing | legacy |
| [briefing-pagination](messages/briefing-pagination.md) | agents/briefing.py | conditional |
| [briefing-project-selection](messages/briefing-project-selection.md) | agents/briefing.py | conditional |
| [briefing-read-before-write](messages/briefing-read-before-write.md) | agents/briefing.py | conditional |
| [briefing-refresh](messages/briefing-refresh.md) | agents/briefing.py | conditional |
| [briefing-relationships](messages/briefing-relationships.md) | agents/briefing.py | conditional |
| [briefing-runtime-unavailable](messages/briefing-runtime-unavailable.md) | agents/resolver.py | conditional |
| [briefing-skill-discovery](messages/briefing-skill-discovery.md) | agents/briefing.py | conditional |
| [briefing-source-unavailable](messages/briefing-source-unavailable.md) | agents/briefing.py | conditional |
| [briefing-tools](messages/briefing-tools.md) | briefing | legacy |
| [briefing-truncated](messages/briefing-truncated.md) | briefing | legacy |
| [browser-worker-repair](messages/browser-worker-repair.md) | browser/access.py | conditional |
| [browser-worker-unconfigured](messages/browser-worker-unconfigured.md) | browser/access.py | conditional |
| [completion-finished-work](messages/completion-finished-work.md) | checkpoints | conditional |
| [completion-focus-retired](messages/completion-focus-retired.md) | checkpoints | retired |
| [completion-owner-inbox](messages/completion-owner-inbox.md) | checkpoints | conditional |
| [completion-records](messages/completion-records.md) | checkpoints | conditional |
| [completion-unavailable-work](messages/completion-unavailable-work.md) | checkpoints | conditional |
| [completion-wrapper](messages/completion-wrapper.md) | activity hook | conditional |
| [connection-uncertain-write](messages/connection-uncertain-write.md) | bootstrap/mcp_reconnect.py | conditional |
| [delegated-worker-continue](messages/delegated-worker-continue.md) | bootstrap/delegated_runtime.py | conditional |
| [delegated-worker-rules](messages/delegated-worker-rules.md) | bootstrap/delegated_runtime.py | conditional |
| [delegated-worker-start](messages/delegated-worker-start.md) | bootstrap/delegated_runtime.py | conditional |
| [environment-guidance](messages/environment-guidance.md) | environment | conditional |
| [environment-legacy](messages/environment-legacy.md) | environment | legacy |
| [environment-legacy-omitted](messages/environment-legacy-omitted.md) | environment | legacy |
| [environment-omitted](messages/environment-omitted.md) | environment | conditional |
| [environment-truncated](messages/environment-truncated.md) | environment | conditional |
| [environment-unavailable](messages/environment-unavailable.md) | environment | conditional |
| [inbox-nudge](messages/inbox-nudge.md) | inbox_nudges/contracts.py | conditional |
| [initial-learning-assignment](messages/initial-learning-assignment.md) | bootstrap/initialize.py | conditional |
| [initial-ordubashy-assignment](messages/initial-ordubashy-assignment.md) | bootstrap/initialize.py | conditional |
| [learning-applier-assignment](messages/learning-applier-assignment.md) | bootstrap/learning_applier.py | conditional |
| [memory-legacy-plan](messages/memory-legacy-plan.md) | working memory | conditional |
| [memory-omitted](messages/memory-omitted.md) | working memory | conditional |
| [memory-pending](messages/memory-pending.md) | working memory | conditional |
| [overseer-extraction-due](messages/overseer-extraction-due.md) | overseer/due.py | conditional |
| [plugin-discovery](messages/plugin-discovery.md) | agents/preferences.py | conditional |
| [project-briefing-evidence](messages/project-briefing-evidence.md) | domains/business_briefing.py | conditional |
| [retirement-inbox-history](messages/retirement-inbox-history.md) | inbox/retirement_contract.py | conditional |
| [retirement-inbox-reading](messages/retirement-inbox-reading.md) | inbox/retirement_contract.py | conditional |
| [retirement-review-history](messages/retirement-review-history.md) | inbox/retirement_contract.py | conditional |
| [runtime-diagnostic-guidance](messages/runtime-diagnostic-guidance.md) | agents/diagnostics.py | conditional |
| [skills-header](messages/skills-header.md) | always skills | conditional |
| [skills-omitted](messages/skills-omitted.md) | always skills | conditional |
| [startup-denied](messages/startup-denied.md) | claude session | conditional |
| [startup-fault](messages/startup-fault.md) | claude session | conditional |
| [startup-inventory](messages/startup-inventory.md) | claude session | conditional |
| [startup-pending](messages/startup-pending.md) | claude session | conditional |
| [startup-recovered](messages/startup-recovered.md) | claude session | conditional |
| [startup-repair](messages/startup-repair.md) | claude session | conditional |
| [startup-session](messages/startup-session.md) | claude session | conditional |
| [startup-unavailable](messages/startup-unavailable.md) | claude session | conditional |
| [tool-validation-recovery](messages/tool-validation-recovery.md) | transport/mcp.py | conditional |
| [workspace-claude](messages/workspace-claude.md) | trusted workspace preparation | conditional |
| [workspace-code-roots](messages/workspace-code-roots.md) | trusted workspace preparation | conditional |
| [workspace-codex](messages/workspace-codex.md) | trusted workspace preparation | conditional |
| [workspace-completion](messages/workspace-completion.md) | trusted workspace preparation | conditional |
| [workspace-delegation-summary](messages/workspace-delegation-summary.md) | trusted workspace preparation | conditional |
| [workspace-global-coordination](messages/workspace-global-coordination.md) | trusted workspace preparation | conditional |
| [workspace-identity](messages/workspace-identity.md) | trusted workspace preparation | conditional |
| [workspace-inbox](messages/workspace-inbox.md) | trusted workspace preparation | conditional |
| [workspace-native-subagents](messages/workspace-native-subagents.md) | trusted workspace preparation | conditional |
| [workspace-no-substitutes](messages/workspace-no-substitutes.md) | trusted workspace preparation | conditional |
| [workspace-team-browser](messages/workspace-team-browser.md) | trusted workspace preparation | conditional |
| [workspace-team-delegation](messages/workspace-team-delegation.md) | trusted workspace preparation | conditional |
| [workspace-team-queue](messages/workspace-team-queue.md) | trusted workspace preparation | conditional |
| [workspace-team-unavailable](messages/workspace-team-unavailable.md) | trusted workspace preparation | conditional |
| [workspace-worker-browser](messages/workspace-worker-browser.md) | trusted workspace preparation | conditional |
| [workspace-worker-delegation](messages/workspace-worker-delegation.md) | trusted workspace preparation | conditional |
| [workspace-worker-queue](messages/workspace-worker-queue.md) | trusted workspace preparation | conditional |
| [workspace-worker-unavailable](messages/workspace-worker-unavailable.md) | trusted workspace preparation | conditional |
