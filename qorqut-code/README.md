# Qorqut code repository (v1.3 source)

`qorqut-code` is the application host of Qorqut (Qorqbot v1.3): composition, database and
migrations, permissions, Signoffs, the role and plugin catalog, MCP and HTTP transports, native
chat and bootstrap, background jobs, browser and terminal support, and everyday integrations. The
independent engines and the owner dashboard live in ordinary component directories (see
[Repository ownership](docs/REPOSITORIES.md)); which package owns which code after the Operations / Context / host split is in the
[v1.3 module map](docs/v1.3/MODULE-MAP.md).

For the downloadable Linux application, see [Native Linux distribution](docs/distribution/README.md).

Editing source deploys or starts nothing. The [native release runbook](docs/distribution/RELEASE-RUNBOOK.md)
builds and checks a downloadable Linux package with setup, same-schema updates and optional connector installation.
For development from source, [Installation](docs/INSTALLATION.md) describes committed source export and Python
environment preparation. `tools/export-unified.py` exports the ordinary publication repository;
`tools/export-source.py` remains the private multi-repository transition exporter.
Contracts that apply to the retained product are collected in [docs/v1.2](docs/v1.2/README.md)
(the directory keeps its historical name) and the [operating guide](docs/v1.2/OPERATING_GUIDE.md).

## Layout

```text
qorqut-code/
  app/                        host application (distribution qorqut-core)
    qorqut/                 host package and retained domain integrations (package resources included)
    tests/  scripts/  tools/  templates/  plugins/  pyproject.toml  service-constraints.txt  package-sources.json
  tools/                      committed export, environment preparation and native distribution tools
  qorqut-operations/          Operations engine component
  qorqut-context/             shared Context engine component
  biz-dashboard/              owner dashboard component
  docs/  README.md  FOUNDATION.md  DOMAIN-APIS.md
```

- The publication repository contains these directories directly. `tools/export-unified.py` exports a chosen commit without
  private repositories or submodules. The existing multi-repository exporter is retained for private source assembly.
- Role definitions and shared guidance are shipped by the separate `qorqut-agentic` component under `src/qorqut_agentic/resources/defaults/`; no separate roles checkout is needed for fresh builds. See [agentic integration](docs/AGENTIC-HOST-INTEGRATION.md).
- Instance records live under a separate `operations/` root resolved explicitly by the host, never inside
  this repository.
- Source paths in the documents (`qorqut/`, `tests/`, `tools/`, `scripts/`, `plugins/`, `templates/`,
  `package-sources.json`) are relative to `app/`. `app/tools/`, `app/templates/`, `app/plugins/` and
  `app/scripts/` are source-component resources reached through the explicit `app` component, not wheel
  contents.
- There is no business-template component. The three initial shared instructions are package resources of
  the host (`app/qorqut/bootstrap/instructions/`), and a new project is an empty `hq/` and `code/` folder
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
  [broadcasts](docs/BROADCASTS.md) and [shared reminders](docs/REMINDERS.md): one-off and recurring reminders for the owner,
  Ordubashy and other registered agents through the `reminders_*` operations (the `owner_reminder` tool remains as an
  owner-targeted alias).
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
  [global browser desktops](docs/BROWSER-DESKTOPS.md),
  [agent browser backend](docs/AGENT-BROWSER-BACKEND.md), [command recovery](docs/BROWSER-COMMAND-RECOVERY.md),
  and [owner-directed code sessions](docs/OWNER-DIRECTED-CODE-SESSIONS.md).
