# Shared service integration

An installation runs `qorqut.service` from the interpreter and components pinned in its release manifest (`<installation root>/current/manifest.json`), at its configured origin (`http://qorqut.example:1071` below). Source-local `.venv`, editable-install and `start.sh` procedures do not describe an installed service.

Use the [operating guide](../../../docs/v1.2/OPERATING_GUIDE.md) and the explicitly selected installed worker. `--docs-path` from that worker selects the applicable release procedure. A source edit or successful health read does not authorize deployment or prove every integration works.

## Runtime and assembly

`build_runtime(workspace, *, paths, ...)` receives explicit `InstancePaths`, composed Operations storage, and the shared Context interfaces. See [the factory](runtime.py) for its full signature. `workspace` supplies trusted package/configuration locations; `paths` supplies instance/component locations. The live service uses `--instance-root` and explicit `--component` selectors.

The database is `operations/.runtime/operations.sqlite3`; published Context is `operations/context-graph/`, with separate `operations/.runtime/context/`. Ordinary startup validates the existing composed target; it does not initialize or migrate it. The host owns one application, policy, HTTP/MCP registry, and the lifecycle of background, Context, native Chat and inbox-nudge services. Assembly itself does not start a native agent. Failed drains must be reconciled; do not treat a timed-out shutdown as complete.

See [composition](../../../docs/v1.2/CONTEXT_COMPOSITION.md), [lifetime implementation](lifetime.py), [recovery](../storage/RECOVERY.md), and [native Chat](../../../docs/v1.2/NATIVE_CHAT_CHECKPOINT.md). Backend nudges are current; any “future dispatcher” wording in that implementation checkpoint describes its original stage.

## Configuration and checks

Private installation inputs live in the instance's `config/`, including `installation.json`, `dashboard.env`, and `package-sources.json`. Do not print credentials when inspecting service configuration. Generated agent endpoints must reach the actual listening address; loopback is not assumed when the service binds only a specific interface.

The production environment contains immutable installed host/Operations/Context packages, not editable worktrees. The [release guide](../../../docs/release/README.md) describes the tooling that assembles their exact commits and qualified dependency set. A separate development environment may use editable sources, but must include both local engine distributions and declared service/test dependencies.

Read-only health checks use the configured origin:

```sh
curl --fail --silent --show-error --max-time 5 http://qorqut.example:1071/health/live
curl --fail --silent --show-error --max-time 5 http://qorqut.example:1071/health/ready
```

Inspect the running service command and the release receipt before using either as deployment evidence. Agent preparation uses the service interpreter by default. Every agent runs as a managed session by default ([managed agent sessions](../../../docs/MANAGED-RUNTIME.md)), or in a terminal when the owner chose Terminals (legacy); Chat and inbox nudges share the exact native-session input arbiter. Opening Chat does not start an agent; in managed mode sending a message does. See [owner API access](../../../docs/v1.2/CONTROL_VIA_API.md) and [Inbox](../../../docs/AGENT-INBOX.md).

Nightly maintenance receives `runtime.paths` and the host locks; its receipts and default backups live under `operations/.runtime/maintenance/` and `operations/.runtime/backups/`. Capture requires the actual host recovery barrier. Operational Git snapshots, exact domain histories, private backups and Context Git history have distinct owners and coverage.

## Historical v1 transport and integration notes

The remaining material records older integration details. Old paths, retired Context-base behavior, commands and acceptance requirements below are not current operating instructions. Use the references above and the installed operation schemas for v1.2. Historical evidence has not been re-run by this documentation update.

## Authentication And Routes

Global → Agents reads the canonical `agents_list` operation with explicit
`scope: global` through `GET /api/root/agents`. In managed mode every agent runs as a managed session and is reached through Chat, and the terminal routes (`/api/root/terminals`, `/api/root/terminal/<agent-id>/...`, `/api/root/workspace-terminal/...`) refuse. In Terminals mode they answer. The owner reads and changes the mode through `GET/POST /api/root/agents/runtime`. All owner
writes retain same-origin checks.

- Configure `HOST`, `PORT` and explicit `QORQUT_SERVICE_ORIGINS` in the
  existing `.env.dashboard`. The live port remains 1049; test fixtures forbid it.
- `QORQUT_MCP_URL` selects the advertised shared endpoint for generated folders.
- The private, single-owner dashboard has no browser authentication. Owner API
  requests resolve the current registered installation owner directly, without a
  token, cookie, login form or Tailscale identity lookup. Old `?token=` links
  redirect to `/` without exchanging credentials. Agent bootstrap and MCP use
  their existing scoped agent credentials; they do not become owner requests.
