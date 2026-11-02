# Agent Platform Decisions

These decisions describe the application. Executable contracts and tests define
implemented behavior; historical implementation plans do not override current policy.

## Owner decisions

| Area | Decision |
| --- | --- |
| Scope | The Qorqut application and its project workspaces. Personal/global Claude configuration is outside the application. |
| Agent runtime | Agents are Claude Code or Codex sessions in mirrored terminals. Preserve the existing terminal/tmux experience and the ability to start plain `claude` in a prepared agent folder. |
| Background work | DeepSeek is for background routines, learning extraction, filing, and related processing. It is never an alternative business-agent runtime. |
| Deprecated harness | `biz-harness` is retired and absent from the active checkout. Preserve its archived source/history; no active runtime may depend on it. |
| Shared service | One shared MCP service for all agents and plugins, on the Qorqut host or reached over Tailscale. No MCP process per agent or per plugin. |
| Human interface | Humans use the dashboard or talk to agents. There is no requirement to retain human-facing business CLIs. |
| Code reuse | Evaluate existing behavior on its merits. A larger refactor is acceptable. Permanent subprocess wrappers are not a required architecture. |
| Roles | A role defines the job: general worker, Ordubashy (the coordinator) or Learning Applier. A role is not a plugin. |
| Plugins | A plugin is an optional package of abilities, such as scoped email, with relevant tools, knowledge, and rules. Agents with the same role can have different plugins or none. |
| Skills | A skill is a procedure belonging to a role or plugin. Detailed optional procedures load when relevant. |
| Agent configuration | One owner-controlled profile resolves identity, role, scope, enabled abilities, context selection, and operation permissions. |
| Mandatory context | Explicitly supply a short foundation, role brief, and core business orientation. Do not rely on an optional skill invocation for identity or essential operating rules. |
| Ordubashy | It has broader organizational authority and its own role context. Do not make it inherit business-only restrictions and then contradict them. Configured signoffs still apply. |
| Extended context | Preserve the bounded context library, uploaded materials, filing, summaries and document reads. Semantic retrieval and a separate search infrastructure remain deferred. |
| Team visibility | Agents can see their own business's roster, roles, abilities, and teammates' tasks, plans, and notes. |
| Tasks | Preserve task management behavior. Agents can create and assign tasks to themselves, teammates, or the owner. Assignments queue work; they do not wake agents or confer additional permissions. |
| Permissions | Resolve an effective setting for every agent-operation pair: `allow`, `signoff`, or `deny`. Use role defaults and individual overrides. |
| Automatic defaults | Task creation, assignment, comments, completion, and ordinary working notes can execute automatically within the agent's permitted business and write scope. |
| Signoff defaults | Hiring/retiring agents, changing permissions, shared instructions, or business configuration requires owner signoff. |
| Signoff execution | The owner approves a prepared change. The service automatically applies that exact change without waiting for the agent. |
| Review UI | Signoffs uses the existing right-side pop-out panel alongside Notes. Show the selected business's queue, or Global's own queue. No separate modal or aggregate business inbox. |
| Review interaction | Focus/select a proposal to reveal an inline feedback area. No editing of the proposal in this UI. Approve discards draft feedback; Reject saves feedback for the agent to read. |
| Signoff records | Persist proposals, outcomes, and rejection feedback as needed for correctness and agent access. Do not create business journal/bizlog entries for the signoff lifecycle. No separate audit-log product is requested. |
| Storage | Reconsider operational storage. Git should provide versioning, potentially nightly commits, rather than define whether an operation happened. |
| Isolation | Enforce identity, business scope, tool permissions, and signoffs in the application. Strong filesystem isolation against a same-user agent is not required. Do not claim a sandbox boundary that is not implemented. |

## Engineering choices for this design

These are reasoned implementation defaults, not additional statements attributed
to the owner. Change them through a documented design amendment when evidence
warrants it; do not create new user approval prompts for routine implementation
details already within the authorized work.

