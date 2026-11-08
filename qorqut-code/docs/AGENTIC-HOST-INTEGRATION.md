# Agentic procedures in the host

The standalone `qorqut-agentic` distribution supplies the catalogue, authoring,
history, change feed and default resources. The host composes the opened layer,
native sessions, authorization and inbox bridge. A verified source export and
fresh initializer seed an empty instance; see [fresh installation](agentic/fresh-install.md)
and [installation](INSTALLATION.md). Opening an existing instance does not
initialize, repair or silently migrate it. The old layout needs the explicit
[offline upgrade](../app/qorqut/agentic/LAYOUT-UPGRADE.md). Source integration
does not migrate an existing installation; an operator must explicitly prepare
and apply the reviewed plan while the instance is offline.

One per-instance authored Git repository at `operations/agentic/` has five areas:
`roles/`, `operations/`, and `overseer/` are protected through agentic editing
APIs; `plugins/` and the singleton `user-preferences/` are editable. Package
folders contain `agentic.yaml` manifests in this one repository, not individual
plugin repositories or submodules. Runtime state is separate at
`operations/.runtime/agentic/`. The trusted fresh installer writes protected
bundles; public editing, import, history and application APIs cannot do so.
This restriction is not an OS sandbox. The default role definitions retain
Ordubashy, global worker and learning applier. Native tool and browser code and
actual authority remain in the host and extensions. Catalogue discovery and
scope do not grant tools, credentials, plugin activation or permissions.

## Manage guidance

Use the existing `agentic-management/manage` guidance and toolkit: list or search
the eligible catalogue, read relevant entries, locate or create a scoped plugin
or skill, and put universal preferences in `user-preferences/SKILL.md` (entry
`user-preferences/preferences`). Inspect hashes, use `authoring.apply` or the
appropriate entry operation, validate with `catalog.validate`, review the diff
and commit with `history.commit`. Use the existing change feed and inbox; do not
invent a separate notification channel. A clear owner instruction may be recorded
immediately within existing authority; an inferred change needs the existing
owner-led review and application. For API signatures see the engine's
`docs/INTERFACES.md`.

Every role receives the complete canonical preferences. The next normal prompt or resume refreshes the managed projection and relevant
catalogue, without restarting sessions, waking paused agents or interrupting an
active turn. Native managed skills use the existing projection. Discovering a
custom plugin does not inject or read all its bodies; agents locate and read
relevant procedural entries. If a
refresh is delayed it remains pending rather than serving stale instructions;
no arbitrary-load latency is guaranteed. A successful save does not prove refresh or delivery, publication or inbox
delivery; report each state separately. A delivery receipt is not proof that a
model followed the guidance. Native orientation changes still require explicit
preparation; see [projection](AGENTIC-PROJECTION.md).

Overseer extracts whole authorized episodes into loose proposals, clusters and
ranks them for owner-led review and application through existing Signoffs.
Rejections persist, Overseer conversations are excluded from learning, and a
post-merge failure waits for owner disposition. Editable changes target preferences
and plugins. Protected or host problems can be recorded in the separate outside
observation store and retrieved only on owner request; they are not automatically
included in the top-five review, applied or turned into development tasks. Context
continues through its separate process.

Recovery requires a consistent capture of the authored Git root, separate module
state and private binding; an Operations-only backup or Git alone is insufficient.
See [Overseer migration](AGENTIC-OVERSEER-MIGRATION.md) for the separate state
transition and [agentic import](v1.3/AGENTIC-IMPORT.md) for source inventory.

## Application interfaces and receipts

Owner-invoked application uses authenticated host operations including
`overseer_application_prepare`, `overseer_application_prepare_merge`,
`overseer_application_publish`, `overseer_application_record_checks`,
`overseer_application_replan`, `overseer_application_recovery_status`,
`overseer_application_recover` and `overseer_application_inspect_satisfaction`.
The existing `overseer_development_task_create`, `overseer_development_task_link`
and `overseer_development_tasks_reconcile` operations remain available; an outside
observation does not automatically create a task. The module also exposes
`overseer.applications.list` and `overseer.applications.save_plan`. Effects recheck
the current registration, authority, admitted run and exact agreement.

Replanning is pre-merge and needs a new plan Signoff; an old answer or candidate
cannot authorize a new revision. Preserve plan revisions, journals, drafts and check
attempts. After merge, failed or uncertain checks leave the merge in place and
require an owner failure Signoff. Its dispositions include retry checks, repair,
revert as later follow-up, leave or later; no check or rollback is automatic.
Acceptance, merge, checks, refresh, native publication and inbox delivery are
separate facts, supported by their own receipts rather than inferred from a save.
