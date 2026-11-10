# Development preparation from a source export

For installations predating the application package rename, see [Qorqut identifiers and upgrade boundaries](QORQUT-RENAME.md).

For the downloadable Linux application, use the [native distribution guide](distribution/README.md).
For a single publication repository, export the exact commit with `tools/export-unified.py`; its output uses the same
source manifest and preparation contract below.


Existing installations retiring the procedural learning engine use the separately reviewed
[offline learning-removal transition](LEGACY-LEARNING-REMOVAL.md); fresh initialization is not a migration path.

This guide prepares a Qorqbot v1.3 **source tree and Python environments** on one machine and creates one fresh
instance from them. It has four separate stages; the first two build the code, the third is the owner's
configuration, and the fourth is one initialization command followed by a start that only the owner performs.

| Stage | What happens | Who does it |
| --- | --- | --- |
| 1. Source export | One exact Git commit becomes one clean directory | `tools/export-unified.py` |
| 2. Environment preparation | Wheels are built from that export; environments are created | `tools/prepare-local.py` |
| 3. Service configuration | Optional extension selection, credentials, optional services | Owner (below; this guide only points) |
| 4. Initialization and startup | One fresh instance with its owner, then a manual service start | `python -m qorqut.bootstrap.initialize`; the owner starts the service |

Stages 1–2 create no owner identity or instance configuration, install no operating-system service or native
CLI, start no process and contact no provider. The output is **prepared, not configured and not running**.
Nothing resolves a first-party package from a Git URL or a private registry; only third-party packages come
from the normal package index.

## Prerequisites

- Linux, `git`, and **Python 3.12 or newer**. Run both tools with that interpreter (for example `python3.12`) from the root of a
  `qorqut-code` checkout or of its export; they use only the standard library.
- Network access to the Python package index for third-party dependencies and for `setuptools` (wheel builds).
- A local clone of this repository containing the exact commit selected for the build; nothing is cloned or fetched.

Needed only for the optional pieces you enable later (the scripts neither check nor install them):

- **Browser runtime** (agent browser): Node **24+** and npm **11+** (`app/tools/browser/package.json` engines),
  then Chrome (managed Global desktops: [Browser desktops](BROWSER-DESKTOPS.md)). Terminal control needs `tmux`.
- **WhatsApp tool**: Go (the version in `extensions/whatsapp/native/go.mod`) and a C compiler (SQLite uses CGO).
- **Native coding CLIs** for worker sessions (Claude Code, Codex): the pinned versions are in
  `app/tools/native_clis/package.json`.
- **Paper web UI** (only with Paper): Node/npm to build `web/dist`, see below.

## Stage 1 — export the source

From the ordinary single-repository layout, choose one exact commit:

```sh
python3 qorqut-code/tools/export-unified.py --repository /absolute/source-clone --commit <40-hex> --check-only
python3 qorqut-code/tools/export-unified.py --repository /absolute/source-clone --commit <40-hex> --output /absolute/new-export
```

Add `--with-fastmail` when preparing that provider and its separate email engine.
Exporting a provider does not install or configure it. Dirty/untracked files and
Git metadata are excluded. The exporter validates committed directories, safe
paths and symlinks, and publishes the completed export atomically.

The export carries `SOURCE-MANIFEST.json` with component commits, trees and
snapshot digests, without local repository paths. Keep it with the export:
preparation verifies it before building. No sibling repositories or gitlinks
are needed. The older multi-repository exporter remains an internal helper
source for the unified exporter; it is not the normal release entry point.

## Stage 2 — prepare environments

```sh
python3.12 tools/prepare-local.py --source-root /abs/path/export --output /abs/path/prepared \
  [--with-content] [--with-paper] [--with-fastmail] [--with-google-calendar]
```

