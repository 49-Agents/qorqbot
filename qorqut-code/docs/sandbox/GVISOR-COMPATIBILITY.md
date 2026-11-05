# gVisor compatibility probe

This is a bounded compatibility decision for the disposable business
workstation and browser images. It does not install a runtime, change Docker's
daemon configuration, configure networking, or authenticate a model or browser
account.

The probe used gVisor point release `release-20260907.0`, downloaded as the
official x86_64 `gvisor-x86_64.tar.bz2` asset with its published `SHA512SUMS`.
The checksum was verified before extraction. The release contains `runsc` and
the adjacent `gvisor-bin/` sidecars; they must remain together. The host had
`runc` but no `runsc` on `PATH`, Linux `6.8.0-139-generic`, and an ext4
business-data filesystem. No quota change was made.

The upstream procedure used here is the [OCI quick start](https://gvisor.dev/docs/user_guide/quick_start/oci/): export the exact image rootfs,
run `runsc spec`, make a restrictive `config.json`, and invoke `runsc run`.
The [installation guide](https://gvisor.dev/docs/user_guide/install/) documents
the release bucket, checksum files, and the required adjacent sidecars.

## Reproduce

Run from a temporary directory and keep the extracted runtime and OCI bundle
outside the repository. Replace `IMAGE_DIGEST` with the exact image selected by
the lifecycle test. The export is disposable and has no host mounts or network.

```sh
set -eu
release=20260907.0
arch=x86_64
curl --fail --location --output gvisor-x86_64.tar.bz2 \
  "https://storage.googleapis.com/gvisor/releases/release/${release}/${arch}/gvisor-x86_64.tar.bz2"
curl --fail --location --output SHA512SUMS \
  "https://storage.googleapis.com/gvisor/releases/release/${release}/${arch}/SHA512SUMS"
grep ' gvisor-x86_64.tar.bz2$' SHA512SUMS | sha512sum --check -
tar --extract --bzip2 --file gvisor-x86_64.tar.bz2

container=$(sudo -n docker create --network none IMAGE_DIGEST /bin/sh)
mkdir rootfs
sudo -n docker export "$container" | sudo -n tar -xf - -C rootfs --same-owner --same-permissions
sudo -n docker rm "$container"
./gvisor/runsc spec -- /bin/sh
# The generator installs the fixed command, uid/gid 1000 with no supplementary
# groups, noNewPrivileges, empty capabilities, read-only root, bounded memory /
# pids, a private network namespace, and only in-memory tmpfs mounts.
python3 /path/to/biz-core/tools/business_sandbox/gvisor_bundle.py \
  --profile workstation --config config.json
sudo -n /usr/bin/env RUNSC="$PWD/gvisor/runsc" BUNDLE="$PWD" \
  RUNSC_ROOT="$PWD/runsc-root" RUNSC_PROFILE=workstation \
  /path/to/biz-core/tools/business_sandbox/gvisor_probe.sh
```

The helper checks those confinement properties and compares the complete
generated command, environment, and working directory with the selected fixed
profile. It requires the pinned runtime version and applies a 120-second
wall-clock limit. It accepts no target,
mount, network, or command-line payload. For a browser smoke, the bundle must
also provide `/dev/shm` as a writable bounded tmpfs; Chrome otherwise aborts
before exercising its sandbox. The probe used a 64 MiB `/dev/shm` and 64 MiB
`/home/agent` tmpfs, a 512 MiB memory limit, and 256 pids. Workstation used a
128 MiB `/tmp`, 256 MiB memory limit, and 128 pids. Both bundles used
`--network none`; the resulting OCI runs used an isolated network namespace
and made no network request. They had read-only image roots, uid/gid 1000 with
no supplementary groups, and no capabilities.

## Observed result

The exact image digests were:

| Image | Digest | Result |
| --- | --- | --- |
| workstation r3 | `sha256:8cb359f1de8240b517fc8c01da76a214473dd0041c147c5ff6e05321d4d89f12` | PASS |
| browser r6 | `sha256:0a2f2c97bd4df60f80723a916e28523ecd7982eb5a28050931b9884444988f70` | PASS |

The workstation process exited 0 and reported:

```text
uid=1000(agent) gid=1000(agent) groups=1000(agent)
2.1.269 (Claude Code)
codex-cli 0.154.0
v22.22.0
Python 3.12.12
git version 2.39.5
tmux 3.3a
QORQUT_GVISOR_WORKSTATION_OK
```

The browser process exited 0 and reported Chrome `153.0.8010.36`, returned
the `about:blank` DOM, and emitted `QORQUT_GVISOR_BROWSER_OK`. Chrome's normal
sandbox was retained; `--no-sandbox` was not used. gVisor logged unsupported
`rseq` and netlink operations, and Chrome logged absent D-Bus and inotify
system services. Those messages did not prevent this no-network smoke.

An initial browser attempt exposed a fixture error: a read-only `/dev/shm`
caused Chrome to abort with `Creating shared memory in /dev/shm ... Permission
denied` (exit 134). Adding the bounded in-memory `/dev/shm` mount made the
rerun pass. A prior shell pipeline also masked Chrome's failure; the final
command captured the actual Chrome status before printing the success marker.

After the reproducibility review, fresh `runsc spec` bundles were generated
from each image rootfs and passed through the fixed generator and probe again.
The workstation rerun exited 0 with the same native tool markers; the browser
rerun exited 0 with the Chrome version, empty `about:blank` DOM, and
`QORQUT_GVISOR_BROWSER_OK`. The generated profiles now explicitly provide a
bounded 128 MiB `/tmp` (plus the profile-specific 64 MiB mounts), and the
probe compares the complete profile command, environment, and `/` working
directory rather than searching for a marker substring.

## Decision and limits

gVisor is compatible with the current synthetic workstation and browser
processes under this release and these resource settings. It is a candidate
for a separately provisioned runtime image, subject to normal performance and
operational review.

## CDP connector boundary check

The connector's host helper enters Docker's network namespace with `nsenter`
and opens `127.0.0.1:<registered-port>`. A third disposable bundle, built from
the workstation rootfs with the fixed `connector` profile in
`gvisor_bundle.py`, ran a two-request HTTP server on `127.0.0.1:9222` under
`runsc --network=none`. A second process started with `runsc exec` fetched
`http://127.0.0.1:9222` and received `gvisor-loopback` (exit 0). The host then
ran the same namespace entry shape against the sandbox PID:

```text
host_nsenter_status=7
curl: (7) Failed to connect to 127.0.0.1 port 9222
```

This is the expected gVisor boundary: its loopback device and TCP state live in
the Sentry userspace network stack, so entering the host Linux namespace does
not expose that socket. The current host `nsenter` CDP connector is therefore
incompatible with this gVisor mode. Network passthrough could change that
behavior, but it uses the host network stack and reduces isolation; it was not
enabled or treated as an acceptable workaround.

The check also did not exercise the business listener, Docker's gVisor runtime
integration, firewall PREROUTING/REDIRECT, DNS interception, or any external
route. Keep native `runc` as the production default. Selecting gVisor would
require a separately reviewed connector design that can reach the Sentry
loopback without weakening the workload boundary.
