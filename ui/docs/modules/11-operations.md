# M11 — Installation, backup, observability and release quality

Status: Planned. Owns R14 and integrated acceptance. Contributes from Phase 0; completion depends on all modules.

## Outcome and boundary

A fresh installation can run the pinned backend/guest image, preserve active agents across host-app updates, diagnose failures, back up the advertised data and restore it without the old Qorqut installation. Operational truth and test evidence must match what the software claims.

Keep one host service plus libvirt and guest runner services. No new queue/search database server or mandatory hosted telemetry. Installation scripts configure prerequisites and units; application startup does not silently migrate an unknown database, scan old Qorqut state or launch every agent.

## Installation and supervision

The installer validates virtualization permissions, libvirt/QEMU/virtiofs/image tools, local filesystem suitability, disk headroom, subnet collisions and listen addresses. Persist the installation ID, image/package pins, network configuration and onbash ID. The current machine's prerequisites remain unverified until P0.

Pin a stable Linux guest image recipe and all guest-native tools by verified versions/digests. Prefer a maintained LTS distribution with Python 3.12 availability; finalize the exact base in P0. Keep image construction separate from per-agent provisioning. Per-agent first boot receives only identity, mount/network and credential material.

Image/recovery evidence includes a usable general machine desktop and its private libvirt VNC binding separately from the dedicated Chrome TigerVNC surface. Onbash owns two distinct local desktop bindings. Include their configuration/ownership records and conversation/file-reference records in full backups; regenerated restore bindings must not reuse stale sockets, viewers or input epochs. Raw archive and source manifests remain independent of the Chat UI.

Systemd supervises the host and runner; libvirt owns VM lifecycle. Independently supervised agent runtime units/cgroups contain agent-owned tmux/recorder/native trees and separate desktops, outside the runner/host units and without their restart coupling. Daemonization alone does not escape a systemd cgroup. Restarting the actual host/runner service must preserve these process identities. A native CLI unit does not automatically restart a model after death. Desired environment state and any configured runtime recovery policy are explicit records.

Migration is an explicit maintenance operation with a pre-migration backup and supported schema checks. A host binary rollback cannot use an unsupported database schema. Guest protocol compatibility is checked before adopting a new host/runner version. An update that needs native hook reload must say so; rewriting config does not prove an already-running client reloaded it.

## Health and observability

Expose metadata metrics/logs for:

- Agent/environment/run counts and current desired/observed states.
- Runner contact age, source event cursor and native binding confidence.
- Pending/uncertain input attempts, oldest queued message and explicit handling lag.
- Local capture backlog bytes/age, host durable watermarks, parsing lag/gaps and attachment gaps.
- SQLite busy/wait/hold time, job age/retries and stalled reconciliations.
- Disk usage/headroom per archive/spool/state disk and backup coverage/recovery point.
- Guest protocol/image/CLI/browser versions and mismatch diagnostics.

Use stable IDs and sanitized typed reasons in ordinary logs. Raw transcripts, message bodies, browser credentials and tool arguments do not belong in general logs/metrics. Detailed private artifacts remain retrievable through their existing records. No transcript content leaves the installation for external indexing or telemetry by default.

Freshness and readiness are measured, not inferred from a configured file. An alive HTTP endpoint does not prove native CLI readiness, authenticated browser login or healthy archival capture.

## Capacity and admission

Give environments configurable CPU/memory/disk budgets and measure useful defaults in P0/acceptance. Capacity is a scheduling/reliability condition, not a permission gate. If capacity is unavailable, accepted create/start work waits visibly rather than overcommitting until the host becomes unstable.

Define high/critical watermarks with enough reserved space to persist error receipts and pause cleanly. Backpressure first stops new launches/automated input, then pauses affected managed producer subtrees when capture cannot continue. Keep recorder/runner runnable to drain and reconcile. Preserve all committed bytes. Recovery resumes from stored cursors and verified bindings; it does not reset the archive or silently prune old records.

Bound worker concurrency and long reads. Measure directory/status latency and message/capture catch-up with synthetic history, plus memory/CPU under multiple real guests. Publish tested capacity, not an unmeasured “supports hundreds” claim.

## Backup products

### Online archive backup

