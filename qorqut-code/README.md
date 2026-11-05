# Qorqut code repository (v1.3 source)

`qorqut-code` is the application host of Qorqut (Qorqbot v1.3): composition, database and
migrations, permissions, Signoffs, the role and plugin catalog, MCP and HTTP transports, native
chat and bootstrap, background jobs, browser and terminal support, and everyday integrations. The
independent engines and the owner dashboard live in sibling repositories (see
[Repository ownership](docs/REPOSITORIES.md)); which package owns which code after the Operations / Context / host split is in the
[v1.3 module map](docs/v1.3/MODULE-MAP.md).

This is source. Editing or building it deploys, installs or starts nothing. A local source export and
the Python environments for it are prepared as described in [Installation](docs/INSTALLATION.md)
(`tools/export-source.py`, `tools/prepare-local.py`) and a fresh instance is initialized and started by hand; that
is the supported path, and automatic host deployment or upgrade is not supplied. The release-worker tooling in the
[release guide](docs/release/README.md) coordinates explicitly frozen source, package and engine releases only; it is
not a host installer and claims no installation or rehearsal. Contracts that apply to
the retained product are collected in [docs/v1.2](docs/v1.2/README.md) (the directory keeps its historical name) and the
[operating guide](docs/v1.2/OPERATING_GUIDE.md).

## Layout

```text
qorqut-code/
  app/                        host application (distribution bazarlyk-core)
    bazarlyk/                 host package and retained domain integrations (package resources included)
    tests/  scripts/  tools/  templates/  plugins/  pyproject.toml  service-constraints.txt  package-sources.json
  tools/                      export-source.py and prepare-local.py (source export and environment preparation)
  qorqut-operations/          submodule path: Operations engine (exact source pin: Git gitlink)
  qorqut-context/             submodule path: shared Context engine (exact source pin: Git gitlink)
  biz-dashboard/              submodule path: owner dashboard (exact source pin: Git gitlink)
  docs/  README.md  FOUNDATION.md  DOMAIN-APIS.md
```

- `.gitmodules` names the two engines and the dashboard by relative sibling URLs (`../qorqut-operations`,
  `../qorqut-context`, `../biz-dashboard`), so a checkout expects its siblings beside it. Installation does not
  initialize submodules: a source export fills those locations from explicitly selected components
  ([Installation](docs/INSTALLATION.md)).
- Role definitions and shared guidance are shipped by the separate `qorqut-agentic` component under `src/qorqut_agentic/resources/defaults/`; no separate roles checkout is needed for fresh builds. See [agentic integration](docs/AGENTIC-HOST-INTEGRATION.md).
- Instance records live under a separate `operations/` root resolved explicitly by the host, never inside
  this repository.
- Source paths in the documents (`bazarlyk/`, `tests/`, `tools/`, `scripts/`, `plugins/`, `templates/`,
  `package-sources.json`) are relative to `app/`. `app/tools/`, `app/templates/`, `app/plugins/` and
  `app/scripts/` are source-component resources reached through the explicit `app` component, not wheel
  contents.
- There is no business-template component. The three initial shared instructions are package resources of
  the host (`app/bazarlyk/bootstrap/instructions/`), and a new project is an empty `hq/` and `code/` folder
  created only by the configured Ordubashy ([Project creation](docs/BUSINESS-CREATION.md)).
- Owned startup validates the database and enables WAL before admitting work or starting background
  services. Assembling or inspecting an instance never changes its journal mode.

## Capabilities

The host composes the following retained capabilities. Each links to its contract.

- **Projects and records.** Projects own resources (files, records, code, integrations), not resident agents;
  creation, [archive and restore](docs/BUSINESS-ARCHIVE.md), [settings](docs/BUSINESS-SETTINGS.md),
  [backups](docs/BUSINESS-BACKUPS.md), [tables](docs/TABLES.md) and [document history](docs/DOCUMENT-HISTORY.md).
  Project notes are ordinary documents (`notes_create`, `notes_list`) filed under `notes/<date>-<slug>.md`; a tag
  grants nothing and nothing injects a note into an agent's context. The owner's Global notebook is available
  through `/api/root/notes`.
