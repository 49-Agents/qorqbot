# syntax=docker/dockerfile:1
# Debian Official Image digest resolved for this image on 2026-09-12.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

ARG CHROME_VERSION=153.0.8010.36-1
ARG CHROME_SHA256=9bb44e33031c2f2857cf36b4343051a12f93058e4b781e3c76313df87f6c8d32

LABEL org.opencontainers.image.title="Qorqut BusinessSandbox browser" \
      org.opencontainers.image.description="Pinned non-root Google Chrome browser; runtime sandboxing is mandatory" \
      org.opencontainers.image.base.name="debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171" \
      org.opencontainers.image.version="google-chrome-stable-153.0.8010.36-1"

# The Chrome Debian package is pinned by version and the SHA-256 published in
# Google's stable package index.  The package pulls its declared runtime libraries;
# no browser is copied from the host or another business profile.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && curl --fail --silent --show-error --location \
        --output /tmp/google-chrome-stable.deb \
        "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${CHROME_VERSION}_amd64.deb" \
    && echo "${CHROME_SHA256}  /tmp/google-chrome-stable.deb" | sha256sum --check --status \
    && apt-get install --yes --no-install-recommends /tmp/google-chrome-stable.deb \
    && rm -f /tmp/google-chrome-stable.deb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/agent --shell /bin/bash agent \
    && install --directory --owner=agent --group=agent --mode=0700 \
        /home/agent/.cache /home/agent/.config /home/agent/Downloads

COPY tools/business_sandbox/browser_close.pl /usr/local/bin/qorqut-browser-close
RUN chmod 0755 /usr/local/bin/qorqut-browser-close

ENV HOME=/home/agent \
    XDG_CACHE_HOME=/home/agent/.cache \
    XDG_CONFIG_HOME=/home/agent/.config \
    TMPDIR=/tmp

USER agent
WORKDIR /home/agent

# The lifecycle driver starts the browser only after it holds the per-business
# browser/profile lease.  It must not pass --no-sandbox or --disable-setuid-sandbox.
# A normal runtime supplies read-only root, tmpfs /tmp, and writable profile and
# download mounts.  The idle command intentionally exposes no listener.
CMD ["sleep", "infinity"]