- **Everyday integrations.** [Email ownership](docs/EMAIL-OWNERSHIP.md) (the core has no mailbox model; Email Management
  is guidance and the optional Fastmail provider is a separate package); [Calendar](docs/CALENDAR.md) (Google Calendar, plus
  Fastmail CalDAV through the optional extension;
  the owner's personal event labels are private dashboard notes that never change a provider event);
  [Telegram](docs/TELEGRAM-OWNER-BRIDGE.md) (optional, disabled by default, three topics: Ordubashy, Signoffs and
  urgent); WhatsApp ([signoffs](docs/v1.2/WHATSAPP_SIGNOFFS.md), [personal tools](app/tools/whatsapp-personal/README.md));
  and the GitHub-backed [Dev tickets](docs/DEV-TICKETS.md).
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

Python 3.12 or later, with the ordinary component directories described above. Run host commands from `app/` and install from its own declarations and constraints
(`app/pyproject.toml`, `app/service-constraints.txt`):

```sh
cd app
python3 -m venv .venv
# First-party wheels come from a prepared output (docs/INSTALLATION.md): by file path, not from an index or Git URL.
.venv/bin/python -m pip install --no-deps /absolute/prepared/wheels/qorqut_{operations,context,business_docs_versioning,agentic}-*.whl
.venv/bin/python -m pip install -c service-constraints.txt -e '.[service,test]'
```

Keep the code checkout and interpreter as explicit absolute paths; do not reuse a live interpreter.

### Running tests

Current tests live in `app/tests` and `biz-dashboard/tests`. With the current
first-party packages and service dependencies installed in a development venv,
run from the repository root:

```sh
python -I -B -m unittest discover -s qorqut-code/app/tests
node --test --test-concurrency=2 qorqut-code/biz-dashboard/tests/*.test.js
```

Backend tests also need `psutil` for mocked macOS process adapters on Linux.
Browser-dependent Node checks report skips when their optional browser is absent.
The migration, managed-witness and staging-preflight tests exercise upgrade
boundaries. Startup does not implicitly migrate an old installation.

[CI and release preparation](docs/CI-AND-RELEASES.md) describes the automatic
change selection. The [download runbook](docs/distribution/RELEASE-RUNBOOK.md)
qualifies actual downloadable archives separately from routine application changes.

Use synthetic temporary installations and injected providers for ordinary checks.
Real accounts, model-provider calls and native-agent actions need separately
identified acceptance; they are not implied by a source test command.

## Record retention

Routine runs with their jobs, idempotency receipts and application receipts are written for every piece of background
or tool work and are otherwise kept for ever. `qorqut.migration.record_retention` removes the settled ones older than a
window (30 days unless `--days` says otherwise, never less than 7). Nothing runs it automatically, and `apply` removes
nothing without a named `--rule`:

```bash
python -m qorqut.migration.record_retention inspect --instance-root <instance>
python -m qorqut.migration.record_retention apply --instance-root <instance> \
    --rule routine-runs --rule idempotency-receipts --rule apply-receipts [--vacuum]
```

`inspect` is read-only and can run beside the service. `apply` needs the service stopped (exclusive instance ownership).
`routine-runs` removes succeeded runs together with the job each started and keeps failed runs; `apply-receipts` removes
applied receipts that are not bound to a signoff; `idempotency-receipts` means a key is remembered for the window, so a
repeat of an older key is a new call. A row that any other table references is never removed. Freed pages are reused;
`--vacuum` rewrites the file to return them to the disk and needs free space of about the file's size. The exact rules
are in `qorqut-operations/qorqut_operations/storage/retention.py`.

## Operational history packing

The history observer checks for changes at a five-minute minimum interval (`SNAPSHOT_SECONDS` in
`app/qorqut/operations_history/composition.py`) and commits only when the exported tree changes. A table whose rows are
the same as in the last completed snapshot is read and fingerprinted but not checked and serialized again.
The history writer packs Git objects when loose storage reaches 256 MiB, under the same history lock used for snapshots,
with two threads and a bounded delta window; it preserves all commits and refs and does not expire history. A packing
failure is logged without misreporting the already-committed snapshot as lost; a later pass retries. Operational Git
history exports only failed routine runs; routine definitions and schedule changes remain versioned. Existing SQLite rows and
commits are preserved.

## Documentation

- [Core contracts](FOUNDATION.md) and [domain operations](DOMAIN-APIS.md)
- [v1.3 module map](docs/v1.3/MODULE-MAP.md): Operations, Context, Context integration and host ownership, required host adapters, build entry points
- [Architecture and decisions](docs/agent-platform/README.md)
- [Contract index and operating guide](docs/v1.2/README.md)
- [Background programs](app/qorqut/background/README.md)
- [MCP batching](docs/MCP-BATCHING.md)
- [Tool-call latency investigation and evaluation](docs/performance/TOOLCALL-LATENCY-2026-11-10.md)
- [Plugin authoring](docs/plugin-authoring/README.md)
- [Agent bug reports: Dev tickets](docs/DEV-TICKETS.md)

This repository owns the authoritative contract set; keep one copy. Operational databases, credentials, session copies
and backups are not source-control artifacts, and Git exports and versioning are not operational authority.
