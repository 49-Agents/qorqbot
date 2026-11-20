# Qorqbot for Mac — native preparation

First target: Apple silicon (M1 or newer), macOS 14 or newer.
This is a native Python application in a folder, with the existing browser UI.
It does not use a Linux VM. Native Mac acceptance is pending; do not present the
prepared archive as a tested Mac release until the Mac check passes.

The Mac archive is self-contained: it carries its own Python 3.12 with the
libraries already installed, plus git and the agent-browser tool, in
`runtime/venv` and `runtime/native`. Nothing has to be installed first; it does
not need Homebrew or Apple's developer tools. Setting `QORQUT_PYTHON` still
selects another Python 3.12 for the launcher itself.
Qorqbot downloads Claude Code and Codex itself; you sign in to your own account
from the dashboard, in your browser.
Agents' web browsing needs no installed Chrome. Once the app runs, Qorqbot
downloads its own Chrome (a pinned Chrome for Testing build, about 200 MB) in the
background, checks it against its pinned checksum, and uses it for new agent
browsers after it has started once on this Mac. Until then, or if it cannot start
here, an installed `/Applications/Google Chrome.app` is used. An existing agent
browser keeps the Chrome it was created with. An explicit
`QORQUT_CHROME_EXECUTABLE` overrides both, and Qorqbot then downloads no Chrome;
`QORQUT_AGENT_BROWSER_EXECUTABLE` replaces the shipped browser tool. Browser
sandboxing remains enabled.

Verify the download against its checksum, then extract to a short writable
folder under your home directory:

```sh
tar -xzf qorqbot-VERSION-macos14-arm64.tar.gz
cd qorqbot
./qorqbot
```

Your browser opens the printed localhost address (`--no-open` skips that;
`--port` picks another port and later starts reuse it). First launch checks the
shipped programs against the package's checksums and creates your records; it
builds nothing, and downloads only Qorqbot's own Chrome (above), in the
background after the app starts. The shipped programs come before your own on the
PATH Qorqbot and its agents use.
Application files are in `runtime/`; records/work are in `instance/operations/`.
Keep the initialized folder at the same path. Roughly 50 bytes or fewer is
recommended because Unix sockets have short path limits.

Use `./qorqbot stop`, `./qorqbot status` and `./qorqbot verify` from another
Terminal. `./qorqbot install-command` writes a `qorqbot` command into Homebrew's
`bin` folder (or `~/.local/bin`) so those work from any folder; the app offers
the same as **Help > Install Terminal Command**. It never replaces a `qorqbot`
file it did not write; `--remove` takes it out again. The app and a Terminal start
share one server: whichever starts second uses the running one, and quitting the
app leaves a Terminal-started server running. Back up the entire stopped folder. The same-schema updater uses a
matching Mac archive and refuses a Linux or other-CPU archive. Updates with
separately installed connectors or changed managed instructions are not yet
supported. The updater closes any open agent sessions first; they start again when you next message the agent. With Terminals (legacy) it refuses while agent terminals run. Stop Qorqbot before removal. Removing the folder removes
its data; user-installed external programs remain installed.

Google Calendar is bundled inactive. Fastmail is a separate platform-neutral
bundle; Telegram needs its Mac Pillow bundle. A Linux WhatsApp helper cannot run
on Mac: build and qualify the Mac helper separately, including its GPL source.
Use the matching source's connector setup guides and `extension-install` command.
Users supply their accounts and provider setup.

## Qualification limits

The Mac code and package can be prepared on Linux, but only a real Mac can prove
initialization, process permissions, agent sessions, browser ownership and safe
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

For the maintainer: export one reviewed source commit, run prepare-local.py,
build the shipped programs once with `macos_tools.py` (pinned, checksummed
sources; a few minutes, needs only Apple's developer tools), run
native_build.py with `--target macos14-arm64 --bundled-tools TOOLS_FOLDER`,
then run `check_posix.py` against the exact archive on an Apple silicon Mac.
The prepared Mac workflow performs that sequence, then opens the built disk
image on clean Macs that have no Homebrew, Python or developer tools and
requires the sign-in screen (`macos_app/first_open_check.sh`). The command-line
distribution itself adds no installer service, updater feed or VM.

What `--bundled-tools` adds to the archive:

- `runtime/venv`: a complete Python 3.12 (python-build-standalone) with every
  library from `requirements.lock` installed and precompiled. It is not a
  virtual environment, so it works wherever the folder is unpacked.
- `runtime/native`: git and
  agent-browser, their licenses in `native/licenses`, and `native/TOOLS.json`
  with the source of Python and git. `macos_tools.py` refuses a program
  that links against anything outside macOS itself. `native_build.py` downloads
  the pinned agent-browser package itself, checks its checksum and that the
  binary runs, and records its version in `runtime/DEPENDENCIES.json`.
- `PAYLOAD.json` records one checksum for each of the two folders. `verify`
  always checks `native`; it checks `venv` until first use, because optional
  extensions are installed into it afterwards.

Without `--bundled-tools` the Mac archive is the earlier kind, which uses the
computer's own Python 3.12 and git, builds a virtual environment on
first launch and ships the pinned agent-browser tool in `runtime/tools`, as the
Linux package does.

## Qorqbot.app and DMG

`tools/distribution/macos_app/build_dmg.py` wraps the exact checked archive in
`Qorqbot.app`, a desktop app built with Electron, and a drag-to-Applications
DMG. The app shows Qorqbot in its own window. It carries the archive unchanged
in `Contents/Resources/qorqbot-payload`; the archive brings its own Python, git
and agent-browser, so the app needs nothing installed on the Mac.

- Opened from the disk image, Downloads or the Desktop, the app first offers to
  move itself to Applications and reopens from there.
- First open: a short "Setting up Qorqbot" screen while the app verifies the
  archive, unpacks it to `~/Qorqbot/qorqbot` (a short path, see above) and runs
  `./qorqbot setup`; this takes a few seconds. Then it starts the server
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
- A failure is shown in the window with its details and Try Again, Copy
  Details and Show Log actions. The log is shown only then.
- Links to the dashboard's own pages open new app windows; other links open in
  the default browser. **View > Open in Browser** shows the dashboard there.
- The app log is `~/Qorqbot/launcher.log`; the server log stays in
  `instance/logs/application.log`.

Building needs Node.js 22+ with npm (Electron and electron-builder are pinned
in `electron/package-lock.json`) and librsvg for the icon and the disk image
window's background (`dmg-background.svg`). `build_dmg.py` prepares those and
the archive, then electron-builder packages and signs the app and lays out the
disk image window:

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
(`entitlements.plist`), notarize and staple. The Mac workflow's `notarize`
input does this with the repository's Developer ID secrets, then checks the
ticket with `stapler validate` and `spctl`. The notarized path is untested
here; the bundled wheels' native libraries may also need Developer ID
signatures before Apple accepts it.

Tests (macOS):

```sh
(cd tools/distribution/macos_app/electron && npm ci && npm test)   # install/start/stop engine
python3.12 -m unittest discover -s tools/distribution/macos_app/tests   # builder, full DMG build
```
