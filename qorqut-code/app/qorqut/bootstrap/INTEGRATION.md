> **v1.2 reader note:** this file retains earlier bootstrap integration evidence. Current installation paths and lifecycle come from [the operating guide](../../../docs/v1.2/OPERATING_GUIDE.md); current inbox delivery from [Inbox](../../../docs/AGENT-INBOX.md). Old acceptance/version statements are not new rollout requirements.

# Profile And Bootstrap Integration

The backend constructs one AgentConfigResolver(database, businesses_root,
registry, plugin_handlers={"email-management": validate_email_package}). Each
trusted plugin handler has signature
validate(package: ArtifactPackage) -> None and is registered by the domain
adapter, never imported from an authored manifest. Missing handlers make a
granted profile unready. The operation descriptor's plugin_aliases map is the
only alias grant authority. Config discovery uses registry.required_plugin_for
and invocation enforces the same lookup before canonicalization; requests_create
keeps its generic canonical permission. Domain payload grants are also checked.

AgentOperations(resolver).register(registry, application) installs profile and
catalog reads plus agents_create/update/deactivate, permissions_set, and
plugins_enable/disable. CatalogOperations(catalog, sources=...) installs
roles_publish/plugins_publish. Source registrations are trusted service paths
mapped by source ID to (kind, repository_root, manifest_relative_path).

The application policy uses the same resolve_operation_policy helper as config
provenance. Its context refresh calls resolver.refresh_invocation_context(ctx,
uow), which reads the existing transaction. Full resolve_agent_config runs at
the authenticated request boundary and rejects invalid mandatory configuration.
For a worker or policy callback, resolver.profile(principal,
connection=uow.connection) returns (AgentProfile, indexed_row), and
resolver.packages_for_profile(profile, connection=uow.connection) validates
the pinned artifacts and trusted plugin bindings without context composition.

Both operation registrars install their custom target resolvers. The shared
FileApplier resolver dispatches artifact_files to CatalogOperations.
resolve_file_target and agent_bootstrap_files to AgentOperations.
resolve_file_target; other registered document resources go to their document
adapter. Every profile principal, membership, credential revocation, initial
plan, and artifact file effect is represented in the frozen ChangeSet.
AgentOperations.validate_targets verifies the exact authored source bytes and
artifact integrity again on approval/application. Preparation never replaces
that application validation.

## Owner HTTP

GET /api/biz/{slug}/agents/{agent_id}/config authenticates the selected business
owner, calls AgentOperations.owner_config(ctx, agent_id), and returns the
canonical envelope with data: {effective_config: ...}.

POST /api/biz/{slug}/agents/{agent_id}/permissions invokes permissions_set with
arguments {agent_id, overrides} and InvocationOptions(expected_revision,
idempotency_key). The override map replaces the old map; omitted keys inherit.
Success data is {agent_id, profile_revision, overrides, effective_config_id}.

Retired migration records need no invented role. A row with status retired may
hold sanitized archival profile_json. Default agents_list excludes it;
agents_get and agents_list(include_inactive=true) return the separate
{agent_id, name, display_name, status: "retired", profile_revision,
archival_record: true} DTO. It has no active role or plugin fields. Effective
config, permission inspection, and mutation reject it as inactive_agent.

The effective DTO contains explicit role, scope, plugins, tool_policy.overrides,
profile_revision, effective_config_id, configuration_revision,
context_refresh_required, mandatory_context, skills, tools, preparation, and
delivery. Tool rows use canonical_operation, exposed_aliases,
input_schema_version, plugin_requirement, effective_mode, supports_signoff,
source, available, and optional denial_reason. MCP discovery uses the same
current available rows and filtered aliases. Each call still passes through
the application's current authorization and domain grant checks.

## Local Preparation

