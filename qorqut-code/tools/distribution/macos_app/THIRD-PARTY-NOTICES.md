# Mac desktop components

The Qorqbot desktop wrapper uses the following components under their own licenses:

| Component | Version | License and included notice |
| --- | --- | --- |
| Electron | 44.10.2 | MIT; `Contents/Resources/LICENSE.electron.txt` |
| Chromium and its dependencies | Included with Electron | Their individual notices are in `Contents/Resources/LICENSES.chromium.html` |
| node-semver | 7.8.6 | ISC; `node_modules/semver/LICENSE` inside `Contents/Resources/app.asar` |

The bundled native Qorqbot archive retains its own licenses and notices. Build tools
are development dependencies and are removed from the installed desktop wrapper.
