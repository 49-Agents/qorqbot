# Offline procedural learning removal

Fresh installations retain factual Context learning, read-only procedural history and Overseer without the retired
learning library or its seven engine tables. Existing installations need the explicit
`v1.3-legacy-learning-removal-1` transition. Normal startup never migrates a store.

This transition drops exactly seven empty tables, 15 explicit indexes and six triggers. It preserves every row in the five
shared learning tables, including historical deltas; it never converts, applies or purges them. The independent library
repository and historical releases remain intact.

## Prepare

Use the candidate's installed Python outside source checkouts with `PYTHONPATH` unset. Supply the actual canonical
`--instance-root`, `--extensions-config` and explicit `--component NAME=/absolute/path` for `app`, `operations_engine`,
`context_engine`, `dashboard` and `roles` in the verified candidate's exported sources.

Requests, references and saved plans must be private JSON (0600 in an owned 0700 directory). A coordinator request has:

```json
{
  "format": 1,
  "workspace": "/candidate/src",
  "roles": [{"source_id": "learning-applier"}],
  "adoptions": [{"agent_id": "EXISTING-ID", "role": "learning-applier", "expected_revision": 1,
                 "remove_overrides": []}],
  "managed": true,
  "native": [{"agent_id": "EXISTING-ID", "endpoint": "https://existing-service.example",
              "interpreter": "/candidate/venv/bin/python", "versions": null}],
  "context": "/private/current-context-configuration.json"
}
```

Replace all example identities, paths and revisions with current inspected inputs. Include every affected profile and stale
native registration. Only explicitly named retired operation overrides may be removed; other grants/pins/scopes are kept.
Null native `versions` uses the existing local compatibility probe. Null `context` selects normal default Context settings;
use it only when those are the intended runtime settings. Native preparation starts no model or conversation.

The release-reference JSON binds absolute `export`, `manifest`, `selection` paths and their `manifest_sha256` and
`selection_sha256`. Its `components` maps `app` to `qorqut-code:app`, `operations_engine` to `qorqut-operations`,
`context_engine` to `qorqut-context`, `dashboard` to `biz-dashboard`, and `roles` to `agent-roles`. Selected extensions also
need `extension_components`, for example `qorqut_paper` to `qorqut-paper`. Loaded factory/package bytes and wheel origins
are verified independently of those mappings.

Append the common instance/component/selection arguments to:

```text
python -m bazarlyk.bootstrap.learning_removal prepare
  --coordinator-request /private/request.json --release-reference /private/release-reference.json
  --save /private/plan.json
```

Planning is read-only. Review exact identities/schema, empty-table counts, pending work, role/profile adoptions, managed
three-way changes, native refreshes and backup scope. Conflicting owner edits and unknown pending operations block the plan.
Saved factual plans use the actual target Application, document and learning validators without executing them.

For the reviewed Move to project composition with Content/Fastmail/Paper, predecessor digest
`8276e39d7065fc636158dd0a77a51102287028876657737ae10c81f9dc5a4e22` becomes
`0844307b7e31bb8c1a53b53fe8c6bd659a1fa9f3dc522c9c11b41d65bd7997f6`.
Exact schema and extension selection must also match. Earlier CRM/pre-Move schemas and the Agentic module's own state
upgrade are separate prerequisites; they are never bundled into this removal.

## Apply and recover

After separate deployment authorization, quiesce all writers through the current release contract and prevent automatic
predecessor restart. With the same candidate interpreter and common path/selection arguments:

```text
python -m bazarlyk.bootstrap.learning_removal apply
  --plan /private/plan.json --backup /private/new-before.sqlite3 --coordinate
```

One continuous instance owner covers fresh verified database/file backup and baseline A, real role publication/adoption,
managed upgrade, native preparation, baseline B, DDL and completion. Ordinary authority/revision/receipt checks remain.
Exact A-to-B accounting includes module change-feed rows and Git bookkeeping; no table or whole directory is exempt.
The SQL transaction keeps foreign keys enabled, drops children first, changes only the target identity digest and verifies
all retained rows against B before its single commit. A read-only reopen verifies the result again.

Successful preparation remains after SQL rollback to B; it is not a restore to A. Partial preparation keeps earlier
effects and durable stage evidence. Keep the runtime stopped until its configuration is reconciled. Never force imports,
rewrite saved receipts or restart automatically after failure.

An uncertain commit is inspected against durable identity/schema/row evidence. Exact-target retry is a verified no-op
without another backup, publication, adoption, refresh or drop. Corrupt evidence and drift refuse. Never start predecessor
code against the target. An owner-chosen full restore requires matched database, files, binding, roles/configuration and
code from A, with any newer work preserved first. No automatic destructive restore is provided.

The SQL transaction is atomic; the database/files/Git/preparation/deployment sequence is not. Use the current release
mechanism for selection/startup and keep post-start checks read-only. Executable synthetic rehearsals are in
`app/tests/test_learning_removal_preparation.py` and `app/tests/test_learning_removal_migration.py`.
