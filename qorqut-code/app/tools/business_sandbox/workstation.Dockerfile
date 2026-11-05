# Uses the Docker daemon's bundled frontend; no external syntax image is needed.
# Docker Official Images digests, resolved for this image on 2026-09-12.
FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS node
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

LABEL org.opencontainers.image.title="Qorqut BusinessSandbox workstation" \
      org.opencontainers.image.description="Pinned non-root native coding tools; no browser, Docker client, or credentials" \
      org.opencontainers.image.base.name="python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"

ARG GH_VERSION=2.100.0
ARG GH_SHA256=e4d4bb4498e8d007abe545b6568926793ace1b6447da598294a610018cb164be

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/agent \
    PATH=/home/agent/.local/bin:/opt/qorqut/bin:/opt/qorqut/venv/bin:/opt/qorqut-tools/node_modules/.bin:/opt/qorqut-browser/node_modules/.bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    NPM_CONFIG_PREFIX=/home/agent/.local \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PIP_CACHE_DIR=/home/agent/.cache/pip \
    PYTHONUSERBASE=/home/agent/.local \
    PLAYWRIGHT_BROWSERS_PATH=/home/agent/.cache/ms-playwright \
    DISABLE_AUTOUPDATER=1 \
    TMPDIR=/tmp

# Node is copied from its pinned official image.  Git, SSH and tmux come from
# Debian; no sudo, Docker CLI, daemon socket or browser binary is installed.
COPY --from=node /usr/local /usr/local
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl git openssh-client tmux build-essential pkg-config ripgrep jq unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --silent --show-error --location \
        --output /tmp/gh.tar.gz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    && echo "${GH_SHA256}  /tmp/gh.tar.gz" | sha256sum --check --status \
    && tar --extract --gzip --file /tmp/gh.tar.gz --strip-components=2 \
        --directory /usr/local/bin "gh_${GH_VERSION}_linux_amd64/bin/gh" \
    && rm /tmp/gh.tar.gz \
    && git config --system init.defaultBranch main \
    && groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent \
    && install --directory --owner=agent --group=agent --mode=0700 \
        /home/agent/.cache /home/agent/.config /home/agent/.local \
    && install --directory --mode=0755 /business /opt/qorqut /opt/qorqut-tools /opt/qorqut-browser

# The browser-control package is retained exactly from tools/browser.  It supplies
# the Playwright CLI only; Chrome belongs to the separate browser container.
COPY tools/browser/package.json tools/browser/package-lock.json /opt/qorqut-browser/
RUN npm ci --prefix /opt/qorqut-browser --omit=dev --ignore-scripts

# Both model CLIs are pinned by this lockfile.  Their binaries are immutable image
# content, while the agent's future user-local installs stay under /home/agent.
COPY tools/business_sandbox/package.json tools/business_sandbox/package-lock.json /opt/qorqut-tools/
# Claude Code's lock-selected Linux binary is materialized by its own postinstall;
# the lockfile still fixes every downloaded package and integrity digest.
RUN npm ci --prefix /opt/qorqut-tools --omit=dev

# Keep the immutable image CLIs on the login-shell path too.  HOME-local tools
# still take priority for ordinary non-login agent shells through the ENV above.
RUN ln --symbolic /opt/qorqut-tools/node_modules/.bin/claude /usr/local/bin/claude \
    && ln --symbolic /opt/qorqut-tools/node_modules/.bin/codex /usr/local/bin/codex \
    && ln --symbolic /opt/qorqut-browser/node_modules/.bin/playwright-cli /usr/local/bin/playwright-cli \
    && printf '%s\n' \
        'export PATH="$HOME/.local/bin:/opt/qorqut/bin:/opt/qorqut/venv/bin:/opt/qorqut-tools/node_modules/.bin:/opt/qorqut-browser/node_modules/.bin:$PATH"' \
        > /etc/profile.d/qorqut-workstation-path.sh \
    && chown --recursive agent:agent /home/agent

# The build context is an owned directory made by tools/business_sandbox/stage_workstation_context.py from a
# prepare-local.py receipt (see README.md); no source tree is copied.  wheelhouse/ holds the host's base first-party wheels,
# first-party.txt (each wheel as `name @ file:///opt/qorqut-wheels/FILE#sha256=DIGEST`) and third-party.txt (the closure's
# remaining requirements).  The first install has no package index, so a missing, altered or unlisted first-party wheel fails
# before pip can fetch anything; the second resolves only third-party packages, under the existing constraints.
COPY service-constraints.txt /opt/qorqut/
COPY wheelhouse /opt/qorqut-wheels
RUN python -m venv /opt/qorqut/venv \
    && /opt/qorqut/venv/bin/pip install --no-cache-dir --no-index --no-deps \
        --requirement /opt/qorqut-wheels/first-party.txt \
    && /opt/qorqut/venv/bin/pip install --no-cache-dir \
        --constraint /opt/qorqut/service-constraints.txt --requirement /opt/qorqut-wheels/third-party.txt \
    && /opt/qorqut/venv/bin/pip check

USER agent
WORKDIR /business

# Runtime callers must use --read-only, a tmpfs /tmp, and explicit writable mounts
# for /home/agent and /business.  This image launches native shells and CLIs only.
CMD ["bash"]
