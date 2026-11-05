# Optional extensions

The core host starts and serves with no extension. Paper (`qorqut-paper`), recorded content (`qorqut-content`) and the Fastmail
provider (`qorqut-fastmail`) and scripting and automation (`qorqut-automation`) are separate installable Python distributions that the trusted installation selects at startup:

```json
"extensions": [{"factory": "qorqut_paper.extension:create", "config": {}},
               {"factory": "qorqut_content.extension:create", "config": {"schema_version": 1, "enabled": false}},
               {"factory": "qorqut_fastmail.extension:create",
                "config": {"engine_url": "http://127.0.0.1:8787", "admin_token_file": "/absolute/private/engine-admin-token"}}]
```

- **Selection** is read once from the trusted installation configuration (`bazarlyk.extensions.installed_selection`), never from a
  request, agent, role package or environment variable. A listed factory that is not installed is a startup error.
- **Composition**: each extension returns an immutable `Extension` declaration (storage, catalog entries, history
  classifications, workspace types, item kinds, source handlers, calendar providers, note derivations, native MCP connections, package validators,
  error statuses, operation guards, routes, assets, lifecycle services). Core merges them in id order and rejects collisions with
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
- **Distributions**: every extension requires `bazarlyk-core==1.3.0.dev0`; the content extension also requires the published
  `bazarlyk-content==0.7.0` library (no direct-URL requirement).
- **Lifecycle**: an extension's services join the start/stop order at the anchors they name (Paper before `changes` / stop before
  `previews`; content before `telegram` / stop before `context`) and are required recovery writers while selected.
- **Visibility**: `GET /api/root/extensions` lists the selected extensions that ship pane assets; assets are served beneath
  `/extensions/<id>/assets/`. An absent extension yields ordinary not-found responses and no tables, tools or workers.
- **Fastmail provider**: `qorqut_fastmail.extension:create` registers the `fastmail_*` account operations (Global scope, the
  `manage_fastmail` grant), one durable mutation program and the `fastmail` CalDAV connection type for the generic
  `calendar_*` operations (a `CalendarProvider`; the core keeps Google and refuses an extension that names `google`). It owns no
  table, so the store fingerprint is the same with or without it. Other extensions find it by id through `runtime.extensions`
  (Paper's explicit sender-route compatibility does). Without it those paths report `provider_unavailable`. See
  [Email ownership](EMAIL-OWNERSHIP.md).
- **Scripting and automation**: `qorqut_automation.extension:create` (config, all optional: `python`, `state_dir`,
  `invocation_seconds` (60), `max_run_seconds`, `max_calls`, `max_step_bytes`) registers Ordubashy's `automations_*` tools (the
  `manage_automations` Global capability; Global and Project scope, plain reads also open to other authorized agents), the durable
  `automation.run` program (admitted, polled and cancellation-settled by the Operations engine worker), the run-bound Python bridge and the `automation`
  inbox source. Its scripts are authored through Agentic authoring (`automations_script_write`) and scheduled through an ordinary
  routine (`automations_schedule_set`). It owns two tables (`automations`, `automation_steps`) and the core `owner_conversations`
  source CHECK now accepts `automation`, so a store must be upgraded offline once with `python -m bazarlyk.migration.automation_upgrade`
  (see [Automation upgrade](AUTOMATION-UPGRADE.md)). Without it no automation tool, table or worker exists. The host only knows the
  generic seams it consumes: the trusted `InvocationContext.automation_run_id`, the one-shot policy gate and capability set bound by the
  extension, the `Extension.background_tick()` hook, the `manage_automations` capability, and the run binding that Fastmail and
  WhatsApp sends carry from their owner-approved plan to the pre-dispatch authorization.
- **Background cadence**: an extension needing a periodic bounded task does not start its own thread. `Extension.background_tick()`
  returns one no-argument callable (or `None`); `attach_background` runs all of them before each tick of the one background service
  (`BackgroundService(before_tick=...)`), after the writer-admission fence of the extension's own writes. A failing callable never
  skips the others and the first error is recorded by the service.
- **Native connections**: an extension that provides an MCP connection names the roles it serves (`NativeConnection.roles`); the
  default role packages do not name any extension.

The reference contract is `execution/EXTENSION-CONTRACT.md` of the v1.3 workspace; each package documents its own configuration
in its README.
