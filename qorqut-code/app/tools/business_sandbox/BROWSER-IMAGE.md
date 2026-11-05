# BusinessSandbox browser image

`browser.Dockerfile` packages Google Chrome Stable
`153.0.8010.36-1`, verified against SHA-256
`9bb44e33031c2f2857cf36b4343051a12f93058e4b781e3c76313df87f6c8d32` from
Google's [stable Debian package index](https://dl.google.com/linux/chrome/deb/dists/stable/main/binary-amd64/Packages).
The base is the pinned Debian Official Image index named in the Dockerfile.

The image has only the `agent` OS account (UID/GID 1000), Chrome and its declared
runtime libraries. It contains no credentials, Docker client/socket, sudo, copied
host browser or owner profile. It starts idle. The trusted lifecycle driver must
hold the browser/profile ownership lease before launching Chrome and must make the
agent endpoint available only through its separately reviewed binding.

Build from the `biz-core` checkout:

```sh
sudo -n docker build --file tools/business_sandbox/browser.Dockerfile \
  --tag qorqut-business-sandbox-browser:local .
sudo -n docker image inspect qorqut-business-sandbox-browser:local \
  --format '{{index .RepoDigests 0}} {{.Id}}'
```

The required runtime confinement is read-only root, no Linux capabilities,
`no-new-privileges`, a writable tmpfs `/tmp`, and explicitly writable per-business
profile and download mounts. Chrome must run as `agent` and never receives
`--no-sandbox` or `--disable-setuid-sandbox`. Docker's default seccomp profile
rejects Chrome's user-namespace sandbox with this capability set. The committed
container-only profile [browser-seccomp-moby-29.1.3.json](../../bazarlyk/sandbox/browser-seccomp-moby-29.1.3.json)
is Moby `docker-v29.1.3` default profile SHA-256
`01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28`, plus
unconditional allows only for `clone`, `setns`, `unshare`, and `chroot`. Its
committed SHA-256 is
`82872222996198cf07177a15c84a0fcd1c8a00f9786aa68a528888d4a6a42a2e`.
The trusted lifecycle config must verify that exact file before passing its
absolute path as `--security-opt seccomp=...`; it must never use
`seccomp=unconfined`.

```sh
sudo -n docker run --rm --name qorqut-browser-image-sandbox-smoke \
  --label qorqut.browser.image-test=true --read-only --cap-drop=ALL \
  --security-opt no-new-privileges \
  --security-opt "seccomp=$PWD/bazarlyk/sandbox/browser-seccomp-moby-29.1.3.json" \
  --shm-size=256m \
  --tmpfs /tmp:rw,nosuid,nodev,exec \
  --env PROFILE="$PROFILE" --env HOME="$PROFILE" \
  --env XDG_CONFIG_HOME="$PROFILE" --env XDG_CACHE_HOME=/tmp \
  --mount type=bind,src="$PROFILE",dst="$PROFILE" \
  --mount type=bind,src="$DOWNLOADS",dst="$DOWNLOADS" \
  qorqut-business-sandbox-browser:local \
  bash -euo pipefail -c 'google-chrome-stable --headless=new --no-first-run \
    --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
    --password-store=basic --user-data-dir="$PROFILE" about:blank >/tmp/chrome.log 2>&1 & \
    chrome_pid=$!; sleep 3; kill -0 "$chrome_pid"; \
    curl --fail --silent http://127.0.0.1:9222/json/version; \
    sed -n -e "/^CapEff:/p" -e "/^NoNewPrivs:/p" -e "/^Seccomp:/p" "/proc/$chrome_pid/status"; \
    tr "\\0" "\\n" < "/proc/$chrome_pid/cmdline" | grep -E -- "--(no-sandbox|disable-setuid-sandbox)" && exit 1 || true; \
    kill "$chrome_pid"; wait "$chrome_pid" || true'
```

The command is a native sandbox startup check only; it must be treated as failed
if Chrome exits with a user-namespace, setuid-sandbox, seccomp or AppArmor error.
It does not use a browser account, model credentials, or an agent-facing listener.
On this Ubuntu host, `kernel.apparmor_restrict_unprivileged_userns=1` and a direct
unprivileged user-namespace probe returns `Operation not permitted`. Do not weaken
AppArmor globally or select `seccomp=unconfined` to make this pass. The actual
confined Chrome result determines whether a narrowly reviewed runtime policy or a
different isolation runtime is required.

## Native test status

On 2026-09-12 the pinned image built successfully as
`qorqut-business-sandbox-browser:local-5dbab96-r1` with manifest-list digest
`sha256:b21b01e6cedd1dbe8f6755bd08a1bdfdd1704fd25abe91528b952d9843bf3189`.
Its Chrome version command returned `Google Chrome 153.0.8010.36`.

The Docker Engine `29.1.3` native test passed on 2026-09-12 with the profile
above. It ran as UID 1000 with read-only root, `--cap-drop=ALL`,
`no-new-privileges`, default Docker AppArmor, a writable tmpfs and fresh profile
mount, without either sandbox-disabling argument. `unshare --user --map-root-user`
passed under that container profile, Chrome reached `/json/version` as
`Chrome/153.0.8010.36`, and its browser process reported `CapEff: 0`,
`NoNewPrivs: 1`, and `Seccomp: 2`. The earlier default-profile-only test was
blocked first on user namespace creation and then, after opening those calls, on
Chrome's `chroot`; those four calls are the entire profile delta. Disposable test
containers and volumes were removed.

The exact lifecycle mount layout has no writable agent home: only the registered
profile/download paths, /tmp, and /dev/shm are writable. Chrome therefore receives
HOME and XDG_CONFIG_HOME set to its registered profile, and XDG_CACHE_HOME=/tmp;
otherwise Crashpad attempts to create the read-only home .local directory and Chrome
traps. The image-local qorqut-browser-close helper makes a loopback-only CDP
Browser.close request. Readiness verifies the exact Chrome executable/profile/start
tick identity, the Docker-owned container, a distinct Chrome user namespace, and an
additional Chrome seccomp filter beyond the browser process baseline.

The native SandboxBrowser callback passed on 2026-09-12 against immutable local
image sha256:0a2f2c97bd4df60f80723a916e28523ecd7982eb5a28050931b9884444988f70.
It used the exact profile/download-only mounts and confinement above, returned
READINESS True, then issued the image-local CDP close and returned CLOSE True.
Its process check found explicit --type=renderer workers with a second seccomp
filter and no effective capabilities, while the root Chrome process was bound to
the registered profile and Linux start tick. The owned disposable container and
temporary paths were removed.

This proves a constrained headless Chrome startup only. It does not provide an
owner login/viewer path or authorize a public CDP listener. Production shutdown
must send CDP `Browser.close` through a trusted private-container transport and
observe Chrome exit before another writer may reuse the profile.