- Hostnames and origins must be explicitly configured. A reverse proxy must
  preserve the external Host. Forwarded headers are not trusted for identity.
  Owner mutations require an exact same-origin Origin header and the installation
  owner in an active selected project.
- `GET /api/session` returns `data.{principal_id,principal_kind,display_name,is_owner}`.
- `POST /api/project/{slug}/agents/{id}/permissions` accepts only `expected_revision`, `idempotency_key` and
  the replacement `overrides` map. The UI rereads configuration after saving.
- Signoff list/get and approve/reject/retry routes use the shared signoff DTOs. Decision
  bodies preserve proposal revision, payload hash and status revision verbatim.
  Approved work is durable and applies without a connected proposing agent.
- `POST /api/project/{slug}/operations/{name}` invokes the same canonical application
  operation as MCP. URL project scope cannot be changed by the body.
- `POST /api/agent/bootstrap` and `/api/agent/bootstrap/receipt` accept only the
  bound agent bearer credential and machine registration. Receipts prove hook
  emission, not model compliance. External mandatory-file drift fails closed.
  Bootstrap refreshes stale Claude MCP permission rules when every existing
  generated file is intact and the saved role, skills and credential still match.
  It preserves credentials, profile settings, hook commands and conversations.
  Preparation and bootstrap serialize per agent; a failed permission update
  restores the original file. Readiness/configuration reads never repair files.
  Dashboard-only and denied tool additions do not invalidate preparation. Codex
  does not project native MCP allowlists, so registry changes do not invalidate
  its generated configuration. Current service policy still gates every call.
  Legacy preparation hashes are compared against verified native permissions.
  Other drift remains blocked with a specific reason and an operator reprepare
  instruction; `/role` or opening another conversation is not a repair action.
- `/mcp` uses the installed MCP SDK's stateful Streamable HTTP transport. Every
  request/reconnect reauthenticates. Sessions cannot move between credentials.
  Discovery uses current effective configuration and installed handlers; guessed
  owner-only operations are rejected before application dispatch.

Legacy dashboard routes in `transport.compatibility` are presentation adapters
over typed operations, never subprocess bridges. They retain the current task,
note, Context, Marketing, asset and routine views. New writes use shown
revisions/hashes; exact operation routes always enforce their declared options.
Marketing replacements stay submitted and need their own owner approval.
The custom conversation-summary button and `/compact` endpoint are retired;
Claude's native compaction and bootstrap redelivery are unchanged.
Routines accept fixed program definitions and typed
arguments, not shell commands. Global routine views/actions require an explicit
review project; stop targets a registered routine/job, never the whole worker.

## Local And External Ports

Global environment defaults live in `<root>/.env`; project overrides live in
each registered HQ's `.env`. Owner-only GET/POST `/api/root/env` manages shared
defaults; `/api/project/<slug>/env` returns local `pairs` plus `inherited` Global pairs,
and POST replaces only local pairs. Removing an override restores inheritance.
Responses are no-store and writes are atomic, mode 0600, with identity keys
rejected. No secret values are stored in the database or committed to Git.
A permanent worker receives no plugin when `agents_create` omits `plugins`;
Ordubashy selects its plugins (`qorqut/agents/plugin_defaults.py` lists the
supported pins). Other agents with project access receive the email
toolkit as an exact pin of the newest published version with default config.
Explicit `[]` opts out and an explicit plugin list remains the caller's exact
selection. Seeding and project restore apply the same defaults;
no separate `plugins_enable` approval is needed. An email grant sends only from its
own mailbox list (Ordubashy: every registered mailbox).
The model provider resolves explicit service configuration, Global defaults, then
the selected project's overrides for each job. Managed sessions receive Global defaults only, and an
open session (or a running agent terminal) keeps its environment until it is closed and started again.

Historical transcript reads accept supported saved Claude/Codex records in
registered agent `sessions/*.jsonl` inside the selected owning workspace. Old
harness-format JSONL is excluded. Project learning retains its newest-20 files
per agent selection. Global learning scans registered Global agents' saved
history in bounded incremental batches, excluding Learning Applier chats. No
personal CLI history or arbitrary application cache is read. See
[Global chat learning](../../../docs/GLOBAL-CHAT-LEARNING.md).

Transcript capture is **on by default for every prepared agent**, including
global agents such as Learning Applier. Saving and processing are distinct:
Learning Applier copies remain excluded from automatic learning. Copies live in each agent's own
`sessions/` folder. The existing service `QORQUT_CAPTURE_TRANSCRIPTS` override
accepts `true` or `false`; project `.env` values and
dashboard request bodies cannot select it. Preparation pins the boolean in the
private registration and adds `Stop` and `SessionEnd` command hooks only when
enabled. Changing the flag back to false takes effect when the agent is prepared
again; old copies are not deleted. Every prepared agent ignores `sessions/` in Git.