- **Operations.** [Tasks and work groups](docs/v1.2/PARENT_TASKS_HOST.md) (a work group is a parent Task),
  [Global tasks](docs/GLOBAL-TASKS.md), [personal taskbox](docs/PERSONAL-TASKBOX.md), [task snoozing](docs/TASK-SNOOZING.md),
  [completed Actions](docs/ACTIONS.md) (operational time is America/Puerto_Rico, labelled "San Juan time"),
  [broadcasts and reminders](docs/BROADCASTS.md) and the transport-neutral `owner_reminder` tool, which schedules,
  lists, updates or cancels a message at an absolute time through the durable outbox worker.
- **Authorization, Signoffs and routines.** Owner review of prepared plans, input requests and routines,
  [Signoff input notifications](docs/SIGNOFF-INPUT-NOTIFICATIONS.md),
  [Global review routing](docs/agent-platform/05-GLOBAL-SCOPE-AND-REVIEW.md) and
  [routine tool compaction](docs/ROUTINE-TOOL-COMPACTION.md).
- **Shared Context.** The `qorqut-context` library and its Operations integration (`qorqut_context.integration`: the one queue and
  worker, filing, graph edits, reads, questions, usage and recovery), composed by the host ([module map](docs/v1.3/MODULE-MAP.md)):
  [composition](docs/v1.2/CONTEXT_COMPOSITION.md), [filing](docs/v1.2/CONTEXT_FILING.md),
  [reads](docs/v1.2/SHARED_CONTEXT_READS.md), [writer](docs/v1.2/SHARED_CONTEXT_WRITER.md),
  [graph edits](docs/v1.2/CONTEXT_GRAPH_EDIT.md), [relationships](docs/CONTEXT-RELATIVITY.md) and
  [person details](docs/person-details.md).
- **Roster and agents.** The active roster is Ordubashy (the stable coordinator name), any number of ordinary
  global workers and the Learning Applier, with at most one active registration of Ordubashy and of the
  Learning Applier. Each reaches every active project through derived access; a saved business list or
  membership row is never authority. Agents cannot create agents (Ordubashy registers workers). See
  [agent platform decisions](docs/agent-platform/README.md), [agent management coordination](docs/AGENT-MANAGEMENT-COORDINATION.md),
  [agent inbox](docs/AGENT-INBOX.md), [messaging and the keyless local API](docs/AGENT-MESSAGING.md),
  [Viewbox](docs/AGENT-VIEWBOX.md), [working memory](docs/WORKING-MEMORY.md) and
  [terminal control](docs/AGENT-TERMINAL-CONTROL.md).
- **Chat and attention.** [Native Chat](docs/v1.2/NATIVE_CHAT_CHECKPOINT.md) is derived from each registered agent's
  captured native conversation; every input to a native session passes one arbiter bound to the exact registered
  session. Shared Codex sessions use the existing app-server Unix socket for Chat and steering input
  (`turn/steer` targets the exact active turn; an idle loaded conversation receives `turn/start`; an input
  whose delivery is uncertain is never retried). [Owner attention](docs/OWNER-ATTENTION.md) covers Focus cards,
  the attention queue (`GET/POST /api/root/attention-queue`) and conversations; Focus ordering is
  installation-owner-only. [Owner journal](docs/OWNER-JOURNAL.md) and
  [private conversations](docs/OWNER-PRIVATE-CONVERSATIONS.md) hold the owner's own records.
- **Learning.** Factual Context [multi-source learning](docs/MULTISOURCE-LEARNING.md),
  [Global chat learning](docs/GLOBAL-CHAT-LEARNING.md), the [review companion](docs/LEARNING-REVIEW-COMPANION.md) and
  [supersession](docs/LEARNING-SUPERSESSION.md). Retained historical procedural records stay readable; the retired
  engine's tables and the standalone `qorqut-learning` backend are not installed or started. Its independent source
  history remains outside this release.
