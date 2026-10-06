# M02 Phase-0 prerequisite evidence

Read-only exploration on **2026-10-07**, branch `codex/environments-r1`, base
`db25fc97a4127f8f46f3092ed96578bcdba685ce`. Initial Git status was clean.
Scope: host prerequisites and pinned-source facts only. **The current account
and installed tooling do not permit the planned KVM/libvirt two-guest rehearsal.**
This completes the bounded exploration, not M02 P0 or gates G0–G6.

The reference is the base revision's `BUILD-EXECUTION.md`, `IMPLEMENTATION-PLAN.md`,
`CONTRACTS.md`, `PORTING.md`, M02, ARCHITECTURE storage/network sections, and
D02/D03; these seven files matched the supplied read-only coordination worktree
byte-for-byte at 20:58:45Z. M06's owning `qorqut-browser/docs/BROWSER-PLAN.md` and
the relevant acceptance rows were also read. D02 remains a proposed default
awaiting owner clarification. No VM/container choice, installation recipe,
network selection, resource allocation, or architecture change was made.

## Host observations

All times below are UTC. Probes used Python 3.12.3 stdlib, version commands,
read-only systemd queries and kernel/package metadata. Access results apply to
this executor's effective account, not to a future privileged libvirt service.

| Time | Command / read method | Observed result |
| --- | --- | --- |
| 20:57:55Z | `platform.freedesktop_os_release()`, `platform.release()`, `platform.machine()` | Ubuntu 24.04.5 LTS; kernel `6.8.0-138-generic`; `x86_64`. Hostname omitted. |
| 20:57:55Z | `os.cpu_count()`, `os.sched_getaffinity(0)`, filtered `/proc/cpuinfo` flags | 8 logical CPUs; affinity permits 8. All 8 CPU flag records have `vmx`; none have `svm` or `hypervisor`. These flags do not prove a successful KVM launch. |
| 20:57:55Z | `/proc/modules`, `stat('/dev/kvm')`, `os.access(..., effective_ids=True)` | `kvm_intel` and `kvm` loaded. `/dev/kvm` is a character device, mode `0660`, group `kvm`. Effective read and write access both **false**; account not in that group. No KVM open, ioctl or guest creation attempted. |
| 20:57:55Z | Relevant group lookup only | `libvirt` and `libvirt-qemu` groups absent. No account/group changes. |
| 20:58:45Z | `systemd-detect-virt --vm` and `--container` | Both printed `none`, exit 1 (no virtualization detected by this utility); not a hardware certification. |
| 20:57:55Z | Selected `/proc/meminfo` fields | Total 65,625,380 KiB (62.585 GiB); free 897,808 KiB (0.856 GiB); available 12,899,928 KiB (12.302 GiB). Swap total 8,388,604 KiB; swap free **92 KiB**. This is a changing shared-host snapshot, not a two-VM memory budget. |
| 20:57:55Z; 20:58:45Z | `os.statvfs()` / device comparison; `findmnt -n -T <path> -o FSTYPE,TARGET` | Checkout, assigned task-state directory and `/var/lib` parent are on the **same ext4 `/` filesystem**. Total 501,730,279,424 bytes (467.273 GiB); available to an ordinary account 145,552,896,000 bytes (135.557 GiB); free including reserved blocks 171,114,622,976 bytes; 25,660,404 available inodes. Do not sum these three samples. |
| 20:57:55Z | Effective directory write-access checks | Checkout and task-state directory writable; `/var/lib` itself not writable. ARCHITECTURE specifies `<data-root>` without an absolute destination. No installation data root was supplied, chosen or discovered from live configuration; future data-volume capacity/access remain unverified. |
| 20:57:55Z; 20:58:45Z | `/proc/1/comm`, `/run/systemd/system`, `systemctl --version`, `systemctl --user show --property=Version` | PID 1 is systemd, runtime directory exists, version `255.4-1ubuntu8.17`; version build reports unified hierarchy. System-manager unit reads and user-manager version read succeeded; user bus socket exists and has effective read/write access. No unit started or restarted. |

### Tools and service access

At 20:57:55Z, `shutil.which()` found **none** of:
`qemu-system-x86_64`, `qemu-kvm`, `qemu-img`, `qemu-nbd`, `virsh`,
`virt-install`, `virt-host-validate`, `virtiofsd`, `guestfish`, `virt-builder`,
`virt-make-fs`, `virt-customize`, `cloud-localds`, `genisoimage`, `xorriso`.
`virtiofsd` was also absent at `/usr/libexec/virtiofsd`,
`/usr/lib/qemu/virtiofsd`, and `/usr/lib/virtiofsd`. This is a PATH/standard-location
check, not a search of every filesystem or a claim about remote hosts.

