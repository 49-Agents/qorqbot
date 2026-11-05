# Reusable core source map

This map describes source ownership, not an installed instance or a deployment.
The [installation guide](../INSTALLATION.md) describes source export and fresh
preparation. See [agentic host integration](../AGENTIC-HOST-INTEGRATION.md) for
installation and the authored/runtime boundary.

| Source component | Ownership and source pointers |
| --- | --- |
| Host `qorqut-code` | Composition, transports, native sessions, authority and integrations: `app/bazarlyk/composition/`, `app/bazarlyk/agentic/`, `app/bazarlyk/agents/`, `app/bazarlyk/service/`. Native browser and tool implementations remain host or extension code. |
| Operations `qorqut-code/qorqut-operations` | Tasks, Actions, Projects, documents, Signoffs, inbox and storage contracts: `qorqut_operations/`. |
| Context `qorqut-code/qorqut-context` | Shared Context engine and Operations integration: `qorqut_context/` and `qorqut_context/integration/`. Host wiring is in `app/bazarlyk/shared_context/`. |
| Agentic `qorqut-agentic` | Standalone catalogue, authoring, history, change feed and Overseer storage: `src/qorqut_agentic/`. Default role and guidance resources live in `src/qorqut_agentic/resources/defaults/`; the three role definitions are under `roles/imported-roles/`. |
| Dashboard `qorqut-code/biz-dashboard` | Static owner interface: `index.html`, `js/`, `css/`, `server.py`. |
| Document history, email | Separate source components `dependencies/document-history/`, `dependencies/email-management/`; Operations uses document history and email guidance is imported into agentic plugins. The procedural learning engine is retired; factual Context intake and read-only historical records remain. |
| Optional extensions | Paper, content and Fastmail are separately installed and selected by trusted host configuration; see [Extensions](../EXTENSIONS.md) and `app/package-sources.json`. |

The host composes Operations, Context integration and the agentic engine; Context
integration uses Operations, and Operations uses document history. Extensions
depend on the host, not the reverse. `tools/export-source.py` and
`tools/prepare-local.py` export selected components and prepare local wheels.
The dashboard is static source; no frontend framework or build step is implied.

The engine's defaults are reusable package resources. Fresh initialization
creates a separate per-instance authored Git tree at `operations/agentic/` and
separate state at `operations/.runtime/agentic/`; those are instance data, not
source repositories. Its five authored areas are `roles/`, `operations/`,
`overseer/`, `plugins/` and `user-preferences/`. See
[fresh installation](../agentic/fresh-install.md) and the
[offline layout upgrade boundary](../../app/bazarlyk/agentic/LAYOUT-UPGRADE.md).

## Shared boundaries and host adapters

An Action revision and its Context input share one SQLite commit. Operations
settles the Action; the host supplies `qorqut_context.integration.queue.admit_action_revision`
as the required callback. A failed admission rolls back both. Context owns the
queue and its driver; `app/bazarlyk/shared_context/service.py` binds host lifetime
and configuration. There is no second queue or network hop.

Operations owns source document storage, parsing, search, journal and snapshots
in `qorqut_operations/documents/`. Context's `qorqut_context/integration/filing/`
freezes source versions and adds filing information; `integration/reading/` serves
published graph reads. The host composes both in
`app/bazarlyk/domains/documents/composition.py`.

Operations' resolver and host policy rules own authorization; Context contributes
policy declarations. Operations owns the snapshot engine and export catalogue,
while Context supplies coverage for its storage contributions and the host selects
and composes areas. Operations supplies recovery barriers, Context its capture, and
`app/bazarlyk/storage/recovery.py` coordinates instance recovery.

Other host adapters include `app/bazarlyk/domains/tasks/ports.py` for task routing,
`app/bazarlyk/domains/items/` for identity and relationship ports,
`app/bazarlyk/inbox/` for delivery, `app/bazarlyk/operations_history/` for history
composition, and `app/bazarlyk/composition/` and `service/` for runtime wiring.
