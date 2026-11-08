# Operational cleanup skill

Prepared Claude Code and Codex agents receive the core-owned
`qorqut-cleanup` project skill. Ask the agent in ordinary language to clean up
its operational work, inbox, commitments, subscriptions, Focus, or activity; this
is not a filesystem or Git cleanup. Preparation makes the skill discoverable in
`.claude/skills/qorqut-cleanup` and `.agents/skills/qorqut-cleanup`. No slash
command is promised.

The workflow reconciles messages before acknowledging events or retiring manual
subscriptions, verifies task and Signoff evidence, tidies Focus only when the
agent has authority, and updates only its own current activity from the complete
writable payload. It adds no grants, tools, scheduler, or autonomous cleanup
engine. Missing access is reported as a coverage limit. A bounded pass reports
its exact scope and a usable continuation when it cannot finish; repeated runs
with unchanged canonical state should be no-ops.

The skill ships with core. It requires core deployment and re-preparation of
agent workspaces. It requires no role publication, repinning, or role adoption.