The dashboard's existing preparation boundary delegates to
qorqut.bootstrap.preparation.prepare_agent(resolver, authenticated_agent_principal,
endpoint=shared_service_url, versions={"claude-code": observed_claude_version, "codex": observed_codex_version},
interpreter=installed_python). This trusted backend function prepares an already
approved profile; it is not an agent mutation endpoint. The temporary existing
server --prepare-agent compatibility entry delegates identically after resolving
the registered business and agent UUIDs. Do not infer role from a directory.
Prepared folders must be inside the registered businesses root.

An initial legacy projection requires migrate_legacy=True only as part of a
reviewed migration. Normal preparation repairs known managed projections and
removes revoked generated skills; unknown Qorqut skill subtrees are errors.
No preparation code touches personal CLI configuration, starts a model session, changes
tmux, or starts an MCP process. Credentials are opaque token_urlsafe(48) values;
the database stores their unprefixed SHA-256 token_hash and credential ID.
The local credential is mode 0600 and never appears in public configuration.

Both CLI projections use the same registration and scoped credential through the local MCP bridge. Claude
loads `.claude/skills`; Codex loads `.agents/skills`. Both trees are generated
from the same pinned bytes, never independently authored. The same writer removes
revoked skills from both trees. Codex configuration uses the TOML serializer;
hooks live in `.codex/hooks.json` and orientation in `AGENTS.md`. An existing
unmanaged Codex file requires explicit migration before preparation overwrites it.

An ecosystem Ordubashy profile whose adopted, role-owned skills include
`qorqut-role-operations-delegation` receives a Codex `[agents]` block with
Luna/low defaults and a three-helper limit. These defaults apply only when the
owner explicitly requests a native Codex subagent. Ordinary delegation uses the
permanent global team; durable delegated assignments require explicit selection.
If dispatch is unavailable, Ordubashy first tries another suitable worker or
repairs the obstacle, then may perform authorized work directly when useful.
The parent yields for completion/blocker inbox delivery rather
than repeatedly polling status. The role skill selects medium worker effort for
source comparison. Existing Ordubashy pins
without that exact role skill, plugin lookalikes and every other role retain the
prior configuration. Native children inherit the parent's scoped MCP and current
approval/sandbox settings; they are bounded helpers, not registered agents, and
must not bootstrap, own an inbox watcher, consume events or take over `$role`.
Publish the companion role, adopt its immutable pin, reprepare the intended
workspace and refresh the session before relying on these settings.

Durable work is a separate gate: only the 1.34.0+ ecosystem Ordubashy artifact,
the role-owned delegation skill and explicit `manage_delegated_assignments`
profile capability enable it. The parent keeps the Ordubashy role default
(`gpt-6-luna`, high); the Luna/low native-child defaults are unchanged. The six durable
operation policies do not grant that capability. Core migrations 078–083 and
the restricted binding/application checks are present; action receipts retain
the trusted binding and approved plan revision, assignment-linked messages
retain worker attribution, and migration 081 provides exact linked-event
claims and outcome-plus-ack settlement. A local supervisor is attached only to
the prepared parent Codex watcher and launches a separate `exec`/`resume` child
only on Codex CLI 0.155.x. It uses a separate private state directory and
generation-bound credential, ignores user/project config, disables Apps,
plugins, shell, browser, code mode, web search and native subagents, and exposes
only the required Qorqut MCP server. The parent remains the sole inbox
consumer; ambiguous/unlinked events remain visible to it. Waiting work releases
worker capacity and can be woken by an exact linked event or persisted due
time. Migration 083 keeps failed-execution retries separate from claim counts,
so normal wait/resume continuations do not spend the bounded retry budget. A
parent session replacement fences a live child and queues it for
checkpoint-based recovery by the next trusted watcher; this is not a standalone
daemon when no parent watcher is alive. Unsupported CLI versions and
unprepared registrations remain queued with a visible reason. The source
implementation has not been activated or live-trialed; do not treat it as
rollout approval. Limits and recovery boundaries are in
`../../docs/ORDUBASHY-DURABLE-DELEGATION.md`.