`dpkg-query -W -f='${binary:Package}\t${db:Status-Abbrev}\t${Version}\n'`
queried `qemu-system-x86`, `qemu-utils`, `qemu-kvm`, `libvirt-daemon-system`,
`libvirt-daemon`, `libvirt-clients`, `virtinst`, `virtiofsd`, `libguestfs-tools`,
`cloud-image-utils`, `genisoimage`, `xorriso`, and the installed packages listed
below (plus `systemd-container` and `chromium`). No QEMU/libvirt/virtiofs/image-tool
entry was installed. Exit 1 reflected missing package records; `xorriso`
and `systemd-container` were `un` (not installed), not installed versions.
No guest-image inventory or digest was provided or read from operational data.

| Executable / package | Observed version | Evidence time |
| --- | --- | --- |
| `/usr/bin/python3 --version` | `3.12.3` | 20:57:55Z |
| `systemctl --version` | systemd `255`, package `255.4-1ubuntu8.17`; `systemd-run` present | 20:57:55Z |
| `findmnt --version` | util-linux `2.39.3`, package `2.39.3-9ubuntu6.6`; `blkid`, `unshare` present | 20:57:55Z |
| `mkfs.ext4 -V` (version only) | mke2fs `1.47.0`, package `1.47.0-2.4~exp1ubuntu4.1` | 20:57:55Z |
| `ip -Version` | iproute2 `6.1.0`, libbpf `1.3.0`; package `6.1.0-1ubuntu6.4` | 20:57:55Z |
| `tmux -V` | `3.4`, package `3.4-1ubuntu0.1` | 20:57:55Z |
| `google-chrome --version` | `153.0.8010.36`, package `153.0.8010.36-1` | 21:00:09Z |
| `Xtigervnc -version` | TigerVNC `1.13.1`, package `1.13.1+dfsg-2build2`; `Xvnc` also on PATH | 21:00:09Z |
| `openbox --version` | `3.6.1`, package `3.6.1-12build5` | 21:00:09Z |

All listed version commands exited 0. `chromium` and `agent-browser` were not on
PATH. Installed host desktop binaries are not a pinned guest image, a verified
browser MCP installation, or evidence of an owned onbash desktop.

At 20:58:45Z, `systemctl show <unit> --no-pager
--property=LoadState,ActiveState,SubState,UnitFileState` returned `not-found`,
`inactive`, `dead` for all queried libvirt units:
`libvirtd.service`, `libvirtd.socket`, `libvirtd-ro.socket`,
`virtqemud.service`, `virtqemud.socket`, `virtqemud-ro.socket`,
`virtnetworkd.service`, `virtnetworkd.socket`, `virtstoraged.service`,
`virtstoraged.socket`, `virtlogd.socket`, `virtlockd.socket`.
The queries exited 0; that does **not** mean the units exist or work.

`/run/libvirt/{libvirt-sock,libvirt-sock-ro,virtqemud-sock,virtqemud-sock-ro,virtnetworkd-sock}`
and the current account's standard runtime `libvirt/{libvirt-sock,virtqemud-sock}`
were absent. Libvirt API access, domain capabilities and storage/network pools
were therefore not verified. No connection was attempted that could activate a
session daemon.

## Network constraints, with identifying details omitted

At 20:59:11Z and 21:00:09Z, read-only `ip -j -d link show`,
`ip -j -4 route show table all`, `ip -j -6 route show table all`,
`ip -j -4 addr show`, `ip -j -6 addr show`, and the corresponding `rule show`
commands were parsed in memory. The report retains only address-space classes,
prefix lengths and interface kinds; no assigned IP, gateway, MAC, interface name, peer
identity or complete personal network configuration is published.

- Observed interfaces: one up Ethernet, one tunnel, two bridges (one up, one
  down), three up veth interfaces and loopback.
- Existing IPv4 bridge routes occupy **one /16 and one /28 within
  `172.16.0.0/12`**. A targeted redacted recheck at 21:03:58Z confirmed the down
  bridge has the /16 route and the up bridge has the /28. Both constrain overlap.
  The exact narrower prefixes are intentionally omitted; this does not imply
  that the whole /12 is occupied or that any omitted subnet is free.
- A policy-routing table contains ten tunnel /32 routes within `100.64.0.0/10`.
  IPv4 has seven rules, including three using fwmarks and a non-main table.
  Checking only the main table would miss these routes.
- IPv6 includes a tunnel ULA /48 and /128 routes, link-local /64 routes, a
  non-ULA unicast /64, and a default route. IPv6 has six rules, three with fwmarks.
  IPv4 also has a default route and a non-RFC1918/non-CGNAT interface /32.
