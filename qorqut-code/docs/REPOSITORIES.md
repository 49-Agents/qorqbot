# Repository ownership

Qorqut (Qorqbot v1.3) is a set of sibling Git repositories. `qorqut-code` is the
application host; its three submodule paths (`qorqut-operations`, `qorqut-context`,
`biz-dashboard`) name sibling repositories by relative URL (see `.gitmodules`), so
checkouts must sit next to each other:

```text
/absolute/path/to/qorqut/
├── qorqut-code/          host application (this repository)
├── qorqut-operations/    Operations engine
├── qorqut-context/       shared Context engine
├── biz-dashboard/        owner dashboard
├── qorqut-agentic/       agentic engine and default role resources
└── ...                   optional and supporting repositories below
```

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
| `qorqut-automation` | Optional scripting and automation: Ordubashy's durable scripted automations (`automations_*`, the run-bound Python SDK and bridge, the `automation.run` program, failure delivery) and their authored Agentic plugin. Requires the offline [automation upgrade](AUTOMATION-UPGRADE.md). |
| `qorqut-paper` | Paper (email outreach) extension packaging and UI assets. Paper's own campaign state stays with the Paper service. |
| `paper-service-current` | The Paper service: campaign authoring API and interface, workers and deployment units. |
| `qorqut-content` | Content-creation extension: production guidance and closed method dependencies. |
| `content-library` | Content-creation plugin, recorder library and reusable gallery template used by the content extension. |

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
