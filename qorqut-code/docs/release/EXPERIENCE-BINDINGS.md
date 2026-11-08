# Persistent creative-session installation

These are trusted release-worker source features. Merge and activate an immutable,
validated worker release through [INSTALL.md](INSTALL.md) before using them. This
source change does not activate the installed worker, mount storage, update a live
gallery, publish/adopt plugins or change startup AGENTS.md copies.

Participating channel plugins declare the fixed schema-1
`experiences/contract.json` sidecar: channel ID, session format 2,
`binding: experiences/sessions` and
`dataset_layout: plugin-experiences/<channel>/sessions`. Native plugin.yaml remains
unchanged apart from its ordinary procedure declarations. Tool/rule grants and bounded
native skill packaging remain unchanged. Published artifacts contain no growing data.

## Frozen selections and receipts

A plugin component may select `experience_binding` with exactly these fields:

```json
{
  "schema_version": 1,
  "channel_id": "example-channel",
  "data_root": "/absolute/stable/data",
  "dataset_id": "eb4e1f4f-38db-4cb7-9605-d8735482e5a9",
  "dataset_path": "/absolute/stable/data/plugin-experiences/example-channel/sessions",
  "source_view": "/absolute/installed/channel-source",
  "contract_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "receipt_path": "/absolute/installed/channel-source/experiences/binding.json"
}
```

Paths and the all-zero example hash are placeholders, not installation selections.
Planning verifies the real sidecar bytes at the frozen source commit. Dataset identity,
source view, targets and sidecar hash are included in the immutable plan digest.
The source view is an existing approved installed code view outside worktrees,
release roots, business projects, artifacts and packaged skills. Its actual sidecar
must match the staged artifact and frozen hash before provisioning. References to
that view in prepared critic instructions must resolve before adoption is complete.

The canonical plugin_adoption recipe preflights all selected profiles, then invokes
trusted binding support before publishing/adopting/preparing. It provisions the owned
stable dataset marker and session directory, creates/verifies the exact absolute
source-view binding, and writes a plan/source/selection-bound receipt. A nonempty
unowned data path, mismatched marker, ordinary existing binding directory or incorrect
symlink requires reconciliation; nothing is deleted to make it fit. Retry reconnects
the same dataset after an interrupted receipt. Replacement plans can select the same
dataset explicitly and update their receipt without altering unique data.

Provisioning fsyncs each new directory entry's parent before publishing a verified
binding receipt, including the data root and dataset ancestors. It also flushes the
binding's parent on retries of an existing link. A failed directory fsync propagates;
partial creation remains for reconciliation/retry and produces no verified receipt.
Only the selected ancestor chain is visited, and an explicit missing mount anchor
is never created as fallback.

Adoption receipt verification also reads the selected binding receipt, link target,
marker and contract hash on the destination. A stale or missing receipt is not success.
The growing binding stays outside the transferred immutable artifact so its tree digest
and native published package digest remain stable. Uninstall verifies and removes
only the owned binding, with retained-dataset evidence; reinstall reconnects the same
identity. No Git commit is made by runtime writes.

The trusted helper supports:

```sh
python -m qorqut.release_worker.experience_binding --plan PLAN --component CHANNEL --artifact ARTIFACT --operation verify
```

`bind` requires that exact plan's recorded adopt authorization; `unbind` requires its
recorded recover authorization as an explicit uninstall/recovery selection. Neither
command grants publication/profile authority or authorizes new target selection.
Use the stable installed worker environment, never an imported development checkout.
Full plugin transfers separately carry and verify the dataset; a clone or native
procedure package alone is code-only.

## Independent session backup

`installation.session_backup` selects the one supported v1 transport: an already
approved Linux NFSv4 remote mount. It names destination/dataset UUIDs, absolute
mountpoint/dataset root, expected server/export/version, absolute verification file
and its SHA-256, and an absolute backup binding receipt. These effect-bearing fields
are covered by installation_digest and plan_digest. Actual infrastructure selection
and setup remain rollout prerequisites; the recipe never mounts networking.

The infrastructure verification repeats those target fields, names who/when verified
storage independence and survival of gallery-host loss, and references hashed restore
evidence. Evidence reports verified outcome, destination identity and an exercised
host-loss restore. Marker-only or independent=true declarations are insufficient.
Runtime checks Linux NFS/nfs4 metadata, version 4/4.1/4.2, exact mount/server/export,
non-bind root and dataset marker. Local/loopback targets and local interface IP aliases,
nested mounts inside backup generations, missing/nested/changed mounts,
aliases and unsupported identities fail without a local fallback.

The trusted adoption recipe verifies this approved existing target and writes an
exact plan-bound bind-backup receipt. Gallery configuration then references that
receipt and frozen verification hash/plan. The gallery pins opened target directories,
rechecks mount/identity during I/O and publishes complete immutable per-cutoff
generations, hashed inventories and destination receipts. Protection advances only
when source, both archives and that verified backup cover the revision. Old generations
and failed staging are retained; fsync, outages and disk-full remain visible errors.

Focused tests inject mount observations internally into temporary fixtures. All NFS
and host-loss results from such tests are simulated, not evidence of an independently
verified live installation. Real independent backup restore, exact creator transcript
adapter, stable dataset path, gallery write-pause upgrade and selected profile adoption
remain production prerequisites. Storage durability does not erase historical context
gaps. Read the content-creation source session-format-v2 guide for capture and restore.


## Recorded-session format 3 compatibility

The source worker accepts exact schema-1 channel sidecars selecting session
format 2 or 3. The sidecar bytes remain pinned by the frozen contract hash;
provisioning, verification and adoption receipts require a dataset marker with
that exact selected format. Existing format-2 selections keep their behavior.
Unknown formats and extra sidecar fields fail validation.

This support does not migrate an existing format-2 dataset to format 3. A marker
mismatch fails without rewriting the marker, replacing a binding or deleting
history. A reviewed migration must fence old writers, retain prior receipts and
preserve original journal prefixes. Source support does not activate this worker,
provision data roots or authorize plugin adoption; use the installed release
workflow only under the separately approved installation/release scope.

Managed new sessions require format 3 and the exact installed channel binding.
Existing format-2 selections remain valid for their v2 recorder; source-side v3
adoption does not rewrite markers, sidecars or receipts in live installations.
Inventory/fence all old writers, preserve browser drafts and exact journal prefixes
and rehearse a full-copy explicit upgrade before separately approved rollout.
