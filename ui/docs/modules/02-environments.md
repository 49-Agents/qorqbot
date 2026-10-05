# M02 — Persistent VM and host execution environments

Status: Planned. Owns R03/R04/R14 and machine-surface parts of R06/R13. Depends on M00/M01 contracts; integrates with M03/M06 and M11 installation/backup. True VM is the pending-owner default D02.

Repository: `qorqut-core`. Its image recipe consumes pinned `qorqut-browser` setup assets; browser does not provision agent VMs or own installation-wide settings.

## Outcome and boundary

Provision, inspect, stop, restore and explicitly replace one persistent QEMU/KVM Linux environment per ordinary agent. Onbash selects a host environment through the same application boundary. Environment availability is independent of the current native CLI run.

Use libvirt instead of implementing hypervisor process management. Keep one concrete VM driver and one host target. A generalized multi-cloud scheduler or pluggable hypervisor marketplace is unnecessary.

## Persistent resources

Persist domain UUID, agent ID, environment generation, image digest, MAC/DHCP reservation, root/state disk paths and filesystem UUIDs. Stamp libvirt metadata with installation ID, agent ID and generation. Generated `domain.xml` is recoverable configuration, not a second authority.

Each VM uses an immutable pinned base plus persistent root overlay, a separate ext4 state disk and an exclusive virtiofs workspace. Native HOME/profile/spool remain on ext4. Configure virtiofs shared-memory backing and compatible UID/GID mapping. Startup readiness checks mount identity/content; it must not mask an absent mount with an empty local directory.

Use one installation libvirt NAT network and stable guest reservations. Validate subnet overlap at installation. Guest-local ports repeat across VMs; there is no per-agent public port allocator. Authenticate and verify runner identity after locating it. Never identify a VM solely by an IP or display name.

Keep a distinct machine graphical surface in addition to each agent's dedicated Chrome TigerVNC display. Default VM path is libvirt VNC on a private host Unix socket plus a usable general guest desktop on its virtual display. The image must expose actual desktop/tools, not just a firmware/boot screen; P0 verifies this alongside Chrome isolation. Persist surface/domain bindings and regenerate socket paths on restore. Onbash uses an independently owned local general desktop through M06, never a libvirt console or the owner's existing desktop. No browser-profile cloning or shared display fallback.

## Lifecycle and recovery

Creation persists desired state and a job before external effects. Under a per-agent host-local lock, inspect owned libvirt resources, create only missing matching resources and verify observed state. Preserve the lock across the external call. Recheck generation before publishing results.

A database lease cannot stop a stale external writer. Before allowing another worker to mutate the same environment, ensure the old lock holder/process is gone. A libvirt timeout requires inspection of the domain/resource outcome. Do not blindly create a second domain after an uncertain create response.

Host service restart enumerates and reconnects owned domains without restarting them. Missing contact is `unreachable`, not stopped. A VM reboot changes `boot_id`, ends old process bindings and initiates explicit runtime recovery; it does not erase native history. A VM assigned to an agent never falls back to host execution when unavailable.

Image replacement is a recorded stopped operation: drain/stop, back up compatible state, create a new root overlay, advance generation, boot and verify. Preserve the old root/image for inspected recovery. Do not rebase a used writable overlay onto an unrelated new image. Native/browser state downgrade needs its own compatibility evidence.

Portable restore installs and verifies the exact backing image by digest before opening an overlay. If the data root changed, rewrite only its backing-file locator to that same verified image and regenerate domain/virtiofs paths. Do not treat path relocation as permission to substitute another base. Test restoration under a different absolute data root.

## Implementation sequence

1. **M02.01 P0 rehearsal:** verify virtualization prerequisites and boot two disposable guests with distinct workspace/state disks, fixed internal ports, host MCP access and separate browser/machine graphical surfaces. No owner accounts or existing Qorqut agents.
2. **M02.02 Driver:** implement typed inspect/ensure/stop operations, stable resource metadata, per-agent process locks and bounded libvirt execution.
3. **M02.03 Image recipe:** create a reproducible pinned Linux image with runner, tmux, dedicated Chrome Xtigervnc/Openbox, a separate general guest desktop on its virtual GPU, CLI binaries and native tools. First boot supplies only agent-specific identity/network/mount configuration. Keep display/session-bus ownership separate so applications cannot attach to the wrong display.
4. **M02.04 Reconciler:** connect desired state/jobs with owned-resource inspection, safe retry and observed environment status. Unowned collisions are reported; nothing is adopted by a guessed name.
5. **M02.05 Host target:** implement onbash environment selection and persisted host resource allocation. It uses the same runner code and never borrows Qorqut's live profile/display.
6. **M02.06 Replacement/export hooks:** add explicit stopped image replacement and quiescence interfaces for M09/M11, including base-image dependency inventory.

## Source decision

P03: borrow Qorqut's desired/observed state, exact-generation targeting and external-writer warnings. The Docker implementation, business namespaces, proxy-only business networking and adoption policy are not direct ports to libvirt.

## Tests and acceptance

- Retry/concurrent ensure produces one owned domain and one resource set.
- Inject failure after each network/disk/domain step and inspect/recover without leaking a second identity or deleting retained state.
- Two guests can use the same local VNC/CDP/runner ports and see only their mapped authored workspace through normal paths.
- Browser and Machine resolve to separate displays on the same correct agent; private console reconnection preserves the VM and does not replace Chrome. Restore relocates machine-console socket/domain bindings too.
- Chrome cookies, CLI state, authored files and root-overlay changes survive ordinary VM reboot; host service restart changes no live guest process.
- Missing/wrong mount, stale reservation, wrong agent/generation/boot identity and protocol mismatch block readiness visibly.
- Stop/rebuild/restore preserve history and image pins. Snapshot rollback cannot overwrite newer source lineage.
- Relocated restore resolves every qcow2 backing path and regenerated domain path to verified destination resources; missing/wrong bases fail before activation.
- Host onbash passes shared execution-target contract tests without any VM resources.

**Done:** G0/G2 real-VM evidence is recorded; ordinary operations do not depend on SSH scripts or Qorqut-specific config; backup/replacement semantics are tested before declaring VM persistence complete.
