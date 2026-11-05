# Offline authored-layout upgrade

`upgrade_prepare` recognizes installed inventory schema 1 and returns an
`agentic_layout_v2` plan with `schema_version: 2`. Inspect and retain the complete
plan before applying it. For a current schema-2 installation, the same entry point
returns an `agentic_managed_update_v2` plan for ordinary supplier updates; that
separate path is described below. Neither path silently resets an installation.

The digest is SHA-256 of the canonical JSON plan before adding `plan_digest`.
The plan records binding bytes' digest, original binding/source identity,
verified export digest, unchanged optional selection, root/state device and
inode, Git HEAD, index digest/entries and dirty paths. Runtime inspection reports
its schema, unfinished-work conflicts, and the complete database byte length and
SHA-256, with a 1 GiB inspection ceiling. A nonempty runtime WAL prevents row
inspection; normal offline shutdown, checkpoint or recovery must occur outside
preparation before replanning. Preparation does not perform those operations.

`path_map` maps each installed legacy path to one or more target paths; split
manifests have multiple targets. `ref_map` maps stable old entry refs to target
refs. `files` contains target bytes and SHA-256 hashes; `rewrites` records the
old/new hash of each rewritten structural manifest. `target_inventory` is the
old inventory with destinations and canonical skill refs updated; source records,
original supplier commits and source paths remain unchanged. Locally absent
imported or seed files are listed separately and stay absent. A generic preference
seed is included only when no singleton exists; its export provenance is recorded
separately. Existing preference bytes retain their original origin.
Legacy plans also record the path, digest, size and mode of every existing file
in the five new areas, including an empty mapping when there are none, as well as
every legacy package file. Older plans lacking this evidence require preparation
again.

Saved approvals, execution snapshots, change history and consumer cursors are not
rewritten. Saved pre-merge approved plans require owner replanning under current
rules before use. An unsettled merged application must finish its post-merge
checks or failure handling before relocation; it cannot be replanned after merge.
Pending authoring/publication, unresolved workflow verification, in-flight
invocations, unfinished Git operations, unsafe paths, conflicting destinations,
invalid inventory, missing installed assets and unresolved catalogue references
make the plan incomplete. A saved workflow with running control alone does not
prove active work. No profile, credential, native session or capability is changed.

Run `upgrade_prepare` against the verified export with the instance fully offline,
review its complete plan, then call `upgrade_apply` with that exact plan and the
original host-installer attribution. The application retains the original binding
and intent privately, relocates the reviewed files, publishes one Git commit,
reconciles the existing runtime and then activates the validated binding. It does
not change native profiles, sessions, grants or the database inode. The existing
change feed records any resulting authored change.

If application is interrupted, keep the original plan and attribution and invoke
`upgrade_apply` again while the instance remains offline. Once the intent is
captured, the original export is no longer needed. Normal installed open refuses
until activation is durably complete; it does not attempt recovery. An altered
binding, receipt, post-state or conflicting intent refuses without repair or
rollback. Do not replan over partial effects. Completed retries return the original
result without repeating the migration, even after ordinary later edits and commits.

Normal prompt refresh checks the same completed activation evidence before reading
the current catalogue revision. The shared handoff waits at most four seconds for
a current response; if it cannot verify and deliver within that bound, it reports
pending rather than using an older response. Generated native bootstrap hooks have
at least six seconds available for the wait and cleanup.

Empty legacy `packages/` directory scaffolding may remain and is inert. It is not
removed automatically; any legacy file, symlink, special node or unsafe directory
still blocks normal use. Filesystem support for unnamed temporary publication,
exclusive linking, atomic replacement and file/directory fsync is checked in
private disposable probe directories before publication. The per-effect identity
checks still apply. The read-only prepare does not probe or change the filesystem.
Git preflight rejects unsupported index features, filters, noncanonical HEADs and
unfinished Git operations. Existing inspection size and path limits apply.

## Ordinary managed updates after relocation

For an installed five-area layout, prepare against the verified new export and
retain the exact `agentic_managed_update_v2` plan. The plan compares previous
supplier hashes, current authored files and the proposed supplier files. It
preserves user preferences, custom plugins, explicit custom source inventories
and local edits where the supplier has not changed the same content. Conflicting
local and supplier edits require deliberate resolution before application.
The change-feed inventory includes files outside the catalogue areas; those rows
remain part of the full runtime preservation proof, never supplier edit targets.

Apply the saved plan only under exclusive offline ownership. The private installer
may update protected supplier bundles; this does not enable protected editing
through public agent tools. A content update produces one Git commit and one
attributed change-feed event, then publishes its verified binding. A binding-only
update changes provenance without an authored commit or event. An exact no-op
writes no binding, history or runtime state.

The prior binding, plan/source evidence and completion journal remain private
instance records. Resume an interrupted update with the original plan and
attribution; after capture it does not depend on retaining the export directory.
Ordinary open refuses unfinished updates. Completed retries verify retained
ancestry and allow legitimate later authored changes; they do not reapply the old
content. An unrecorded directory left between creation and its journal write is
an ambiguous conflict: preserve it for explicit resolution rather than adopting
or deleting it automatically. No automatic rollback is performed.

The legacy-learning-removal coordinator uses this same updater after layout and
module-state prerequisites are complete. Its bounded public plan contains the
validated plan identity, exact file-change metadata and hashes of larger records,
not authored file bodies. It verifies the real private completion proof, unchanged
unrelated module records and exact file effects before its separate database
transition. Context and Operations migrations retain their own contracts.