- **Browser and terminal.** [Chrome control](docs/BROWSER-CONTROL.md), [browser handles](docs/BROWSER-HANDLES.md),
  [isolated project browser](docs/BROWSER-ISOLATED.md), [global browser desktops](docs/BROWSER-DESKTOPS.md),
  [agent browser backend](docs/AGENT-BROWSER-BACKEND.md), [command recovery](docs/BROWSER-COMMAND-RECOVERY.md),
  [owner-directed code sessions](docs/OWNER-DIRECTED-CODE-SESSIONS.md) and the [sandbox](docs/sandbox/BUILDS-AND-DEPLOYMENTS.md).
- **Everyday integrations.** [Email ownership](docs/EMAIL-OWNERSHIP.md) (the core has no mailbox model; Email Management
  is guidance and the optional Fastmail provider is a separate package); [Calendar](docs/CALENDAR.md) (Google Calendar, plus
  Fastmail CalDAV through the optional extension;
  the owner's personal event labels are private dashboard notes that never change a provider event);
  [Telegram](docs/TELEGRAM-OWNER-BRIDGE.md) (optional, disabled by default, three topics: Ordubashy, Signoffs and
  urgent); WhatsApp ([signoffs](docs/v1.2/WHATSAPP_SIGNOFFS.md), [personal tools](app/tools/whatsapp-personal/README.md));
  [Cloudflare and project services](docs/PROJECT-SERVICES.md); and the GitHub-backed [Dev tickets](docs/DEV-TICKETS.md).
- **Optional extensions.** Paper (email outreach), content creation and the Fastmail provider are installed independently of the core;
  see [Repository ownership](docs/REPOSITORIES.md).

## Packaging and checks (`app/`)

The host depends on the local Operations, Context and document-history distributions (and, for the learning extra,
the learning library) by exact version; their wheels are built from the selected local source export
([Installation](docs/INSTALLATION.md)), never taken from a Git URL or a private registry. Installing the host wheel
alone does not establish the accepted engine versions: `tools/prepare-local.py` builds the complete first-party wheel set
from an exported source tree and installs each owned environment from those exact local files.

`app/tools/check_installed_host.py` verifies such a prepared installation against its own `PREPARATION-RECEIPT.json` and the
`SOURCE-MANIFEST.json` of its `src` copy, and writes one new report. It installs, repairs and modifies nothing, runs no test and
contacts no service:

```sh
python3 app/tools/check_installed_host.py --prepared-root /absolute/prepared --output /absolute/new-report.json
```

It refuses a missing or mismatched wheel, a first-party package installed from anything but the receipt's wheel file (an index,
VCS, directory or other URL origin), installed Python or resource bytes that differ from the wheel, imports or `sys.path` entries
outside the environment, and a failing `pip check`. See the [installed tooling guide](docs/INSTALLED-TOOLING.md).

The release worker and its installation contract are in the [release guide](docs/release/README.md) and the
[worker installation](docs/release/INSTALL.md); neither deploys or upgrades the host.

## Development

Python 3.12 or later, with the sibling repositories checked out beside this one and the role repository supplied
explicitly. Run host commands from `app/` and install from its own declarations and constraints
(`app/pyproject.toml`, `app/service-constraints.txt`):

```sh
cd app
python3 -m venv .venv
# First-party wheels come from a prepared output (docs/INSTALLATION.md): by file path, not from an index or Git URL.
.venv/bin/python -m pip install --no-deps /absolute/prepared/wheels/qorqut_{operations,context,business_docs_versioning}-*.whl
.venv/bin/python -m pip install -c service-constraints.txt -e '.[service,test]'
```

Keep the code checkout and interpreter as explicit absolute paths; do not reuse a live interpreter.

### Running tests

Run the tests that cover what you changed: name the files or node IDs and let pytest report where the time goes.

```sh
.venv/bin/python -I -m pytest -q -p no:cacheprovider --durations=10 \
  tests/test_attention_queue.py 'tests/test_routine_read_paths.py::test_routine_grouping_matches_between_sql_and_python'
```

Running the whole suite (`.venv/bin/python -I -m pytest -q tests`) or any other broad suite needs the owner's explicit
authorization; the focused run above stays the default.

