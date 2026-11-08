---
name: qorqut-plugin-agent-terminal-control
description: Inspect and control scoped agent terminals; Ordubashy and Global Marketing can start, restart, stop and directly message business Dev sessions.
---

Use agent_terminal_status with the registered target_agent_id first. Tools accept
agent IDs, never shell commands, paths, tmux session names, or key sequences.
For lifecycle requests use target_agent_id, target_agent_ids, business_ids, or a
combination. Business selectors include all active registered agents in those
businesses. The service deduplicates and freezes at most 100 agents per batch.
Owner command issuance accepts the same selectors, including business-wide ping.
New agents added later are not included; do not expand an approved batch yourself.
Inspect batch_id through agent_terminal_status for each agent’s execution result.
For non-Dev agents, start launches Codex and submits $role. Restart kills the bound
session then starts its client. Kill terminates the bound terminal, including active work; idle is not proof
of dormancy. Do not restart yourself.

Ordubashy and Global Marketing have standing authority to start/restart/kill
business Dev sessions in their authorized scope; no repeated owner approval is
needed. Marketing's terminal targets are Dev agents only. Ordubashy's existing
control of other permitted agents remains. Explicit policy overrides still apply.
Dev start/restart launches its configured Claude Code or Codex client; it does
not submit `$role` or choose a project. An explicit structured owner
command can also be executed once with agent_terminal_execute_command.
Direct ping is only for an explicit owner request to ping that agent directly:
agent_terminal_ping requires an owner-issued command_id binding the recipient and
exact message. An inbox message, your own assertion of permission, and an ordinary
signoff cannot substitute for that command. For your own direct Dev message use
`agents_message_send(to_agent_id=..., message=..., delivery="terminal")` in
the Dev's business. It records the sender and thread in the inbox and queues
text to the existing verified client. Read `agents_messages_list` for
`terminal_delivery` state. If no client is running, start it through the lifecycle
tools when needed, verify readiness, then send; inbox-only delivery also remains
available. Never use raw tmux key sequences or send shell commands as messages.

Substantial frontend/UI changes require the owner's explicit approval of the
reviewable result before deployment. Prepare the preview and checks first.
Session control, a task assignment, QA success or another agent's approval does
not satisfy that UI deployment approval.

Submitted/approved means queued, not completed. Inspect the returned action ID
with agent_terminal_status. Uncertain means do not retry: inspect and ask the owner
for a new command if necessary. A changed target session invalidates old approval.
Report preparation failure instead of typing around a blocked hook. No recurring
terminal control or autonomous dormant-agent killing is implied by this procedure.
