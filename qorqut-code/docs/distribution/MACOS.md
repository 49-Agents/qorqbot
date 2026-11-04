# Qorqbot for Mac — native preparation

First target: Apple silicon (M1 or newer), macOS 14 or newer, Python 3.12.
This is a native Python application in a folder, with the existing browser UI.
It does not use a Linux VM. Native Mac acceptance is pending; do not present the
prepared archive as a tested Mac release until the Mac check passes.

Install Python 3.12, Git and, for agent terminals, tmux yourself. With Homebrew:

```sh
brew install python@3.12 git tmux
```

Use your normal Terminal with those commands on PATH. If Python is not on PATH,
set `QORQUT_PYTHON` to its executable, for example
`export QORQUT_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"`.
Install and authenticate your chosen Codex or Claude Code client separately.
Browser automation needs your installed Google Chrome and agent-browser.
Standard Chrome in `/Applications/Google Chrome.app` is detected; an explicit
`QORQUT_CHROME_EXECUTABLE` overrides it. Browser sandboxing remains enabled.

Verify the download against its checksum, then extract to a short writable
folder under your home directory:

```sh
tar -xzf qorqbot-VERSION-macos14-arm64.tar.gz
cd qorqbot
./qorqbot
```

Open the printed localhost address. First launch creates a local virtual
environment and installs the included Python libraries offline. The Mac archive
contains Mac-compatible wheels, not Linux libraries or bundled system tools.
Application files are in `runtime/`; records/work are in `instance/operations/`.
Keep the initialized folder at the same path. Roughly 50 bytes or fewer is
recommended because Unix sockets have short path limits.

Use `./qorqbot stop`, `./qorqbot status` and `./qorqbot verify` from another
Terminal. Back up the entire stopped folder. The same-schema updater uses a
matching Mac archive and refuses a Linux or other-CPU archive. Updates with
separately installed connectors or changed managed instructions are not yet
supported. Stop agents before updates or removal. Removing the folder removes
its data; user-installed external programs remain installed.

Google Calendar is bundled inactive. Fastmail is a separate platform-neutral
bundle; Telegram needs its Mac Pillow bundle. A Linux WhatsApp helper cannot run
on Mac: build and qualify the Mac helper separately, including its GPL source.
Use the matching source's connector setup guides and `extension-install` command.
Users supply their accounts and provider setup.

## Qualification limits

The Mac code and package can be prepared on Linux, but only a real Mac can prove
initialization, process permissions, agent terminals, browser ownership and safe
updates. Run the native acceptance workflow before making this download public.
macOS hides the environment of Apple platform processes (Finder, Dock,
`/bin/sleep`), even for the same user. The ownership scan used by updates and
connector installation treats those hidden signals as absent, as the Linux scan
does for unreadable `/proc` entries, and counts a process as this folder's when
its executable is inside the folder, its readable environment carries the
folder marker, or an ancestor is such a process. Recent macOS also reports a
different device number through `/dev/fd/N` than `fstat()` for the same file,
so the Darwin descriptor check compares inode, type, size and timestamps.
Ordinary app startup, status and stop use the saved process identity.
The Linux X11/systemd desktop stack is not available on Mac; ordinary Chrome
browser automation is separate. Shared Codex app-server writer-lock attribution,
legacy recovery administration and managed Agentic upgrade paths retain explicit
platform limitations where Linux kernel facilities have no verified equivalent.
The Mac check must record these limits; passing static checks is not native
acceptance. Existing Linux archives remain unchanged.

For the maintainer: export one reviewed source commit, run prepare-local.py and
native_build.py with `--target macos14-arm64`, then run `check_posix.py` against
the exact archive on an Apple silicon Mac. The prepared Mac workflow performs
that sequence. The command-line distribution itself adds no installer
service, updater feed or VM.

## Qorqbot.app and DMG

`tools/distribution/macos_app/build_dmg.py` wraps the exact checked archive in
`Qorqbot.app`, a desktop app built with Electron, and a drag-to-Applications
DMG. The app shows Qorqbot in its own window. It carries the archive unchanged
in `Contents/Resources/qorqbot-payload`; it bundles no Python, git or tmux.

- First open: a set-up screen shows each step and its log while the app checks
  prerequisites, verifies the archive, unpacks it to `~/Qorqbot/qorqbot` (a
  short path, see above) and runs `./qorqbot setup`. Then it starts the server
  on port 1049 (or the next free port on the first launch) and shows the dashboard.
  It remembers that port across restarts so existing agents keep their server address.
  If the saved port is occupied, close the other program and retry.
- Closing the window hides it; the Dock icon brings it back. **Quit** stops the
  local server first. Opening the app again focuses the existing window.
- The **Server** menu shows the status and can stop, start or restart the
  server, open the app and server logs, and reveal the install folder.
  **Help > How Qorqbot Works** explains all of this, including removal.
- A newer app offers to update the installed folder through its own
  `./qorqbot update` with the bundled archive and checksum.
- Missing Python 3.12, git or tmux, and any failure, are shown in the window
  with Copy Command, Try Again and Show Log actions.
- Links to the dashboard's own pages open new app windows; other links open in
  the default browser. **View > Open in Browser** shows the dashboard there.
- The app log is `~/Qorqbot/launcher.log`; the server log stays in
  `instance/logs/application.log`.

Building needs Node.js 22+ with npm (Electron and Electron Packager are pinned
in `electron/package-lock.json`) and librsvg for the icon:

```sh
brew install librsvg node
python3.12 tools/distribution/macos_app/build_dmg.py \
  --archive /abs/qorqbot-VERSION-macos14-arm64.tar.gz --output /abs/dmg-out
```

The DMG is about 145 MB because Electron includes Chromium. Electron's license
and Chromium's third-party notices are copied into the app's Resources.

The default signature is ad-hoc. A copy that was downloaded is quarantined, so
macOS blocks the first open; users choose **System Settings > Privacy &
Security > Open Anyway** once (right-click > Open no longer bypasses this on
macOS 15 and newer). Opening without that prompt requires an Apple Developer ID:
pass `--identity "Developer ID Application: ..."` and `--notary-profile NAME`
(from `xcrun notarytool store-credentials`) to sign with the hardened runtime
(`entitlements.plist`), notarize and staple. The notarized path is untested
here; the bundled wheels' native libraries may also need Developer ID
signatures before Apple accepts it.

Tests (macOS):

```sh
(cd tools/distribution/macos_app/electron && npm ci && npm test)   # install/start/stop engine
python3.12 -m unittest discover -s tools/distribution/macos_app/tests   # builder, full DMG build
```
