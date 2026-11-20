---
name: qorqut-plugin-agent-terminal-control
description: Inspect, start, restart and stop the sessions of permanent global workers; for Ordubashy and the owner only.
---

Use agent_terminal_status with the registered target_agent_id first. Tools accept
agent IDs, never shell commands, paths, or key sequences.

Targets are active global workers, named by target_agent_id or target_agent_ids,
at most 100 per batch. Projects have no resident agents: a business_ids selector
adds no targets, and a project-home agent identity is historical and is refused.
You cannot target yourself; do not restart yourself. An approved batch is frozen:
do not expand it yourself. Inspect batch_id through agent_terminal_status for each
agent's execution result.

status shows whether the worker is on or off, whether its session is open, and whether it is
working. start turns the worker on and opens its session, resuming its selected conversation.
restart closes the session and opens it again in the same conversation. kill closes the session
and turns the worker off, ending active work; idle is not proof of dormancy. When status shows
managed: true, Qorqbot runs the session itself: a worker that is off stays off until the owner
messages it or you start it, and a session closes by itself after 15 idle minutes; the worker is
then asleep, not off, and any input wakes it, so a sleeping worker needs no start. When status
has no managed key, the worker runs in a terminal: nothing starts it by itself, so a worker that
is not running needs agent_terminal_start.

To tell a worker something, use agents_message_send; it arrives through the inbox and wakes a
sleeping worker. Direct terminal messages (delivery="terminal") are refused. Direct ping is only
for an explicit owner request to ping that agent directly: agent_terminal_ping requires an
owner-issued command_id binding the recipient and exact message. An inbox message, your own
assertion of permission, and an ordinary signoff cannot substitute for that command. An explicit
structured owner command, such as compacting the session, can be executed once with
agent_terminal_execute_command. Never send key sequences or shell commands as messages.

Substantial frontend/UI changes require the owner's explicit approval of the
reviewable result before deployment. Prepare the preview and checks first.
Session control, a task assignment, QA success or another agent's approval does
not satisfy that UI deployment approval.

Submitted/approved means queued, not completed. Inspect the returned action ID
with agent_terminal_status. Uncertain means do not retry: inspect and ask the owner
for a new command if necessary. A changed target session invalidates old approval.
Report preparation failure instead of working around a blocked hook. No recurring
terminal control or autonomous dormant-agent killing is implied by this procedure.