- `/proc/sys/net/ipv4/ip_forward` and `/proc/sys/net/ipv6/conf/all/forwarding`
  both read `1`. Forwarding alone does not establish usable NAT, DNS or firewall
  access for new guests; none of those paths was exercised.

The first namespace-comparison probe at 20:58:45Z failed with `PermissionError`
reading `/proc/1/ns/net`; the sanitized network probe was rerun without requiring
that read. Thus observations are limited to **this executor's current network
namespace**, whose equivalence to PID 1's namespace was not verified. No other
namespace was entered. No candidate subnet was selected or tested for exact
overlap. Disconnected networks, future VPN routes and libvirt inventory are not
covered by this snapshot.

At 21:01:23Z, `ss -H -ltn` exited 0; filtering its output to the planned numeric
ports found no TCP listeners on `8091`, `8092`, `5901`, or `9222` in this namespace.
No connections or bind probes were made. This is not a reservation, proof of
future availability, or proof that two guests can repeat those ports. M02's
private machine-console Unix sockets and onbash's persisted distinct bindings
were not allocated or exercised.

## Pinned source facts and reuse limits

Read only the supplied core release source rooted at
`qorqut/components/core/e71fd1e527368bfd41b9815126f4bf33452aeebb/source`.
References below are relative to that root; line numbers refer to those exact
files. P03/P09 are the relevant entries in PORTING.md. No Qorqut code was
imported, executed, copied or adapted, and no live database, deployment
configuration, credential, profile or native store was inspected. No dashboard
code adaptation is part of this assignment.

| Source reference | Observed mechanism and boundary relevant to M02 |
| --- | --- |
| `bazarlyk/sandbox/state.py:1–13,127–187,253–280` | Desired/observed generation and an expiring `lease_fence` govern publication. Replacement invalidates leases while retaining the old observation; ready/failure writes check current fence and generation. The source explicitly says a database lease cannot stop an expired process from changing Docker. This is a publication fence, not sufficient external-writer fencing. |
| `bazarlyk/sandbox/lifecycle.py:118–210` | A per-business nonblocking `flock` stays held across reconciliation, inside an installation writer lock. Intent/lease commits precede external driver work; external calls do not hold a SQLite transaction. Missing config stays pending; loss of lease is raised; ready requires a verified driver result. Business/bootstrap/adoption machinery is source-specific. |
| `bazarlyk/sandbox/runtime.py:157–214,364–397,445–493` | Ownership derives from installation-root hash plus **business ID**, not agent ID. The concrete driver uses Docker agent/browser/gateway containers and business proxy networking. Pinned-image checks occur before draining replacements; stop invokes the browser hook, stops owned containers and reinspects running/restarting state before removal. Mixed generations/configuration drift fail visibly. Workspace/HOME/profile use host bind mounts. This is not the proposed libvirt, qcow2/ext4/virtiofs or VM-console implementation. |
| `bazarlyk/sandbox/terminal.py:34–40,81–128`; `bazarlyk/sandbox/lifecycle.py:212–244` | Missing sandbox intent returns `None` for legacy host fallback; existing non-ready intent fails closed. Ready resolution checks exact container ID, generation, fingerprint, network membership and running state. Host rollback is a separate explicit stopped operation. The new M02 contract instead explicitly selects onbash as host and forbids unavailable VMs from falling back to host. |
| `tools/browser/desktop_session.py:236–327,376–440` | Collision checks cover displays, ports, profiles, Xauthority and VNC-password paths. Preflight refuses existing live resources and requires an existing owned profile/password path; it never deletes Chrome's profile lock. Startup creates a private session bus, distinct child process groups, TigerVNC, Openbox and fixed-profile Chrome with loopback CDP. These are desktop mechanisms, not a general VM machine desktop or mount-identity check. |
| `tools/browser/desktop_session.py:344–366,443–487` | Shutdown orders Chrome before window manager, Xvnc and D-Bus. Group cleanup keeps the child leader unreaped and refuses stale IDs after ownership loss; SIGTERM may escalate to SIGKILL. Ordering gives Chrome an opportunity to flush; it does not itself verify cookie durability, and escalation is not a successful-flush guarantee. |
| `bazarlyk/browser/isolated/session.py:109–140,164–199,249–305,374–440`; `bazarlyk/browser/isolated/processes.py:34–83` | Separate legacy implementation verifies process/listener/namespace bindings, withdraws readiness, attempts CDP `Browser.close` on the owned listener, then stops Chrome before other children. CDP-close exceptions are suppressed before bounded signal fallback; ownership loss is surfaced. `prepare_paths` creates profile/download paths, which cannot prove the future intended state mount exists. Its `PrivateNetwork=yes`, proxy gateway, business assignment and core database dependencies are not the planned VM/host split. |

