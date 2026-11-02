FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac AS native-base
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3-pip ca-certificates git tmux \
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
    libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 libcairo2 \
    tigervnc-standalone-server tigervnc-tools x11-utils x11-xserver-utils \
    openbox dbus-x11 xauth xfonts-base fonts-dejavu-core gnome-keyring \
    && rm -rf /var/lib/apt/lists/*

# Ubuntu's server hardcodes /usr/bin/xkbcomp. Rebuild with its supported
# configure option so xkbcomp is resolved from the helper's packaged PATH.
# Build dependencies and source trees never enter the customer runtime.
FROM native-base AS desktop-builder
RUN printf 'deb-src http://archive.ubuntu.com/ubuntu noble main universe\n' > /etc/apt/sources.list.d/qorqut-source.list \
    && apt-get update \
    && apt-get build-dep -y tigervnc=1.13.1+dfsg-2build2 \
    && mkdir /build
COPY native_xvnc.py /build/native_xvnc.py
RUN python3 /build/native_xvnc.py
COPY native_xvnc_relocate.py /build/native_xvnc_relocate.py
RUN python3 /build/native_xvnc_relocate.py

FROM native-base
COPY --from=desktop-builder /build/Xtigervnc /usr/bin/Xtigervnc
COPY --from=desktop-builder /build/TIGERVNC-BUILD.json /usr/share/doc/tigervnc-standalone-server/TIGERVNC-BUILD.json
COPY native_collect.py /build/native_collect.py
RUN python3 /build/native_collect.py /native
CMD ["/bin/true"]
