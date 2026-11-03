# Repository ownership

The publication source is one Git repository with ordinary component directories. A clone needs no private sibling repository
or submodule initialization. The directories retain package boundaries and independent wheels:

```text
qorqut-code/                   host application
  qorqut-operations/           Operations engine
  qorqut-context/              shared Context engine
  biz-dashboard/               owner dashboard
  extensions/                  Google Calendar, optional Telegram and WhatsApp
qorqut-agentic/                agentic engine and bundled role resources
extensions/qorqut-fastmail/    optional Fastmail connector
services/email-engine/        optional Fastmail transport
dependencies/                document-history and email-management
```

`tools/export-unified.py` exports selected component subtrees from one exact commit and records their tree and byte digests.
`tools/export-source.py` remains the private assembly tool for existing independent development repositories; its gitlinks are
input metadata, not a requirement of the publication layout. Neither exporter publishes or changes repository visibility.

## Core

| Repository | Responsibility |
|---|---|
| `qorqut-code` | Application host: composition, database and migrations, permissions, Signoffs, catalog, MCP and HTTP transports, native chat and bootstrap, background jobs, browser/terminal support and everyday integrations. |
| `qorqut-operations` | Portable Operations engine: authority, application and storage contract, Projects, Tasks/Actions, tags, relationships, operational tables, assets, source documents and exact document history, inbox, Signoffs, routines, background services and the snapshot engine. A library; it starts no thread or store until the host invokes it, and it imports nothing from the host, Context or an extension. |
| `qorqut-context` | Shared Context engine: knowledge graph, evidence, propagation, publication and graph history, with no dependencies. Its `qorqut_context.integration` package (extra `integration`, which requires `qorqut-operations`) owns the Context queue, filing, graph edits, reads, questions, usage and recovery that connect it to Operations. |
| `biz-dashboard` | Owner dashboard (static frontend and its small local helpers). Served by the host. |
| `qorqut-agentic` | Standalone agentic engine and packaged defaults, including the three role definitions and shared procedures. Source availability does not imply publication or adoption. |

## Supporting libraries and services

| Repository | Responsibility |
|---|---|
| `document-history` | `qorqut-business-docs-versioning`: shared document revision history and append-only activity, stored in a SQLite connection the host owns. |
| `email-engine` | Fastmail JMAP mail service: account API, rebuildable cache and durable action ledger. It is the transport of the optional Fastmail provider and is prepared only with `--with-fastmail`; Qorqut core keeps no mailbox model. |
| `email-management` | Instruction-only Email Management package (v3: no tools, `owner_input` approval, a native discovery skill and an `instance-template/`), granted by default to the configured Ordubashy. |

The independent `learning-library` repository (`qorqut-learning`) and its Git history are retained separately, but it is
not part of a Qorqbot installation, preparation, build or schema after the retired procedural-learning removal (2026-10-28).
Historical issue-bridge records describe that retired integration.

## Optional extensions

| Repository | Responsibility |
|---|---|
| `qorqut-fastmail` | Optional Fastmail provider: `fastmail_*` account operations, Fastmail CalDAV for the generic calendar operations, a narrow sender-route interface for Paper, and the offline export of legacy mail records. |
| Host `extensions/telegram` | Separately installed Telegram connector. |
| Host `extensions/whatsapp` | Separately installed WhatsApp connector and native helper. |
| Host `extensions/google-calendar` | Bundled Google Calendar provider, inactive until an account is connected. |

Paper, content creation and their supporting libraries are outside this publication repository. Cloudflare and service
provisioning are excluded. Optional connector source does not mean it is installed in the default download.

The [v1.3 module map](v1.3/MODULE-MAP.md) states which package owns which code and which host adapters remain.

[Installation](INSTALLATION.md) exports exact commits of these repositories into one tree and prepares environments
from it; nothing is cloned, fetched or pulled from a private location.

The core starts without either extension installed; extensions are installed
independently and can coexist. Source availability of an extension does not imply it
is installed, published or adopted.

## Catalog and package sources

The host's source map (`app/package-sources.json`) selects the retained role and plugin sources
(`qorqut-agentic/src/qorqut_agentic/resources/defaults`, `qorqut-code/app`, `dependencies/email-management`) by paths relative to an explicit
installation workspace; it carries no repository ownership or URL and enables no Paper or content source.
Publication snapshots only manifest-selected files; profiles pin individual package versions and digests. Repository code is not implicitly added to an
agent's context. There is no automatic discovery of executable code: adding a source
entry does not implement a new operation. The runtime catalog serves role/plugin
discovery and assignment; no marketplace repository or service is needed.

## Compatibility names

The Python distribution and import (`bazarlyk-core`, `bazarlyk`), `BAZARLYK_*` and
`BIZ_*` environment variables, MCP key/tool namespace, generated skill names, tmux
socket, `.bazarlyk` storage directories, saved history refs and internal identifiers
such as `business_id` and `business_create` remain stable compatibility identifiers.
Renaming them would change active integrations and stored receipts.

## Housekeeping

Keep SQL schema migrations and the authority and backup/recovery helpers. Before
deleting any checkout, verify that its remote copy exists, its working tree is clean,
its commits are pushed and no linked worktrees depend on it. Preserve unresolved work
rather than deleting it with the checkout.

Default roles and shared role skills are maintained only in `qorqut-agentic/src/qorqut_agentic/resources/defaults/`. The installed Agentic layer owns the active instructions and preferences.
