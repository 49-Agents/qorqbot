# Credential discovery and capability handoffs

`environment_list` lists the selected workspace's effective Settings variable
names, `configured` (nonempty) status, and `origin` (`global` or `business`).
An optional `names` array checks up to 100 specific bindings; absent names return
`configured: false, origin: null`. Empty values retain their origin. Business
overrides, including empty overrides, take precedence over Global defaults.
Global reads include only Global settings. Identity variables are excluded.
Use `limit`/`cursor` and follow `next_cursor` before concluding a name is absent.

The operation returns no values, hashes, lengths, or previews of secrets. A
configured value is not proof of valid credentials, provider readiness, or an
agent's authority to use a service. File errors remain errors, not empty lists.
Global access requires `read_global_context`; selected businesses retain normal
membership and agent-scope checks. Core enables metadata discovery by default,
including existing pinned roles, while explicit role/profile denial wins.

Supported operations already accept variable-name references: a daily JSON
metrics source uses `endpoint_env` and `token_env`; the existing worker resolves
values from Settings at execution. Discovery adds no secret-reading or writing
operation and no arbitrary-script runtime. Owners continue managing values in
Settings.

## Handoffs to the human owner

Every agent's common startup orientation explains the capability-gap workflow:
check its tools and suitable teammates, then create or update an ordinary task
assigned to the human owner if no available agent can perform the work.
`assignees_list` includes `is_owner` so agents can distinguish the human owner
from agent assignees. Existing task policy and signoff requirements apply.
The task stays in the originating business and appears in the human's personal
taskbox. Agents without task access or a business destination report the limitation to
the owner or an authorized coordinator rather than guessing an assignee/scope.

Include the requested outcome, specific blocker, attempts, and completion
criteria. Link existing blocked work with `blocked_by`; update an existing
handoff instead of duplicating it. Reference prepared notes by document ID and
scripts by persistent workspace path, or repository path and commit. Preserve
temporary scripts in the agent's own scratchpad before handing them off. Include
run instructions, dependencies, tests and relevant limitations in the note.
Credentials stay in Settings, referenced only by name. References neither run
code nor grant additional access; local paths require access to the host and
are not automatically exposed as dashboard downloads. Existing note access and
task text are sufficient; no new attachment store is introduced.

## Adoption

Deploy the reviewed core change, then refresh agent configuration through the
normal bootstrap/`agent_config_get` path. Reconnect the MCP client if it caches
the tool list. No role publication, migration or existing task mutation is
needed. Active conversations need refreshed instructions before this guidance
can affect their behavior. Deployment is a separate owner-authorized step.
