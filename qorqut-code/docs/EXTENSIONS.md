# Optional extensions

The core host starts and serves with no extension. Google Calendar is bundled without an account; Fastmail, Telegram and
WhatsApp are separate optional installations. Paper and content creation remain outside the publication repository.
Installed distributions are selected once by the trusted installation configuration, for example:

```json
"extensions": [{"factory": "qorqut_fastmail.extension:create",
                "config": {"engine_url": "http://127.0.0.1:8787", "admin_token_file": "/absolute/private/engine-admin-token"}}]
```

- **Selection** is read once from the trusted installation configuration (`bazarlyk.extensions.installed_selection`), never from a
  request, agent, role package or environment variable. A listed factory that is not installed is a startup error.
- **Composition**: each extension returns an immutable `Extension` declaration (storage, catalog entries, history
  classifications, workspace types, item kinds, source handlers, calendar providers, note derivations, native MCP connections, package validators,
  error statuses, operation guards, selected capability grants, policy rules, Signoff outcome/visibility callbacks, managed guidance, routes, assets, lifecycle services). Core merges them in id order and rejects collisions with
  core names, with each other and with the reserved route namespaces. `install(runtime)` runs once on the still-open registry.
  A registration only adds: a package validator for a plugin core validates, an inbox source keyed like a core table or source, or
  a typed reference named like a core item kind is refused before anything is installed. An ASGI route needs an inspectable absolute
  `path` (a fixed path, a parameterized path that reserves its static prefix, or a route declaring `prefix = True`); it may not
  overlap core's API, MCP, health, static and asset namespaces or pages, or another extension's route, and its frame rules must
  lie inside its own routes.
- **Store**: an extension's tables come from its storage contribution and are part of the store fingerprint. A store is prepared
  offline for one selection (`initialize_empty_target`, used by `python -m bazarlyk.bootstrap.initialize --extensions-config`, see [Installation](INSTALLATION.md)), the runtime never migrates, and a store prepared for a different
  selection is refused when serving: the opened store's extension owners, ordered DDL, table specifications, immutable columns and
  required table / lifecycle metadata must equal the selected extensions' own storage, not only carry the same owner names.
- **Distributions**: each connector declares its compatible host version and its own optional dependencies.
- **Lifecycle**: an extension's services join the start/stop order at the existing core anchors they name and are required recovery writers while selected.
- **Visibility**: `GET /api/root/extensions` lists the selected extensions that ship pane assets; assets are served beneath
  `/extensions/<id>/assets/`. An absent extension yields ordinary not-found responses and no tables, tools or workers.
- **Fastmail provider**: `qorqut_fastmail.extension:create` registers the `fastmail_*` account operations (Global scope, the
  `manage_fastmail` grant), one durable mutation program and the `fastmail` CalDAV connection type for the generic
  `calendar_*` operations (a `CalendarProvider`; core ships no calendar adapter, so a connection type no selected extension
  supplies is `provider_unavailable`). It owns no
  table, so the store fingerprint is the same with or without it. Other extensions find it by id through `runtime.extensions`
  (Paper's explicit sender-route compatibility does). Without it those paths report `provider_unavailable`. See
  [Email ownership](EMAIL-OWNERSHIP.md).
- **Google Calendar provider**: `qorqut_google_calendar.extension:create` (extension id `google_calendar`; source in
  `extensions/google-calendar/` of this repository) supplies the `google` connection type to the same `calendar_*` operations.
  Its configuration is empty (connections stay in the private calendar configuration), it reads no credential or Google service
  when selected, and it owns no table or worker. See [Calendar](CALENDAR.md).
- **Native connections**: an extension that provides an MCP connection names the roles it serves (`NativeConnection.roles`); the
  default role packages do not name any extension.

The declaration contract is [extensions.py](../app/bazarlyk/extensions.py); each package documents its own configuration in its README.
