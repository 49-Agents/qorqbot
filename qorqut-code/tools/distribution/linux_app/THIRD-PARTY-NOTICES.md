# Linux desktop components

The Qorqbot desktop app for Linux (AppImage and .deb) uses the following components under their own licenses:

| Component | Version | License and included notice |
| --- | --- | --- |
| Electron | 44.10.2 | MIT; `LICENSE.electron.txt` next to the `qorqbot` program (`/opt/Qorqbot/` in the .deb) |
| Chromium and its dependencies | Included with Electron | Their individual notices are in `LICENSES.chromium.html`, in the same folder |
| node-semver | 7.8.6 | ISC; `node_modules/semver/LICENSE` inside `resources/app.asar` |

The AppImage also contains the AppImage runtime that electron-builder adds (the AppImage-tools-runtime bundle of
electron-userland/electron-builder-binaries, built from AppImage/type2-runtime). The runtime is MIT licensed and is
linked statically with musl libc (MIT), libfuse (LGPL-2.0), squashfuse (BSD-2-Clause), zstd (BSD-3-Clause) and zlib
(Zlib); it names these licenses in its own text. `usr/lib` of the image holds helper libraries that Electron loads
if the computer lacks them: libnotify (LGPL-2.1), libappindicator and libindicator (LGPL-2.1 and LGPL-3.0), libgconf
(LGPL-2.0) and libXss and libXtst (MIT/X11). The .deb contains none of these; it depends on the computer's own
libraries instead. Build tools are development dependencies and are removed from the installed desktop wrapper.

The bundled native Qorqbot archive retains its own licenses and notices. A self-contained archive ships the following
programs, built by `tools/distribution/linux_tools.py` from the sources recorded in `runtime/native/TOOLS.json`; each
license is in `runtime/native/licenses` of the installed folder (`~/Qorqbot/qorqbot`):

| Program | Version | License |
| --- | --- | --- |
| Python (python-build-standalone build of CPython) | 3.12.16 | PSF License 2.0, with the licenses of the libraries it includes |
| tmux | 3.8 | ISC |
| libevent (inside tmux) | 2.1.13 | BSD 3-Clause |
| utf8proc (inside tmux) | 2.12.0 | MIT |
| ncurses (inside tmux) | 6.5 | X11 |
| git | 2.56.1 | GPL-2.0; source: https://github.com/git/git/archive/refs/tags/v2.56.1.tar.gz |
| zlib (inside git) | 1.3.2 | Zlib |
| OpenSSL (inside git) | 3.5.9 | Apache-2.0 |
| curl (inside git) | 8.23.0 | curl |
| agent-browser | 0.38.1 | Apache-2.0 |
