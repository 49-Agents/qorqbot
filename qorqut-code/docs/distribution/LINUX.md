# Qorqbot desktop app for Linux

Ubuntu 24.04 or newer on an Intel or AMD 64-bit computer (x86_64); other distributions with glibc 2.38 or
newer (Fedora 39+, Debian 13) also work. Debian 12 and Ubuntu 22.04 are too old for the shipped tmux and git.
The app is an AppImage or a `.deb`; both show Qorqbot in its own window. Everything Qorqbot needs (Python 3.12,
tmux, git, the agent-browser tool) is inside; install and sign in to `claude` or `codex` yourself.

The same self-contained `.tar.gz` also runs without the app: extract it, `cd qorqbot` and run `./qorqbot`; it
opens the dashboard in your browser. The older system-Python path is described in [README.md](README.md).

The app carries the exact Linux archive unchanged in its `qorqbot-payload` folder. That archive is
self-contained: it brings its own Python 3.12, git, tmux and the agent-browser tool, so nothing has to be
installed first. Install and sign in to your chosen Codex or Claude Code client separately. Browser
automation needs your installed Google Chrome. Each release lists the SHA-256 of its files in `SHA256SUMS`:

```sh
sha256sum -c SHA256SUMS --ignore-missing
```

## AppImage

Download `Qorqbot-VERSION-x86_64.AppImage`, make it executable and open it:

```sh
chmod +x Qorqbot-VERSION-x86_64.AppImage
./Qorqbot-VERSION-x86_64.AppImage        # or double-click it in your file manager
```

- It is one file; keep it where you want to run it from. Ubuntu 24.04 mounts it with the `fuse3` it ships.
  Without FUSE (a minimal install or a container), run it with `APPIMAGE_EXTRACT_AND_RUN=1` in front, or
  `sudo apt install fuse3`.
- The first time, the app offers to add Qorqbot to your applications menu. It writes
  `~/.local/share/applications/qorqbot.desktop` and an icon, nothing else. Later, **File > Add to Applications
  Menu** and **Remove from Applications Menu** do the same. If you move the AppImage, open it once from its new
  place and the entry follows.
- Ubuntu 24.04 blocks the user namespaces that Chromium's sandbox uses (`kernel.apparmor_restrict_unprivileged_userns`),
  and an AppImage cannot install the exception the `.deb` installs. On such a system the AppImage's launcher
  starts Chromium with `--no-sandbox`; **Help > About Qorqbot** says which case applies. The windows only
  show the local dashboard, but if you want the sandbox on Ubuntu 24.04, use the `.deb`.

## .deb

```sh
sudo apt install ./qorqbot_VERSION_amd64.deb
```

`apt` fetches the few libraries Electron needs (GTK, NSS, ALSA and others; see `dpkg -s qorqbot`). The package
installs the app in `/opt/Qorqbot`, a **Qorqbot** entry (Office) in the applications menu with its icons, and
`/usr/bin/qorqbot-desktop`, which starts the app. On Ubuntu 24.04 it also installs an AppArmor profile,
`/etc/apparmor.d/qorqbot-desktop`, that allows user namespaces for `/opt/Qorqbot/qorqbot-desktop`, as Chrome and VS Code do, so
Chromium's sandbox can stay on. Where user namespaces do not work at all, the install script makes
`chrome-sandbox` setuid instead.

Pre-release versions use `~` in the package version (`1.3.0~preview.2`), so a release always upgrades them.

## What the app does

- First open: a short "Setting up Qorqbot" screen while the app verifies the archive, unpacks it to
  `~/Qorqbot/qorqbot` (a short path, see [README.md](README.md)) and runs `./qorqbot setup`; this takes a few
  seconds. Then it starts the server on port 1049 (or the next free port on the first launch) and shows the
  dashboard. It remembers the port across restarts so existing agents keep their server address.
- Closing the window hides it to the system tray when your panel shows tray icons (a StatusNotifier host, as in
  KDE Plasma or GNOME with the AppIndicator extension); click the icon to bring it back. Stock GNOME has none,
  so closing the window quits there. **File > Quit** (Ctrl+Q) or the tray menu's Quit stops the local server first, unless the server was
  started from a terminal, which then keeps running. Opening the app again focuses the existing window.
- The **Server** menu shows the status and can stop, start or restart the server, open the app and server logs
  and show the install folder. **Help > How Qorqbot Works** explains this, including removal.
  **Help > Install Terminal Command** installs a `qorqbot` command in `~/.local/bin`; with the `.deb`, that file
  runs the browser version; the app itself is `qorqbot-desktop`, so the two never collide.
