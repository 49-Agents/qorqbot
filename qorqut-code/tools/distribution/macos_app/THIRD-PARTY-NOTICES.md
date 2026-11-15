# Mac desktop components

The Qorqbot desktop wrapper uses the following components under their own licenses:

| Component | Version | License and included notice |
| --- | --- | --- |
| Electron | 44.10.2 | MIT; `Contents/Resources/LICENSE.electron.txt` |
| Chromium and its dependencies | Included with Electron | Their individual notices are in `Contents/Resources/LICENSES.chromium.html` |
| node-semver | 7.8.6 | ISC; `node_modules/semver/LICENSE` inside `Contents/Resources/app.asar` |

The bundled native Qorqbot archive retains its own licenses and notices. It ships the
following programs, built by `tools/distribution/macos_tools.py` from the sources recorded in
`runtime/native/TOOLS.json`; each license is in `runtime/native/licenses` of the installed folder:

| Program | Version | License |
| --- | --- | --- |
| Python (python-build-standalone build of CPython) | 3.12.16 | PSF License 2.0, with the licenses of the libraries it includes |
| tmux | 3.8 | ISC |
| libevent (inside tmux) | 2.1.13 | BSD 3-Clause |
| utf8proc (inside tmux) | 2.12.0 | MIT |
| jemalloc (beside tmux) | 5.4.0 | BSD 2-Clause |
| git | 2.56.1 | GPL-2.0; source: https://github.com/git/git/archive/refs/tags/v2.56.1.tar.gz |
| agent-browser | 0.38.1 | Apache-2.0 |
 Build tools
are development dependencies and are removed from the installed desktop wrapper.