The shared service issues no provider credentials: GitHub and Git are the upstream
clients, authenticated the ordinary way inside the terminal.
Readiness is reported per CLI. Missing or drifting files in an unused CLI do not
block the other. The old `prepare_claude_agent` entry remains a compatibility
wrapper. The profile's existing `runtime` field is descriptive, not a lock on
which CLI can use its identity; opening either does not mutate the profile.

Minimum contracts are Claude Code 2.1.263 and Codex CLI 0.153.4. These checks do
not establish project/hook trust, login, or host sandbox compatibility. Review
Codex hooks with `/hooks` and start a fresh conversation. Prepared Codex uses a
restricted permission profile; legacy personal `sandbox_mode` settings override
such profiles and must be reconciled by the owner. Do not disable the sandbox or
silently grant hook trust.

Owner consent (`qorqut/agent_trust.py`) is the one exception, and it is
explicit, not silent: on one dashboard screen the owner allows Qorqbot to start
agents itself. Qorqbot then records folder trust for its prepared agent
workspaces only (`~/.claude.json`, `~/.codex/config.toml`): the workspace folder
as given and, when different, its resolved path. An enclosing git repository
(Qorqbot's operations repository or an external one) is never recorded, so a CLI
can still ask the owner to trust it; the owner answers that in the terminal. The
prepared agent shell runs Codex with `--dangerously-bypass-hook-trust` while the
consent file `<runtime>/agent-trust.json` exists, and only when the shell is
physically in the agent's workspace root (see below). Revoking it in Settings
deletes the file, so new Codex launches return to native hook review. Without
consent nothing changes and the `/hooks` review above still applies.

Claude account connectors (Gmail, Calendar, Drive and others) belong to the
owner's Claude account. Qorqbot does not list or set them up; when an agent
has one, `qorqut/connector_gate.py` blocks its calls that change something
until the owner approves them. Business-scoped agents run Claude with
`dontAsk` and the prepared allow list, so a connector's tools reach Global
agents such as Ordubashy first. Agents browse with Qorqbot's own browser
control (`browser_turn`, docs/BROWSER-CONTROL.md), not with local connectors.

Codex reviews the exact hook definition, including its command. Embedding an
archive release's Python path in that command invalidated trust whenever agents
were prepared against a new release. MCP could remain connected while bootstrap,
activity observations and the inbox watcher were all skipped. A prepared file or
an MCP connection alone is therefore not evidence of automatic inbox delivery.

Prepared Codex hooks now call `/bin/sh <workspace>/.qorqut/codex-hook` with one
fixed mode (`bootstrap`, `lifecycle`, or `activity`). This protected, hash-checked
projection dispatches only those fixed helpers through the selected installed
Python. Repreparation updates the interpreter inside the helper without changing
hook definitions. Credentials and authority still come from the same registration
and service. This does not grant or bypass native hook trust; changes to the hook
definition itself still require review.

Migration: deploy core, reprepare affected agents, then review the new hooks once
with Codex's `/hooks`. Existing sessions that never bootstrapped must be resumed
or started after that review so native bootstrap can claim their own consumer
UUID. Do not fake startup, copy a different consumer UUID, acknowledge pending
messages, or inject a prompt to conceal missing delivery. Verify a current-session
bootstrap receipt, activity registration and the `native_session_owners` row naming
this exact session, binding and generation; old lock files do not establish an owner. Preserve
the old conversation until recovery has been verified.

Project creation is the Global `business_create` operation (the configured Ordubashy is its only caller), not a bootstrap
step: it registers the project with its owner and system memberships and makes the empty `projects/<slug>/hq` and `code` folders. It
creates and prepares no agent, document, Task, routine or external registration: projects have no resident
agents, and the global agents reach a new project at once with no profile or membership write. There is no
seed CLI or template scaffold. Existing preparation preserves current pins and authored content.

register_context_sources registers authored metadata_json.bootstrap_key values:
foundation, ecosystem-agent, ecosystem in namespace global; product, icp, brand,
positioning in the business UUID namespace. Global paths are relative
to the operations root and sit under files/instructions/ (operations/context-graph is
reserved for the published Context graph; Global context/ documents are a separate authored tree); business paths are relative to the
registered hq. Core
pillars require one structurally parsed Agent Brief section. Existing folders
and external file changes require explicit migration/reconciliation.

## Authenticated Hook HTTP

POST /api/agent/bootstrap takes {registration_id, session_id, event,
last_effective_config_id?, runtime?}. Runtime defaults to `claude-code`; `codex`
selects only its projection checks and hook adapter. Authenticate the bearer first, then call
resolver.bootstrap(principal, **arguments). Allowed events are startup, resume,
clear, compact, fork, prompt. Return the canonical status/data envelope. The
response includes ready, effective_config_id, refresh_required, reload_skills,
preparation, the full mandatory_context and a fresh work_briefing when required.
The hook joins the two rendered sections within an 120,000-character total bound.
Preparation retries temporary connection/gateway failures, maintenance and storage
contention on a 30-second cadence within a 180-second window, with each request
limited to 15 seconds and the remaining budget. Identity and payload remain the
same. Permission, registration and configuration failures are not retried. Only
successful normal context delivery clears an existing preparation blocker.
Generated Claude and Codex bootstrap hooks allow 240 seconds for preparation and
post-delivery bookkeeping. Existing agent folders must be prepared with the new
integration during an explicitly approved rollout; source deployment alone does
not update their hook commands or timeouts. Other operations are not replayed.
The briefing uses existing authorized domain reads, never model calls or a new
store. agent_config_get also returns it for explicit refresh. Work changes do not
alter effective_config_id or force preparation. An unchanged prompt
does not reconstruct or return the bundle; cache invalidation includes mandatory
source revisions and external file stat changes followed by hash verification.

Bootstrap repairs two kinds of service-generated drift under the preparation lock.
When a core or package change alters only the instructions of already selected
skills, it rewrites those managed skill files for both CLIs if every one still
matches its recorded hash; role/plugin pins, credential, settings and sessions
are unchanged. Claude tool permissions are refreshed the same way. A changed
skill set, edited file, pin change or credential problem still returns
`reprepare_required` for an operator.

After successful, inbox-enabled bootstrap, the HTTP boundary calls the shared
inbox service with the authenticated principal and registration. A previously
unseen UUID on `startup`, `clear` or `fork` atomically reserves that agent's
inbox and returns `inbox_consumer_id` (the native session UUID) to both CLIs.
The existing `session_registrations.metadata_json.inbox_startup_claim` records
the one-time claim. No new table, endpoint, role permission or client launcher
is introduced. Reservation does not report a successful watcher heartbeat;
the matching watcher must check before inbox reads/acknowledgements.

Repeated startup is idempotent. Resume, compaction and prompts do not take over.
A session whose hooks never delivered but left a local preparation failure sends
its first later hook as `startup`, so a blocked or offline startup still claims.
Once replaced, a session receives `inbox_session_superseded` on bootstrap,
polling, reads and acknowledgements, even after release/expiry of the newer
lease. The shared watcher exits on that response, and its old release cannot
affect the newer owner. Pre-upgrade sessions keep legacy consumer behavior until
the first fresh managed startup for that agent. Claims do not inspect, change or
acknowledge pending events or subscriptions. Keep the session claim records and
current watcher row when maintaining operational history: they prevent replay.

POST /api/agent/bootstrap/receipt takes {registration_id, session_id,
effective_config_id, bundle_hash, event, emitted_at, briefing_hash?}. Authenticate and call
resolver.record_delivery. This records bytes emitted by the hook, never proof
that a model consumed or obeyed them. bundle_hash validates mandatory configuration;
briefing_hash is the hook's observation of the separately emitted live evidence,
saved in receipt metadata without recomputing potentially changed work. It grants
no authority and never acknowledges context reviews. Requests must reject unknown/actor/cwd
fields and bind each registration to the authenticated principal.

Every successful UserPromptSubmit emits the managed one-line response-style
reminder from the prepared Agentic runtime instructions, including when the
configuration is unchanged, for both supported CLIs. Cached delivery preserves
any operational skills and does not create a configuration delivery receipt.
Full deliveries reserve the reminder's space before optional briefing and
environment text; the mandatory configuration hash remains unchanged. Explicit
owner preferences and requests for detail override the brevity default.

SessionStart always emits the bounded full bundle; failure emits a visible
preparation error. The primary composer emits core and topic instructions before
identity, assignment and contextual data. It incorporates the always-loaded skills
instead of installing a second skills hook. The combined limit preserves the
previous mandatory-context and skill budgets (120,000 characters each).
UserPromptSubmit uses the same composer, emits a replacement when changed, and
blocks when unready. Ordubashy's operational-intake reminder is emitted on every
prompt, including when configuration and skills are unchanged. Cached prompts can additionally emit a
short uppercase inbox recovery reminder when `inbox_watcher_check_needed` is
true. This advisory bootstrap field checks agent-level heartbeat freshness
(five minutes plus one minute of grace) and lease expiry; it is not proof of
current-session watcher ownership. No extra receipt, inbox acknowledgement,
process launch or tool gate is produced by that reminder.

For Dev only (the server-supplied `review.role_id`), every hook observes a
private per-registration/session receipt `.qorqut/delivery/<sha256>.review.json`
before the cached return: counts, bounded native Codex turn IDs (kept across a
review reset), the acknowledged `work_reviewed_at` and a due flag, never prompt
text. Lifecycle events, server-verified inbox notices and this registration's
checkpoint continuation never count. The fourth unreviewed owner message adds a
quiet review reminder on cached and full bootstraps; a full bootstrap reserves its
room ahead of optional memory and environment text. A receipt failure never blocks
the owner; it is reported as `bootstrap`/`review` (call site `bootstrap.review`).
The helpers read only the fixed generated
registration/credential and their own receipt files. With explicit service-level
transcript capture enabled, successful hooks also copy only the supplied,
matching CLI session into the registered agent's folder. Capture defaults to on
and can be explicitly disabled by the service owner during preparation.
Stop/SessionEnd use the same bounded copy adapter. DeepSeek reads only these
business-local copies. See `../service/README.md` for scope and failure behavior.
Service revocation remains authoritative on the next call regardless of old
prompt text. Setting-layer and personal/managed-source presence are explicitly
not_inspected. Compatibility tests pin Claude 2.1.263's generated contract;
live conversational acceptance remains an owner-run rollout check.

The native hook composer supports Claude and Codex. Inbox delivery is backend-owned; neither adapter starts Monitor or an inbox watcher. The exact native binding, current process evidence, durable input arbiter and raw transcript capture are described in [Native Chat](../../../docs/v1.2/NATIVE_CHAT_CHECKPOINT.md) and [Inbox](../../../docs/AGENT-INBOX.md). The backend may use a provider queue or verified terminal adapter according to the native transport. These are not agent-managed polling loops. Only explicit inbox tool calls acknowledge events.

Activity observations and checkpoint credentials always use the exact registered
workspace. Claude may continue after its cwd changes only when that registration's
protected session observation proves the same agent, registration, session,
runtime and live native owner/process start, with matching boot and PID namespace
and no offline prior state. This preserves activity and Stop checkpoints; cwd does
not grant business authority. Codex keeps its registered-workspace cwd check.
Generated hooks adopt source changes through trusted preparation; source edits alone
do not update an existing agent folder or recover an already stale watcher/session.

Technical bootstrap, hook, activity, and inbox failures use the same bounded
System Dev incident pipeline. A prepared runtime appends a private, fsynced
`.qorqut/technical-reports/pending.jsonl` record when the service is
unavailable, then replays the exact observation ID on a later authenticated
hook or watcher check. Records contain only a component, operation, bounded
error code, call site, retry metadata, native session ID, and recovery bit;
prompts, tool input, credentials, mail, and transcripts are never included.
`POST /api/agent/technical-observation` derives agent and business identity
from the registered credential, accepts a session with no rows anywhere (a
failed first bootstrap can report before registration succeeds), and otherwise
requires a session row bound to the caller's principal and exact registration
together; a native UUID alone is not an identity. The server
uses durable observation receipts and session-scoped impacts, so a recovery
can close only the matching agent/session/operation episode. Expected
validation, access denial, supersession, normal shutdown, and busy results do
not create incidents. The outbox is bounded; one deterministic recovery is
queued per failure episode (bound, when its marker is unreadable, to the newest
valid queued failure in the whole outbox), records the service rejects for
themselves are quarantined in `rejected.jsonl` with their reason, and local
health separates dropped, queue-full, rejected and quarantine-failed state.
The pending-outbox lock orders every marker write and delete: a failure's append
and marker install are one acquisition, replay repair rechecks under the lock, and
retirement checks the version and unlinks in one locked step, so a newer failure's
marker survives. No service request runs under that lock.
Durable quarantine retires only the exact version-matched local marker; it does
not recover or close anything on the service or clear preparation state, and the
retained evidence keeps the diagnostics `rejected_pending_review` until it is
reconciled. The inbox watcher keeps model-facing outage and restored notices to
an outage of a minute or more, while technical failure and recovery observations
are kept for any outage. Reporting failures
never block or recursively report the user hook.

Migration 072 is required before enabling the service route, followed by fresh
preparation so generated activity hooks receive the helper. No role, grant, or
new daemon is introduced. Replay occurs only during existing bootstrap,
activity/checkpoint, or inbox watcher work; an idle or stopped runtime leaves
its private outbox pending for the next connection.

Generated agent-local Codex configuration explicitly sets
`shell_environment_policy.inherit = "all"` and `ignore_default_excludes = true`
so shell tools retain the operator-provided terminal environment, including
credentials. No values are copied into generated files. Explicit user/managed
filters are not cleared. This changes neither filesystem nor MCP authorization.
The parent terminal must already contain a variable for Codex to inherit it;
reconnecting to an existing tmux shell does not reload Global Settings.

## Role work command

Core installs the shared `role` skill into both clients' prepared skill folders.
Its digest is part of the effective configuration and preparation inventory;
existing agents require re-preparation to adopt it. Use `/role` in Claude Code
and `$role` in Codex. Mandatory startup guidance also establishes the task/inbox
work loop after a greeting and across resume/compaction, preserving explicit
pauses and current approval requirements. See [Role workflow](../../../docs/ROLE-WORKFLOW.md)
for installation conflicts, operational limits and owner-run acceptance.

### Temporary storage contention

The shared Claude/Codex composer retries explicit `busy` bootstrap errors up to
three times with the same registration, session, lifecycle and configuration
payload (250ms and 750ms delays; bootstrap requests capped at 15 seconds).
Other errors are not automatically retried. Failed preparation remains blocking.

A private, session-local `.qorqut/preparation-recovery/` marker tells an existing
inbox watcher to suppress ordinary reminders while preparation is blocked. The
watcher keeps its normal lease checks and probes bootstrap every five minutes.
It announces the pause once and readiness once per failed hook attempt. A probe
never delivers configuration or clears the block: only successful hook delivery
clears the marker and explicitly tells the agent that preparation recovered.
These advisory files do not change service authority, acknowledge inbox events,
or start an agent. Normal reminders resume after successful preparation.

### Environment and credentials section

Full lifecycle/refresh bootstrap responses include a separate `environment`
metadata object. It uses the agent's policy-checked `environment_list` scope.
Every agent is global, so the bootstrap lists Global bindings; `environment_list`
in a selected business adds that business's bindings, which override Global ones.
Explicit tool denials are respected. No secret values are returned.

The shared Claude/Codex hook renders **Environment and credentials (names only)**,
with Settings names, origin and configured/empty state separately from inherited
startup-process variable names. Settings presence is not proof of injection into
an execution tool, nor credential validity or authority to act. Agents should
check presence in the actual tool process before claiming access is unavailable.
The inventory is bounded to 30 Settings entries and 2,400 characters; pagination
is available through `environment_list`. Space-limited bootstraps use a short
lookup reminder, or omit it if even that would exceed the context budget.
Unchanged prompt bootstraps do not repeat the inventory. Existing sessions receive
it on their next full lifecycle/refresh bootstrap after deployment; this change
does not inject credentials into processes or restart sessions.

### Operator-selected Codex bypass

Trusted host preparation accepts `codex_permission_mode="bypass"` on
`prepare_agent`. Select it only with the owner's explicit instruction for that
agent. It generates `default_permissions = ":danger-full-access"` and
`approval_policy = "never"`, with no scoped filesystem profile or deny overlay.
This grants the model host filesystem access; Qorqut MCP authorization and
business Signoffs still apply. Claude settings and other agents are unchanged.

The selection is saved in the machine registration's projection metadata and
preserved on subsequent preparation. Pass `codex_permission_mode="scoped"`
to revoke it. Every agent is global (ecosystem scope), so new agents default to bypass;
scoped access is an explicit selection. Explicit saved selections survive re-preparation. Both modes use
`approval_policy = "never"`; configured MCP servers use
`default_tools_approval_mode = "approve"`. Scoped filesystem denies remain in
force: out-of-scope requests fail instead of prompting. Qorqut server-side
permissions and Signoffs remain unchanged. This is a trusted
preparation option, not an agent-callable permission change. Do not edit generated
config files or their recorded hashes: normal bootstrap integrity checks remain
active. Existing Codex conversations must reload/select Full Access after the
operator prepares them; preparation does not start a model turn or restart a
session. In bypass mode Codex should not need to persist per-tool approval choices.

### No-prompt defaults for both runtimes

All prepared Codex MCP servers explicitly approve tool calls. The service still
checks each call's grants, scope and Signoffs. Scoped agents never prompt for a
sandbox escape; an action outside their grants fails.

Claude local settings select `dontAsk` with explicit allow rules for Qorqut MCP,
role-declared MCP connections, reads, web tools, and writes to existing allowed
paths. Every agent is global (ecosystem scope), so its settings also allow host
tools and carry no generated filesystem deny rules. Its prepared Bash
terminal loads `.qorqut/agent-shell.bash`, which preserves personal `.bashrc`
and makes plain `claude` pass the prepared launch settings via `--settings`.
That trusted launch file selects `bypassPermissions`; the local settings keep
`dontAsk`. The `claude` and `codex` wrappers (bash and zsh) apply that, the
connector arguments, the trust refresh, the Codex hook-trust bypass and the
opening `/role` or `$role` prompt only when `pwd -P` equals the physical workspace
root. From any other folder, a workspace subfolder included, or when the folder
cannot be determined, they run `command claude "$@"` / `command codex "$@"` with
exactly the owner's arguments. Claude ignores bypass when set only in project
settings, so outside the prepared terminal use:

```
claude --settings /absolute/agent/workspace/.qorqut/claude-launch-settings.json
```

Already-running Claude conversations need the native mode selection or a resume
with these launch settings; preparing files never sends model input or kills a
session. Personal/managed ask rules, interaction tools, and platform-enforced
restrictions are not silently rewritten. The no-prompt policy addresses tool
permission dialogs, not questions about missing business information.

## Codex model defaults

Prepared workspaces start on owner-chosen role defaults (`ROLE_MODEL_DEFAULTS` in
`codex_config.py`), for the supported global roles in ecosystem scope only:
Ordubashy `gpt-6-luna` with high reasoning; Learning Applier `gpt-6-astra` with
low reasoning; workers start on Ordubashy's persisted model and effort. Any other
role or scope gets no model default. No Fast/service-tier override is added.
These are project startup defaults, not interactive launcher arguments or model
restrictions: an owner can select another model/effort for a conversation. Native
interactive `codex resume` preserves the conversation selection. Codex may still
warn that saving a user-wide default is shadowed by the project default; that is
different from changing the current conversation.

Preparation updates generated configuration, not running session choices. Existing
processes started explicitly with `--model` keep that selection. Deployment and
agent preparation are required to activate this source change. Roles, task
workers' explicit assignment choices and personal `~/.codex/config.toml` are not
changed. Plain noninteractive `codex exec resume`
uses its own model resolution; the conversation-preservation check here covers
Qorqut's owner-facing native interactive terminal.

The opt-in loopback-only contract `QORQUT_TEST_CODEX_MODEL_DEFAULTS=1` exercises
the installed CLI with synthetic responses and no production credentials: default
Luna request, explicit Astra/high choice, then native interactive resume without
a forced model flag.


## MCP reconnection across releases

The generated `qorqut` server uses a protected `.qorqut/mcp-bridge` stdio
launcher for both Claude and Codex. Its process belongs to the CLI, so a dashboard
restart does not close the local pipe. The bridge forwards JSON Streamable HTTP
to only the registered service's `/mcp`, reads scoped credentials per request,
and never prints credentials or provider exception details.

On the next request after a service restart, an expired MCP session is replaced
immediately. Temporary outages retry with 0.25, 0.5, 1, 2, then 5 second backoff
within a 60-second request budget, including discovery and execution. Individual
HTTP requests allow at most 60 seconds, capped by the remaining budget. Recovery
initializes and acknowledges the new remote session, refreshes
the tool inventory and notifies the CLI that tools changed. Startup/tool timeouts
allow 240 seconds; Claude's startup setting is local to the prepared agent folder.
Exhausting the budget returns an error but keeps the bridge alive and repairs its
connection in the background. Background discovery gets 60 seconds per attempt.
A background repair cannot complete a request that already returned an error.
It does not restart the terminal or model session.

Discovery and known read-only calls may be replayed. A call rejected for an
expired session, or one that failed before connecting, can be sent after recovery.
A write whose response was lost is never automatically repeated: the agent gets
an unknown-outcome error and must reconcile or use the exact original idempotency
key. Authentication, preparation and configuration failures stop immediately.
The bridge does not clear a bootstrap blocker, fabricate context delivery or
bypass current tool permissions. Bootstrap still uses the normal hooks.

This bridge targets Qorqut's existing JSON HTTP endpoint, not arbitrary streaming
MCP servers. Other configured MCP servers keep their connections. Existing bridge
processes retain imported code: selecting a new persistent helper artifact affects
new bridge starts, not an already-open pipe. A supported native MCP reconnect can
start the new helper without restarting the conversation; do not claim rollout
from a configuration rewrite, helper selection or backend deployment alone.
An old direct-HTTP configuration needs explicit preparation/adoption first. Once
the corrected bridge is running, routine service releases need no manual
reconnect. No live preparation is performed by source development or these tests.

Focused checks: `tests/test_mcp_reconnect.py` (including a real stdio subprocess
and loopback HTTP session replacement), `tests/test_preparation_busy_recovery.py`,
and the selected preparation/projection cases in `test_dual_cli.py` and
`test_agent_configuration.py`.