`bootstrap.transcript_snapshot` checks the registration opt-in before inspecting
the supplied transcript path. It requires the exact prepared cwd, matching safe
session ID/filename and own-session Claude message records. It takes one bounded
8 MiB UTF-8 JSONL snapshot into that agent's `sessions/<session_id>.jsonl` with
mode 0600, a mode 0700 directory and atomic replacement. It follows no symlinks
and never searches Claude's directories. Successful existing lifecycle/prompt
bootstrap hooks also refresh the selected session; snapshot failures emit only
a nonblocking, redacted diagnostic and preserve the preceding copy.

This is best-effort input for learnings, not lossless transcript archiving.
Claude may still be writing its last message when `Stop` runs; the next approved
event refreshes the snapshot. `SessionEnd` is also best effort. Unsupported
fork/history shapes, changed cwd, partial JSONL and oversized sessions are skipped,
not repaired or read through a scanner. There are no sidecars, retries or extra
services. Real capture and actual Claude hook behavior remain pending owner-run
testing; consent is recorded and fixture tests use synthetic transcripts only.

## Verification And Owner Acceptance

```sh
.venv/bin/python -I -m pytest -q tests/test_service_*.py
../core/.venv/bin/python -I -m pytest -q tests
```

The second command is run from `dashboard`. Tests use temporary databases,
files, credentials and isolated localhost ports.
The actual installed SDK and generated Python hooks are exercised; no test
launches Claude. `tests/service_browser_fixture.py` provides the real isolated
backend for browser tests, including a submitted Marketing request and pending
signoff. Its deterministic model port is test-only.

After the reviewed cutover, the owner should:

1. Message an active agent in its Chat, and confirm
   current identity, role, assignment and selected project facts arrive.
2. Compare an ordinary worker with an agent granted a plugin: only the latter has
   that plugin's skill and aliases. Confirm plugin-native procedure use.
3. Exercise Claude resume, clear and native compact; confirm current bootstrap
   provenance and a matching session copy in this agent's project folder.
4. Revoke a tool/plugin while the session is open and confirm the next call is
   refused. Restore only through the owner profile UI.
5. Propose a signoff mutation, disconnect the agent, approve it in the selected
   project and confirm one exact application with draft feedback discarded.
6. Reject a separate proposal with feedback and confirm the agent can read it.

Actual Claude behavior and transcript copying remain pending the owner's
confirmation. Live health, authentication and project views were independently
checked after cutover. Generated settings and hook receipts alone do
not establish that the model received or followed the supplied context.

## Maintenance

The shared Context is assembled by `qorqut.service.runtime.build_runtime` (`qorqut.shared_context.service`): one queue, filing
and graph-edit port set, and one `Context`/`Pipeline` over `InstancePaths.context` and `InstancePaths.context_runtime`.
Composition is inert; `runtime.context_services` (`start()`, bounded `stop(timeout=…)`, `wake()`, `diagnostics()`) is started and
stopped only by the owner of the instance lifetime, registers the dedicated driver `system` principal, and never initializes a
missing Context. `--context-config` names an explicit JSON file of the library's `RunnerConfig` fields; without it the library
defaults apply. The old `qorqut-contextbase` package, `ContextBaseService` and `CONTEXT_FILER_MODE` are retired.

The following service-specific rationale is retained for maintainers:

- Remove dead `server.py` bridge/routine/HTTP implementation bodies after any
  remaining external imports have moved to the shared service. Keep only the
  entrypoint and genuinely reused parsers; current production does not use them.
  Start with `Bridge`, `Routines`, and `Handler`; their public routes already
  return retired responses or run through typed compatibility adapters.
- Retire compatibility revision fallbacks and duplicate response shaping after
  all dashboard mutation controls send the canonical DTOs and shown revisions.
  Keep the existing views and draft-preservation behavior during that change.
- Replace duplicated Context presentation assembly with direct typed view DTOs
  only after imported map/person metadata and fresh UUID-backed uploads have
  full browser coverage. Do not create another document authority.

Removed checks that intentionally no longer describe supported behavior include
per-user Claude plugin installation, CLI actor overrides, direct writable legacy
HTTP handlers, arbitrary routine command strings and automatic Git commits/pushes
for operational writes. Connected core tests replace these expectations.
Newly exported live transcripts, provider network
behavior and real Claude lifecycle behavior are not claimed by fixture tests.
