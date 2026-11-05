# BusinessSandbox workstation image

This is a versioned, non-root workstation image for a single registered business.
It contains Python 3.12.12, Node 22.22.0, Git, GitHub CLI, OpenSSH, tmux, Claude
Code, Codex, the existing locked Playwright CLI, C/C++ build tools, pkg-config,
ripgrep, jq and unzip. It intentionally does **not**
contain Chrome, a Docker client, sudo, credentials or a Docker socket. Chrome is
owned by the separate browser container.

The Dockerfile pins the official Python and Node OCI indexes by digest. `gh` is the
official `cli/cli` v2.100.0 Linux AMD64 release asset and is checked against the
project's published SHA-256. `package-lock.json` resolves and integrity-locks
Claude Code 2.1.269 and Codex 0.154.0; `../browser/package-lock.json` is installed
unchanged for Playwright CLI 0.1.19 and proxy-chain 3.0.1. The upstream installation
references are [Docker Official Images](https://hub.docker.com/_/python),
[Claude Code](https://docs.anthropic.com/en/docs/claude-code/getting-started),
[OpenAI Codex](https://github.com/openai/codex), and the
[GitHub CLI release](https://github.com/cli/cli/releases/tag/v2.100.0).

## Build

The image installs the host's first-party Python packages only from local wheels that `tools/prepare-local.py` already built;
ordinary third-party dependencies come from the package index under `service-constraints.txt`. The core workstation needs the host's
**base** first-party closure (Operations, Context, document history and the host itself), not Paper's service, the learning
engine or any optional extension.

`stage_workstation_context.py` reads the preparation receipt, selects those wheels, checks each file's SHA-256, name and version
against the receipt and against every requirement that names it, and writes a **new, owned** build context. It refuses a missing or
altered wheel, a first-party requirement given as a URL or without a prepared wheel, and a version that does not satisfy a
requirement, before anything is written. It never modifies the prepared root or the source checkout, builds no image and runs no
Docker command. Run it with the prepared `venv/bin/python`:

```sh
PREPARED=/absolute/prepared                  # output of tools/prepare-local.py
CONTEXT=/absolute/workstation-context        # must not exist yet
APP="$PREPARED/src/qorqut-code/app"
"$PREPARED/venv/bin/python" "$APP/tools/business_sandbox/stage_workstation_context.py" \
  --prepared-root "$PREPARED" --output "$CONTEXT"
docker build \
  --file "$APP/tools/business_sandbox/workstation.Dockerfile" \
  --tag qorqut-business-sandbox-workstation:local \
  "$CONTEXT"
docker image inspect qorqut-business-sandbox-workstation:local \
  --format '{{.Id}} {{json .RepoDigests}}'
```

The context holds `wheelhouse/` (the selected wheels, `first-party.txt` binding each to its local file and SHA-256, and
`third-party.txt` with the remaining requirements), `service-constraints.txt`, the browser and CLI lock files that the Dockerfile
copies, and `CONTEXT.json` recording what it was staged from. The Dockerfile's first `pip install` uses `--no-index --no-deps`
with `first-party.txt`, so a missing, altered or unlisted first-party wheel fails before any package can be fetched; the second
installs only `third-party.txt` under the constraints, and `pip check` ends the step. The image build itself is unchanged
(pinned Python and Node images, pinned `gh`, locked Claude Code, Codex and Playwright CLI); it still needs the Docker daemon and
network access for those pinned inputs and the third-party packages.

The lifecycle provisioner supplies persistent business and HOME mounts. For a
repeatable offline image check, run the fixed `native_smoke.py` with bounded,
empty tmpfs directories and no network or credentials:

```sh
docker run --rm --name qorqut-workstation-image-smoke \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 768m --memory-swap 768m --cpus 1.5 --pids-limit 256 \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=128m,mode=1777 \
  --tmpfs /home/agent:rw,exec,nosuid,nodev,size=128m,mode=700,uid=1000,gid=1000 \
  --tmpfs /business:rw,exec,nosuid,nodev,size=64m,mode=700,uid=1000,gid=1000 \
  --mount "type=bind,src=$APP/tools/business_sandbox/native_smoke.py,dst=/probe.py,readonly" \
  qorqut-business-sandbox-workstation:local python3 -I /probe.py
```

Executable writable mounts are deliberate: project binaries and installed tools
must run. The image root remains read-only. Production keeps the workspace at its
registered absolute path and mounts the host-private persistent HOME at
`/home/agent`; the empty tmpfs directories above belong only to this probe.

The probe checks ordinary and login-shell gh selection, Git defaults, native
versions, C compilation, a project Python venv, Node and two real tmux sessions.
Its CPU/RSS figures describe that short synthetic probe, not model-session memory
or whole-business capacity. It does not authenticate a model or browser account.

The Dockerfile builds with the Docker daemon's bundled Dockerfile frontend, avoiding an external syntax image
(BuildKit documents its [bundled and external frontends](https://docs.docker.com/build/buildkit/frontend/)). Earlier CLI startup
timeouts under host disk contention remain a reliability concern. See the [sandbox adoption guide](../../../docs/sandbox/ADOPTION.md)
for the native adoption, rollback and recovery gates. No image has been built or deployed from the local-wheel recipe in this
source tree; the offline probe above is how a built image is checked.

## GitHub and Git

The image installs the pinned upstream `gh` at `/usr/local/bin/gh` and Git with `main` as the default initial branch. It holds no
GitHub App key and no credential helper, and the shared service issues no provider credentials; authenticate `gh` and Git the
ordinary way inside the terminal.