`--output` must be absent or empty and must not contain or lie inside the export. Symlinks anywhere in the export that are
absolute or resolve outside it (including a required source directory that is itself such a link) are refused before
anything is copied. The export is then copied to `prepared/src` and never modified; subprocesses run in
`prepared/build`, pip configuration files and requirement/constraint/find-links/target/prefix/user overrides in the ambient
environment are ignored (index, proxy, certificate and cache settings are kept for third-party packages), and the tool needs
`packaging` (installed with pip or setuptools). The tool then:

1. creates `prepared/venv` and reuses content-checked pure-Python first-party wheels; cache misses are built
   from the copy into `prepared/wheels` (`pip wheel --no-deps`). Use `--no-wheel-cache` for a cold build;
2. **before anything is installed**, reads each wheel's metadata and reconciles every active first-party requirement
   (`qorqut-*`, markers and extras applied) of each environment against the wheels selected for it. A
   requirement that is a URL, has no wheel among the selected sources, is satisfied only by a wheel built for a different
   environment (for example the Paper service's FastAPI closure), or does not match the local version is reported as an
   integration error, as is the same distribution built twice;
3. installs `prepared/venv` in one `pip install` with the complete first-party set as explicit wheel files (Operations,
   Context, document history, any selected content library/adapter, Paper host bridge and Fastmail host
   bridge, the optional Google Calendar provider (`--with-google-calendar`, built from
   `qorqut-code/extensions/google-calendar`) and the host as `qorqut_core-….whl[service]`) under `app/service-constraints.txt` (and, with
   `--with-fastmail`, the Fastmail package's own `constraints.txt`); third-party dependencies come from the package index;
4. with `--with-fastmail`, creates `prepared/email-engine-venv` for the email engine (the transport of the Fastmail provider)
   and installs its wheel there. A standard preparation builds no email engine and creates no such environment;
5. with `--with-paper`, creates `prepared/paper-service-venv` from the service's hash-locked
   `requirements/runtime.lock` (which must name no first-party package) plus its wheel, so its web-framework dependencies
   never enter the host environment;
6. verifies as a second check that every first-party distribution in each environment was installed from one of the
   wheels the tool built, then writes `prepared/PREPARATION-RECEIPT.json`.

A missing component fails before anything is created, naming what is missing. A
failed step leaves the incomplete output and its `prepared/logs/` in place; remove it before retrying.

The receipt records the Python version, source components (name, commit, tree, snapshot digest), each wheel (name, version,
SHA-256, source directory, owning commit, build-input digest and whether it was reused), the installed versions of every environment, and an explicit
`not_included` list. Optional check of an environment's internal consistency:

```sh
prepared/venv/bin/python -m pip check
```

See [CI and release preparation](CI-AND-RELEASES.md) for wheel cache controls and the distinction between backend releases and download qualification.

## Optional builds (not done by the scripts)

- **Paper web UI.** The Paper service serves a UI only from an explicit built artifact. Build it once from the
  prepared copy (downloads npm packages; nothing is built or fetched implicitly):

  ```sh
  npm --prefix prepared/src/services/paper-service/web ci
  npm --prefix prepared/src/services/paper-service/web run build      # tsc -b && vite build -> web/dist
  ```

  Then set `web_dist` in Paper's settings file to the absolute path of that `web/dist`. With `web_dist` unset
  the API runs without a UI; a configured path that is missing or lacks `index.html` stops `serve-http` and is
  reported by `check-config`.
- **Browser runtime.** From `prepared/src/qorqut-code/app`: `python3 -I tools/browser/build_runtime.py` downloads
  the pinned Node distribution over HTTPS, verifies its SHA-256 and runs the locked `npm ci` (or run
  `npm ci` yourself with Node 24+/npm 11+ in `tools/browser`).
- **WhatsApp tool.** From `prepared/src/qorqut-code/extensions/whatsapp/native`, see "Build and focused checks"
  in its [README](../extensions/whatsapp/native/README.md) (`go build -trimpath -o <absolute output> .`).

## Stage 3 — configuration (owner steps)

Instance configuration lives outside the source and the prepared output, in the instance root created by Stage 4:
private files with mode 0600 and credential values referenced by file, never placed in a manifest, command line,
document or repository. `app/package-sources.json` maps the retained roles (`qorqut-agentic/src/qorqut_agentic/resources/defaults`),
`agent-terminal-control` (`qorqut-code/app`) and ordinary email management (`dependencies/email-management`) to
paths relative to the installation workspace, which is `prepared/src`; the initializer uses that workspace and
needs no copy of the map.

**Extensions.** No extension is selected by default. To select one, write a JSON file naming its trusted factory
(`qorqut_paper.extension:create`, `qorqut_content.extension:create`, `qorqut_fastmail.extension:create`,
`qorqut_google_calendar.extension:create`), for example:

```json
{"extensions": [{"factory": "qorqut_paper.extension:create", "config": {}}]}
```

The file holds exactly the `extensions` list and is passed to the initializer with `--extensions-config`. Each
named package must have been prepared (`--with-paper`, `--with-content`, `--with-fastmail`, `--with-google-calendar`) and installed in `prepared/venv`;
selecting anything else is refused before any file is written. See [Extensions](EXTENSIONS.md) for each factory's
configuration.

Optional integrations are configured per their own documents: [Email ownership](EMAIL-OWNERSHIP.md),
[Calendar](CALENDAR.md), [Telegram](TELEGRAM-OWNER-BRIDGE.md),
[browser control](BROWSER-CONTROL.md) and the WhatsApp README above.

**Fastmail provider (optional).** The core contains no Fastmail code, no email engine and needs no mailbox file. To use the
`fastmail_*` operations and Fastmail CalDAV, prepare with `--with-fastmail`: this installs the `qorqut-fastmail` bridge into
`prepared/venv` and the email engine into the separate `prepared/email-engine-venv` (`python -m engine_email`, see its README
and deployment notes in that component). Nothing is configured or started by the preparation. Run the engine yourself, then
name `qorqut_fastmail.extension:create` in the extensions file with `engine_url` and `admin_token_file` (an absolute private
file holding the engine administration token), as in [Extensions](EXTENSIONS.md). The extension adds no table, so a store
prepared without it opens with it and the reverse.

**Google Calendar provider (optional).** The core contains no Google Calendar adapter or OAuth helper. To use Google events,
prepare with `--with-google-calendar`: this installs the `qorqut-google-calendar` wheel into `prepared/venv` and nothing else, so
a standard preparation records and installs no Google wheel. Nothing is selected, connected or started by the preparation.
Name `qorqut_google_calendar.extension:create` (empty `config`) in the extensions file, then connect an account with
`python -m qorqut_google_calendar.connect` as described in [Calendar](CALENDAR.md). Until an account is connected the extension
only makes a `google` connection type available; it contacts no Google service.

Paper, when selected, runs separately from `prepared/paper-service-venv`. Always give it an explicit settings
file (`EMAIL_OUTREACH_CONFIG`), for example with the safe fake transport, handoff and context kinds first:

```toml
database_url = "sqlite:////abs/path/data/outreach.sqlite3"
bind_host = "127.0.0.1"
bind_port = 8840
web_dist = "/abs/path/prepared/src/services/paper-service/web/dist"
transport_kind = "fake"
handoff_kind = "fake"
context_kind = "fake"
```

Keep that file at mode 0600. Do not enable the host-integration fields until the host bridge is configured.

## Stage 4 — initialization and startup

Initialization creates **one fresh, empty instance**. It is not a migration, adoption, release or reset, it reads
nothing from an existing installation, and it starts no service. Run it with the prepared host environment, so the
host and any selected extension are imported from the installed wheels and not from the source copy:

```sh
prepared/venv/bin/python -m qorqut.bootstrap.initialize \
  --prepared-root /abs/path/prepared --instance-root /abs/path/new-instance \
  [--owner-name "Your Name"] [--extensions-config /abs/path/extensions.json]
```

| Argument | Meaning |
| --- | --- |
| `--prepared-root` | The output of `tools/prepare-local.py` (its `PREPARATION-RECEIPT.json` and `src/SOURCE-MANIFEST.json` must agree) |
| `--instance-root` | The new instance directory: absent or empty, with an existing parent |
| `--owner-name` | The owner's display name; default `Owner` |
| `--extensions-config` | The extension selection of Stage 3; without it no extension is selected |

**Refused before anything is written:** an unprepared or inconsistent prepared root; an instance root that is the
filesystem root, a symlink, below a symlink, missing its parent, a non-directory, or an existing non-empty
directory; an overlap of the instance with the prepared output (so with any source or component, in either
direction); and an extension that is not installed in the prepared environment. Initializing the same instance
again is therefore refused with no change. There is no reset, resume or force option. If a step fails after the
first write, the incomplete directory is left for inspection, nothing is removed or retried, and the command says
so; remove it yourself and start again.

**What it creates**, using the same production primitives as the host-foundation checks: the instance directory
with its operations history and database (every directory 0700, every file 0600; nothing group- or world-readable), one human owner principal and the installation row,
the packaged shared instructions, the `ordubashy`, `global-worker` and `learning-applier` role sources published
through the catalog, exactly two registered agents, Ordubashy and the Learning Applier, with Ordubashy as the
management coordinator, and the system worker principals for the registered background programs. No business
worker membership, project, conversation, routine, job, provider connection or native product agent is created,
and nothing is contacted.

**The operations-root Git repository** is created last, and only here. The table-history and nightly-history
publishers both publish through a Git repository at `<instance>/operations/`; the nightly publisher's first run starts
from that repository's `HEAD`, which must therefore name a commit. The initializer creates a private repository there
(`<instance>/operations/.git`, 0700) whose checked-out branch is the neutral `qorqut-instance`, holding exactly **one
empty bootstrap commit**: Git's empty tree, no parent, authored by the local machine identity `Qorqut
<qorqut@localhost>`. It is built explicitly (no staging, no scan of the instance, no index), with no remote, no hooks, no
signing and no global or system Git configuration, always against that exact `.git` directory so Git can never fall back
to a repository that contains the instance. The two publishers keep their own branches (`qorqut-tables`,
`qorqut-history`) and temporary indexes and never move `qorqut-instance`; `<instance>/operations/.runtime/history.git`
(operational snapshots) and the Context and project repositories are separate stores. The receipt records the repository
path, branch and bootstrap commit. Only a repository this command creates receives that commit: starting, reading or
serving never creates, repairs or completes one, and an existing instance with a missing or unborn repository stays a
separate, explicitly authorized action. This supplies the nightly publisher's Git prerequisite and nothing else: its
schedule, verified backup capture, exports and retention keep their own documented prerequisites.

### Continuing Overseer runs after a conversation change

`evidence_busy` is a pre-dispatch, retryable response for inventory and episode reads.
Overseer automatically retries the identical request between tool calls after waits of
5, 10, 20, 30 and 30 seconds, at most five retries within 120 seconds of the first busy
response. The budget includes request time and is not reset by another busy response.
Do not start a wait/retry that cannot fit within the remaining budget. Stop on a different
error. Exhaustion is a service fault with saved progress, not an owner decision to resume.
This narrow busy policy supersedes generic stop-after-one-retry guidance; it does not
authorize replay of transport failures, uncertain outcomes or mutations. Waiting remains
between calls so it does not consume the existing 60-second MCP connection timeout.

Inventory cursors also retain their server-saved project scope. Adding authorized projects
does not invalidate an existing `all_authorized` cursor or reset its scan position. Every
original namespace must still be authorized, and individual source reads remain checked.
Changed subject filters, unknown cursors and revoked original namespaces are refused.
An `inventory_scope_expanded` gap reports an explicit `additional_selection`: drain the
saved cursor first, then list that selection separately. Never restart the original list
to include the additions. The expanded all-authorized scope is not reported complete merely
because the original cursor drained. Existing frontiers recover legacy cursor scope without
a migration or edits to saved checkpoints.

An admitted extraction, review or application run belongs to its saved Overseer agent and
installation owner. Selecting a replacement native conversation, including switching between
Codex and Claude Code, lets that selected session continue the same run. No dashboard admission,
replacement run or database rewrite is needed. The original invocation, checkpoints, proposals
and retained evidence keep their original identities and conversation provenance.

Run-dependent MCP operations carry the verified native session and binding generation. The host
checks the current role, credential, owner selection and run identity on each call; old sessions,
stale generations and paused selections are refused. Evidence access remains subject to current
source permissions. Existing review decisions, plan agreements and application checks still apply.
Continuing extraction does not start review or application. New workflow invocations and scheduled
due requests retain their own explicit authorization and target-conversation rules.

### Scheduled Overseer extraction (owner-selected, offline setup)

Initialization registers the system worker identities for every program, including
`overseer.extraction_due`; it creates no routine, business, conversation, job or schedule slot. A scheduled
extraction exists only after the owner selects a saved conversation, a schedule and a timezone and creates one
Global/root routine through the existing routine operations. No startup or read path creates one.

The owner first has a saved continuing Overseer conversation with the active `learning-applier` agent (the same
chat must have the current `read_agent_experience` and `manage_agentic_layer` profile grants). Placeholders below
are owner-selected values; the agent id and conversation id are the installed Overseer identity and that saved
chat, not values this guide chooses. `"status": "paused"` saves the definition without activation; changing it to
`"active"` is the owner's explicit scheduling decision.

```json
{"operation": "routines_create",
 "arguments": {"scope": "global", "definition": {
   "name": "<owner-visible name>",
   "schedule": "<owner cron, e.g. 0 9 * * *>",
   "timezone": "<owner IANA zone>",
   "status": "paused",
   "program": "overseer.extraction_due",
   "target": "root",
   "arguments": {"agent_id": "<OVERSEER_AGENT_ID>", "conversation_id": "<SAVED_CONVERSATION_ID>"}}},
 "options": {"idempotency_key": "<owner operation id>"}}
```

Use `routines_update` with the current `expected_revision` to adopt a different schedule, timezone, active state
or target; `routines_pause`, `routines_resume`, `routines_preview` and `routines_runs` are the same existing
operations. A time-only edit keeps the recorded occurrence admissible; pausing or retargeting the routine blocks
it, and an old reference cannot start work until the owner restores matching authority. At the selected slot the
existing scheduler runs the bounded, model-free program, which records one occurrence (missed slots coalesce into
one request with their bounded count) and the existing reconcile hook emits one ordinary inbox event whose source
is `overseer_extraction_due`. In the same continuing chat the agent reads it with `overseer_due_list` or
`overseer_due_get`, then calls `overseer_extract_request` with the exact `reference`; it never starts review or
application from that reference. If that chat is offline, stopped or paused, the request remains pending and the
same reference can be admitted after the owner resumes the same conversation. No delivery advances coverage or
completes a workflow.

For a reproducible synthetic walk-through of exactly that path (real scheduler, program output, ordinary inbox
and same-chat admission, with no provider or native session), run from the repository:

```sh
python -m pytest -q app/tests/test_overseer_schedule_due.py
```

It initializes disposable instances, registers the same worker prerequisite, creates the routine through the real
operation and uses a synthetic saved conversation; it never starts or resumes a real chat. The P07 evidence
script (`scratch/P07/run-focused.sh` in the execution workspace) runs the same focused checks.

The host Operations schema in an existing installation is not upgraded automatically: opening the old store, starting
the host and the fresh-instance initializer stay strict and refuse it. The one explicit, offline, additive transition
is [the agentic/Overseer Operations migration](AGENTIC-OVERSEER-MIGRATION.md). It adds exactly the two additive storage
contributions of this revision to a recognised predecessor store, under the instance lifetime lock and after a
verified private backup, and it never runs at startup. The separate explicit offline upgrade in
[Agentic host integration](AGENTIC-HOST-INTEGRATION.md) covers the agentic binding, packaged seeds and module
state only, preserves the recorded extension selection, and refuses an explicit different selection as a separate
configuration update. It also retains the exact retired binding under
`<instance>/config/agentic-layer-history/<sha256>.json` (0700 directory, 0600 file) before any effect, so the
original supplier provenance stays readable after the active binding names the new distribution.

**Private files**, all mode 0600 in `<instance>/config/` (mode 0700):

| File | Content |
| --- | --- |
| `extensions.json` | The selection that was used |
| `owner-token` | A random owner bootstrap token. It is written only here: never printed, never in the receipt or any command line |
| `initialization-receipt.json` | Identities, selected extensions, paths, source-component and preparation references; no secret |

On success the command prints a summary and **one manual service command** (shown with the paths of your
instance; the instance, component and workspace paths are all explicit, the token only by file):

```sh
/abs/path/prepared/venv/bin/python -m qorqut.service --workspace /abs/path/prepared/src \
  <instance and component paths as printed> \
  --extensions-config /abs/path/new-instance/config/extensions.json \
  --token-file /abs/path/new-instance/config/owner-token
```

Copy the exact command the initializer prints; it is also reproducible from `initialization-receipt.json`. It
binds `127.0.0.1:1049` unless you add `--host` or `--port`. The initializer never runs it, and no operating-system
service is installed: starting, supervising and stopping the service are your decisions. The release worker
described in the [release guide](release/README.md) coordinates frozen source, package and engine releases; it is not
used by this path, and automatic host deployment or upgrade is not supplied.

To check a prepared installation: `prepared/venv/bin/python -m pip check` (and the same for `prepared/email-engine-venv` with
`--with-fastmail` and `prepared/paper-service-venv` with Paper), and `python3 app/tools/check_installed_host.py --prepared-root PREPARED --output
NEW-REPORT.json`, which verifies every wheel and environment against the preparation receipt (see
[Installed-source tooling](INSTALLED-TOOLING.md), which also covers the workstation build context).


Agentic initialization is part of this fresh-instance path. The separate
`qorqut-agentic==0.2.0` local wheel and exported component are required. Export,
preparation and import share `app/qorqut/source_snapshot.py`: it hashes sorted
canonical JSON records of file paths, byte hashes and executable bits (or literal
symlink targets), excluding descendant component roots. Old format-1 exports
without `snapshot_sha256` must be re-exported. This integrity check detects changed
bytes; it is not a signature against the same OS owner.

The initializer imports all supplied procedural sources, enables the core email
and terminal packages plus the explicitly selected extensions, seeds the module's
packaged management toolkit, and commits the initial authored tree. It writes the
private `config/agentic-layer.json` binding last, and references its digest in the
normal initialization receipt. Supplied unselected extension procedures remain
imported and disabled. This does not enable native permissions.

Normal startup only opens the recorded module at `operations/agentic` and
`operations/.runtime/agentic`. Missing or mismatched state, or missing imported assets needed for activation, is an offline
initialization/update error; startup never migrates or imports. The initializer also seeds the
packaged Overseer extraction/review/application package and creates the private Overseer
archive and application-executor roots with mode 0700. An existing installed host is updated
only through the explicit offline upgrade in
[agentic integration](AGENTIC-HOST-INTEGRATION.md#offline-upgrade-and-private-provisioning). That
step is also the only path from `qorqut-agentic==0.1.0` to this 0.2.0 module state schema: a
version 1 binding and schema 1 module state are refused by ordinary opening, and startup never
migrates them. The upgrade keeps the retired version 1 binding byte for byte in the private
history directory beside the active record. Trusted startup configuration may activate or deactivate already imported optional procedures without rewriting their installation record. See
[agentic integration](AGENTIC-HOST-INTEGRATION.md) for editing, observation and
recovery limitations.