- A newer app offers to update the installed folder through its own `./qorqbot update` with the bundled archive
  and checksum. Close agent terminals first.
- A failure is shown in the window with its details and Try Again, Copy Details and Show Log actions.
- Links to the dashboard's own pages open new app windows; other links open in the default browser.
  **View > Open in Browser** shows the dashboard there.

## Where things are

| What | Where |
| --- | --- |
| Qorqbot, its records and its logs | `~/Qorqbot/qorqbot` (records in `instance/operations/`, server log in `instance/logs/application.log`) |
| The app's log, the saved port | `~/Qorqbot/launcher.log`, `~/Qorqbot/server-port` |
| Your answer to the applications menu question | `~/Qorqbot/desktop-integration.json` |
| The window's own data (the sign-in session) | `~/.config/Qorqbot` |
| The app (`.deb`) | `/opt/Qorqbot`, `/usr/bin/qorqbot-desktop` |

Back up the whole `~/Qorqbot/qorqbot` folder while Qorqbot and its agents are stopped.

## Update and removal

- AppImage: download the new file, quit the app and replace the old file. The new app offers to update the
  installed folder. `.deb`: `sudo apt install ./qorqbot_NEWER_amd64.deb` replaces the app, and its first open offers
  the same update. Your records stay in place.
- AppImage: quit the app, choose **File > Remove from Applications Menu**, delete the file. `.deb`:
  `sudo apt remove qorqbot` removes the app and the AppArmor profile.
- Both leave `~/Qorqbot` and `~/.config/Qorqbot` in place. Stop Qorqbot and its agents, keep any backup you want,
  then delete those folders to remove your data. Your agent clients and Chrome stay installed.

## Build

Needs Linux x86_64, Node.js 22+ with npm (Electron and electron-builder are pinned in
`electron/package-lock.json`) and, for the best icon rendering, `librsvg2-bin` (without it, Electron draws the
same icon). The archive must be self-contained: build the programs once with `linux_tools.py`, then pass them to
`native_build.py`, as in the [release runbook](RELEASE-RUNBOOK.md). `build_linux_app.py` refuses an archive built
without `--bundled-tools` unless you pass `--allow-system-tools` (the app then needs the computer's Python 3.12,
git and tmux):

```sh
python3.12 tools/distribution/linux_tools.py --docker --output /abs/tools --work /abs/tools-work
python3.12 tools/distribution/native_build.py --prepared PREPARED --target ubuntu24-x86_64 \
  --bundled-tools /abs/tools --output BUILD --report BUILD_REPORT.json --version VERSION
python3.12 tools/distribution/linux_app/build_linux_app.py \
  --archive /abs/qorqbot-VERSION-ubuntu24-x86_64.tar.gz --output /abs/linux-app-out [--targets appimage,deb]
```

The output folder holds `Qorqbot-VERSION-x86_64.AppImage`, `qorqbot_VERSION_amd64.deb` and `SHA256SUMS`. Each
file is the archive plus about 100 MB (`.deb`) or 120 MB (AppImage), because Electron includes Chromium.
Electron's license and Chromium's notices are installed with the app; `THIRD-PARTY-NOTICES.md` lists the rest.

Checks:

```sh
(cd tools/distribution/linux_app/electron && npm ci && npm test)             # install/start/stop engine
python3.12 -m unittest discover -s tools/distribution/linux_app/tests         # builder (QORQBOT_LINUX_APP_BUILD=1 also builds)
bash tools/distribution/linux_app/first_open_check.sh APPIMAGE OUT_FOLDER     # new home, Xvfb: sets up, sign-in screen, quits, server stops
bash tools/distribution/linux_app/check_deb.sh DEB OUT_FOLDER                 # Docker, ubuntu:24.04: apt install, files, menu entry, AppArmor profile, opens, apt remove
```

`.github/workflows/linux-app-checks.yml` runs all of this on manual dispatch; with its `version` input it also builds
the AppImage and `.deb` from the checked archive and runs the two gates above on clean machines.

## Qualification limits

The checks run under Xvfb without a window manager and in a container. They do not exercise GNOME or KDE
sessions, Wayland, the tray icon, the AppArmor profile while AppArmor is enforcing, or interactive agent sign-in.
Record those on a real Ubuntu 24.04 desktop before promoting a candidate to the website.