Use SQLite's backup API for a consistent host snapshot, then copy the immutable chunk/attachment/context-packet inventory referenced by that snapshot. New capture can continue because archived objects are not automatically deleted. Validate hashes and record snapshot/event/archive high-water marks. This backup does not promise a complete VM/browser/authored-workspace image.

### Stopped full backup/export

1. Record the included agent/installation set and enter a persisted maintenance/quiescence operation.
2. Stop admitting new API mutations/input to included mutable resources; allow observation and required drain/ack paths.
3. Stop managed native runs and browser automation/Chrome gracefully, perform final source drain and wait for host archival ACK.
4. Stop included guests and verify domains are off. For onbash, stop and verify the entire owned runtime/desktop cgroups, including detached descendants, and cooperating file writers. A process-group check alone is insufficient. Unresolved external writers prevent a fully consistent claim.
5. Snapshot/export the corresponding central records, authored files, raw inventory, root/state disks/native HOME, runner/spool receipts and exact base-image dependencies. Onbash's separate `runtime/spool.sqlite3` must be included through a consistent SQLite backup/checkpoint, not a main-file copy that omits WAL data.
6. Write a versioned manifest with sizes/hashes, state/capture gaps, pins and the actual recovery point. Incomplete drain or unknown writers cannot be labeled a fully consistent backup.
7. Verify the bundle and restore service admission. Restarting agents after backup is an explicit saved choice; do not add an unrequested model turn.

Do not copy live qcow2 files and call them consistent. Shared cross-agent records require a well-defined installation snapshot or agent export slice as specified in M09. Backups remain outside Git; storage encryption/destination configuration is an installation option, not a new secret-management service.

## Restore and upgrades

Restore first validates manifest/schema/image/object hashes into a staged data root. Rebuild derived indexes from the raw archive and compare expected counts/hashes. Install exact base-image dependencies, relocate backing filenames only to those verified images and regenerate destination domain/mount paths. Reissue active transport identities and assign a fresh recovery epoch, preserve original history attribution, and prevent old live sources from writing into restored coordinates. Do not run two active installations with the same writable agent identity unknowingly.

Full-installation restore activates only into an inactive destination after the prior instance is stopped and identity collisions are resolved. Individual-agent import can merge into an existing installation only for an unoccupied identity using M09's journal/provenance rules. A clone receives new current identity and explicit historical provenance; cloning onbash as another host agent is rejected. Software/profile downgrade after new writes cannot be repaired by silently restoring an old state disk and discarding work.

A restored backup is a historical checkpoint, not proof of which effects later occurred elsewhere. Mark unfinished imported/restored external-effect jobs and deliveries held with `recovery_review` reason. Reconcile against retained runner receipts and actual resources before release; possibly submitted input remains uncertain. Missing post-snapshot evidence cannot justify automatic replay. Existing typed operation/delivery reconciliation records any explicit retry and its risk. Normal service restart still uses ordinary persisted receipts; it does not create a new recovery epoch. Online archive restore is read-only by default and never reactivates snapshotted operational queues.

## Implementation sequence

1. **M11.01 Early tooling:** deterministic fault hooks, temporary roots, isolated tmux/VM fixtures and prerequisite diagnostics.
2. **M11.02 Installation/image pins:** minimal service units, reproducible image recipe, schema command and version inventory.
3. **M11.03 Health/admission:** metrics, typed diagnostics, queue/capture limits and tested disk-pressure pause/resume.
4. **M11.04 Backup/restore:** implement the two explicit products and validate independently; integrate portable M09 exports.
5. **M11.05 Upgrade rehearsal:** host/runner/native version changes while retaining agents; explicit native reload requirements, process-cgroup survival, recovery-epoch fencing and supported rollback boundaries.
6. **M11.06 Release evidence:** run the acceptance matrix, record measured capacity/limits and publish install/recovery/operator guides with a named release.

## Acceptance and done gate

Use [ACCEPTANCE.md](../ACCEPTANCE.md) as the integrated authority. Tests cover kill/restart at every durable boundary, unavailable runner/network, disk full, schema/version mismatch, stale process identity, failed browser/setup, stopped full restore and online archive reindex.

**Done:** G6 passes on a fresh host or equivalent isolated installation. No Qorqut services/data are required. Evidence distinguishes unit fixtures, real processes/VMs/browsers and real provider behavior, and known incomplete outcomes remain visible.