1. Use a single Python backend package, initially named `biz-core` with import
   package `bazarlyk`. Host MCP, the dashboard's existing HTTP/static adapters,
   and background-job integration in one supervised application. Preserve
   current URLs and terminal behavior as adapters change.
2. Use one local SQLite database in the instance's `.bazarlyk` storage directory
   for operational records. Agents and remote dashboards access it through the
   application; never share the database file over Tailscale or a network mount.
3. Keep authored context, role/plugin procedures, code, and media as files.
   Operational profiles and permission grants are database-owned. Generated
   `agent.yaml`, Claude settings, exports, and context projections are not
   independent writable sources of truth.
4. Give businesses and agents stable internal IDs independent of display names
   and paths. Preserve existing external request/task IDs during import; do not
   break their references merely to standardize identifiers.
5. Use explicit operation inputs and caller context. No process-global working
   directory or environment mutations to select the acting business in a
   multi-client server. Extract useful validation and domain behavior from the
   existing utilities; CLI parsing, printing, and subprocess chains are not the
   shared application interface.
6. Use a maintained MCP SDK and structured parsers. Introduce dependencies when
   they remove meaningful protocol or parsing complexity. Avoid a new general
   policy language, a custom plugin package manager, microservices, or a second
   database representing the same operational records.
7. Rejection feedback may be empty. When text is entered, preserve it verbatim
   on the rejected proposal. Approval endpoints do not persist draft feedback.
8. The server routes new protected changes by target ownership: shared/global
   proposals use Global Signoffs, and business proposals use that business's
   queue. Review routing grants no authority. Clearly display affected scope;
   never duplicate a proposal into every business. Older prepared proposals
   retain their original queue and exact payload.
9. Role/plugin instruction versions are published immutable file revisions
    referenced by the effective profile. Use simple manifests and hashes rather
    than an update-distribution framework. Scope includes this local deployment,
    not remote plugin-cache propagation.
10. Root-agent administration uses the explicit global resource capability
    `manage_ecosystem_agents`, alongside the applicable operation permissions.
    A review-business selector or proposed role never grants global scope.

## Boundaries that implementations must preserve

- Role responsibility, optional capability, permission mode, and business scope
  are distinct. Enabling a plugin does not grant another business's access.
- Ordubashy's broad visibility does not imply every optional plugin is loaded
  or every mutation is automatically approved.
- Task assignment conveys work, not authority. A task body cannot change a role,
  install a plugin, waive signoff, or impersonate the owner.
- A proposal is a validated, immutable intended operation with a concrete
  preview and expected target revisions. It is not free-form instructions for
  an agent to interpret after approval.
- Approval grants only the reviewed change. Modified targets, changed payloads,
  or a revoked permission require the appropriate conflict/denial handling.
- Internal signoff outcomes must not be added to the business journal, action
  log, or activity toasts merely because a decision occurred. Existing logging
  of substantive business actions is a separate policy.
- Rejected feedback is available through normal agent tools and startup/queue
  orientation. No notification mechanism starts an agent or injects a new user
  message into its terminal.
- Database commits acknowledge operational durability. Nightly Git exports and
  database backups have separate responsibilities; a failed Git push cannot
  undo an already applied database operation.
- File writes and database transactions are not atomic together. Approved file
  operations need a small durable recovery mechanism and hash checks, specified
  in the core and signoff contracts.
- New operational stores get one active writer path. A legacy file importer and
  a live legacy writer must not remain competing authorities after cutover.
- No currently running agent, routine, bridge, or filer is stopped or changed
  during the specification phase.

## Contract ownership

The current application contracts are in [domain APIs](../../DOMAIN-APIS.md) and
[v1.2 contracts](../v1.2/CONTRACTS.md). Resolve cross-document conflicts explicitly; do
not invent a local interpretation that bypasses the common contracts.
