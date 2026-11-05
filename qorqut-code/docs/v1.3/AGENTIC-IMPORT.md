# Agentic source import

The fresh host builds its inventory from explicitly supplied source components.
The logical source ID `roles` points to the engine's
`src/qorqut_agentic/resources/defaults/`, which supplies the three role manifests
and protected operations and Overseer guidance. A separate roles checkout is not
an active fresh-build dependency. Host-owned assets remain in
`app/bazarlyk/agentic/assets/`; native implementations and authority remain in
host or extension code. The version 2 inventory records physical package paths,
bindings, provenance and hashes; selection or discovery grants no authority.

Fresh initialization installs verified protected resources in `roles/`,
`operations/`, and `overseer/`, imported domain and extension packages in
`plugins/`, and the generic singleton `user-preferences/`. Package `agentic.yaml`
files are manifests within one authored Git repository, not separate repositories.
The public authoring and import APIs can change only plugins and preferences;
the trusted installer writes protected bundles only for a fresh target. This API
restriction is not an OS sandbox. See [fresh installation](../agentic/fresh-install.md)
and [host integration](../AGENTIC-HOST-INTEGRATION.md).

Source inventory and planning interfaces are implemented in
`app/bazarlyk/agentic/inventory.py` and `app/bazarlyk/agentic/plans.py`; they
retain provenance, conflict checks and explicit selection. Existing installations
require the explicit [offline layout upgrade](../../app/bazarlyk/agentic/LAYOUT-UPGRADE.md).
Legacy apply is still in progress; fresh initialization must not overwrite an
existing installation or be treated as its migration.

## Interfaces and receipts

The Python interfaces are `inventory_sources(source_roots, selected_extensions,
custom_sources=(), *, email_guidance_root=None, export_root=None)`,
`build_import_plan(inventory, existing_files=None, previous_import=None)` and
`apply_import_plan(layer, plan, attribution)`. Required source IDs are `host`,
`roles` and `email`; optional IDs include `content`, `content-library`, `paper`
and `fastmail`. Content needs both content inputs. Sources and selections are
explicit: no home-directory discovery or live configuration lookup. For a fresh
export, `roles` points to the agentic defaults root, not the old roles checkout.
Inventory reads manifests and Python ASTs without executing source code.

Planning an update requires the actual current file snapshot and previous import
manifest. It retains local-only files, reports conflicts where affected local bytes
changed, and only applies upstream changes where ownership checks permit. A plan
includes coverage, conflicts, file hashes and digests. Apply refuses incomplete or
conflicting plans and checks the plan digest; public authoring restrictions still
apply, so this is not a protected-bundle installer. A nonempty batch uses the
existing authoring API and returns `result`, `no_op` and `import_manifest`; save
the manifest outside authored content for later updates. Digests are integrity
checks, not authorization.

The local JSON command `python -m bazarlyk.agentic` accepts one request with
`operation` and `arguments`. `inventory_sources`, `build_import_plan` and
`apply_import_plan` use the same named arguments as Python; apply requires an
already opened layer identified by `--root` and `--state`. For example, use
explicit copied roots:

```json
{"operation":"inventory_sources","arguments":{"source_roots":{"host":"/copied/qorqut-code","roles":"/copied/qorqut-agentic/src/qorqut_agentic/resources/defaults","email":"/copied/dependencies/email-management"},"selected_extensions":[]}}
```

Success returns `ok` and `result`; failures return `ok: false` and a structured
`error` with nonzero exit status. Inventory and planning do not open a store.