| Area | Focused checks |
| --- | --- |
| Work groups | `tests/test_host_parent_tasks.py` |
| Actions | `tests/test_unified_actions.py` |
| Owner reminders | `tests/test_owner_reminders.py` |
| Owner attention and Focus | `tests/test_attention_queue.py`, `tests/test_focus_completion.py` |
| Project archive and restore | `tests/test_host_business_archive.py` |
| Private conversations and channels | `tests/test_business_task_channels.py` |
| Calendar | `tests/test_calendar_tools.py`, `tests/test_calendar_statuses.py` |
| Chat input and Codex steering | `tests/test_request_user_input.py`, `tests/test_native_chat_user_input.py`, `tests/test_codex_steering.py` |
| Browser surfaces and desktops | `tests/test_browser_surfaces.py`, `tests/test_browser_isolated_surfaces.py`, `tests/test_browser_desktop_session.py` |
| Context filing and usage | `tests/test_context_completion_acceptance.py`, `tests/test_host_context_usage_collector.py` |
| Operational history | `tests/test_operations_history_paths.py`, `tests/test_host_history_composition.py` (host wiring); `qorqut-operations/tests/test_history_snapshot.py`, `test_history_adapters.py`, `test_history_cadence.py` (snapshot engine) |
| Integration diagnostics | `tests/test_integration_diagnostics.py` |

Test guidance:

- **Light domain tests first.** Validation, domain and storage behavior runs on a small application:
  `foundation_support.database_at`, `Application` with `FixedPolicy`, and only the domain's operations registered (see
  `make_tasks` in `tests/test_domain_tasks.py`). `FixedPolicy` returns a preset decision per operation and never
  exercises the real `PolicyResolver` or grants, so authorization and grant tests must use real policy resolution.
  Use `ServiceFixture(full=True)` only for behavior that crosses the assembled runtime (HTTP/MCP transport,
  authentication, real agent policy, background workers); keep one full-service test for the boundary and the rest of a
  matrix on the light application.
- **Fresh databases are cloned, not migrated.** `database_at` and `test_agent_configuration.Fixture` copy a
  process-local seed of the *empty* migrated schema (`tests/schema_seed.py`) instead of applying every migration again.
  Each test gets its own file, `Database` object and connections, and no rows, artifacts, credentials, documents or agent
  folders are cached. Patched migrations, initializer or hooks, and existing files use real initialization
  automatically; a test of initialization itself passes `real_initialization=True` (or `real=True` to the helper).
- **A test earns its place.** Keep tests that protect observable behavior, a failure boundary or an actual regression.
  Remove one only after naming the retained test that protects its useful behavior. Cheap validation and predicate
  matrices, security checks and old-schema tests stay.

Default tests use temporary installations and injected providers, never real model-provider or native-agent actions. Live
filer-provider tests are separately opt-in; use `QORQUT_TEST_FILER_LIVE=0` to disable them explicitly. Authority checks,
backup/recovery and SQL schema migrations remain supported ([Backup and recovery](app/bazarlyk/storage/RECOVERY.md)).

## Operational history packing

The history observer checks for changes at a 60-second minimum interval and commits only when the exported tree changes.
The history writer packs Git objects when loose storage reaches 256 MiB, under the same history lock used for snapshots,
with two threads and a bounded delta window; it preserves all commits and refs and does not expire history. A packing
failure is logged without misreporting the already-committed snapshot as lost; a later pass retries. Operational Git
history exports only failed routine runs; routine definitions and schedule changes remain versioned. This neither prunes
SQLite rows nor rewrites old commits.

## Documentation

- [Core contracts](FOUNDATION.md) and [domain operations](DOMAIN-APIS.md)
- [v1.3 module map](docs/v1.3/MODULE-MAP.md): Operations, Context, Context integration and host ownership, required host adapters, build entry points
- [Architecture and decisions](docs/agent-platform/README.md)
- [Contract index and operating guide](docs/v1.2/README.md)
- [Background programs](app/bazarlyk/background/README.md)
- [MCP batching](docs/MCP-BATCHING.md)
- [Plugin authoring](docs/plugin-authoring/README.md)
- [Agent bug reports: Dev tickets](docs/DEV-TICKETS.md)

This repository owns the authoritative contract set; keep one copy. Operational databases, credentials, session copies
and backups are not source-control artifacts, and Git exports and versioning are not operational authority.