These observations support PORTING.md's narrow mechanism reuse only. They do not
prove that a systemd service restart preserves desktop/runtime cgroups, that a
browser profile survived a reboot, or that machine and browser displays are
independent. No source tests were run and no source success is counted as new
product acceptance.

SHA-256 fingerprints, computed read-only at **21:00:36Z**:

| Source file | SHA-256 |
| --- | --- |
| `bazarlyk/sandbox/lifecycle.py` | `23d80ccf99be04519031cbcb9d8238f267f42e7c752f88c5d3904f259cdd9121` |
| `bazarlyk/sandbox/state.py` | `50fa08a340dae83af1929db3d656dcb70b8250ceb61ab4a37319ccbe939da260` |
| `bazarlyk/sandbox/runtime.py` | `8c58f638b0947169943b8a1293e29d486d4e4d29e0a5e0f01da67edb951f55f9` |
| `bazarlyk/sandbox/terminal.py` | `929051d9c0fa4aee60da7a3de87becffbaafd9b9294758376b334ee42dce65bc` |
| `tools/browser/desktop_session.py` | `c77e41e10934ed1f9e33a5b36fc4ccbb50e548b889da142e31a3948d7d21c085` |
| `bazarlyk/browser/isolated/session.py` | `0a82dbf99f723bc476a4141bad3b5d78bce10127bb156a14935cc013b6d21d81` |
| `bazarlyk/browser/isolated/processes.py` | `ec792967190ab5308bcb1ff398b03b29668165ee39274626226a7c44b2ef7c1d` |

## Readiness disposition and unperformed acceptance

**Unavailable now:** current-account direct KVM access; expected QEMU/libvirt,
virtiofs and image tools; queried libvirt services/sockets. **Unverified:** a
pinned guest image and its independent core/browser installation, intended data
root and capacity, virtiofs memory backing/UID-GID mapping, selected private
network and host MCP reachability, general guest desktop and independent onbash
resources. The low free RAM and nearly exhausted swap are observations; no
workload sizing or admission decision was made.

| Existing requirement | Status in this assignment / exact missing evidence |
| --- | --- |
| M02.01 / A02.1 two guests | **not_run**: no domain boot, separate identities, immutable base/root overlays, independent ext4 state disks or exclusive virtiofs workspaces. No guest mount/content/UID verification or guest-to-guest file/profile/native-store isolation check. |
| M02.01 / A02.1 network and ports | **not_run**: no installation NAT/DHCP reservation, repeated guest `8091`/`8092`/`5901`/`9222`, authenticated runner identity, HTTP/WS access, host core MCP reachability, or separate native core/browser MCP discovery. |
| M02.01 / A06.6 Browser and Machine | **not_run**: no dedicated guest TigerVNC Chrome plus distinct usable guest desktop, private libvirt console socket routing/reconnect, exact surface targeting, or independent host onbash desktop. A firmware screen or host-installed VNC binary would not satisfy this. |
| A06.1 / A02.2 persistence | **not_run**: no synthetic-cookie isolation/restart test, VM reboot, authored/root-overlay/native-state persistence, boot-ID change, or actual core/runner/browser-service restart with unchanged managed runtime processes. |
| A02.3 readiness rejection | **not_run**: no missing/wrong state/workspace mount, stale address, wrong generation/boot identity, recycled process binding, or protocol mismatch injection. |
| A02.4 / M02 replacement and restore | **not_run**: no concurrent ensure/crash/uncertain-create/stop/rebuild rehearsal; no verified-image stopped restore under a different data root, backing-path relocation, or stale source-lineage rejection. |
| A02.5 host target | **not_run**: no separately owned onbash HOME, profile, service ports, capture or shared execution-target contract rehearsal beside VMs. |
| Remaining G0/G2 dependencies | **not_run by this lane**: real recorder/tmux rehearsal, native adapter/source compatibility, stopped backup/restore and cgroup producer-control evidence. Other actors' results are not asserted here. |

No guests, disks, images, networks, listeners, displays or services were created;
no packages were installed, no sudo used, and no owner accounts or running
sessions were used as fixtures. No framework tests are needed for this factual
report. All gates remain pending.

Report validation at 21:04:59Z: `git diff --cached --check` passed. A stdlib
`hashlib.sha256` comparison verified all seven reported source fingerprints;
Git checks confirmed the assigned base, this file as the only staged path and
no unstaged tracked changes. These are document/provenance checks, not runtime
acceptance tests.

Questions retained for Manager/owner before dependent work: **Which execution
substrate is authorized after the open D02 discussion? What exact installation
data root and execution/network context are authorized for a later rehearsal?**
Neither decision blocks completion of this evidence-only assignment, and no
answer or authorization is inferred here.
