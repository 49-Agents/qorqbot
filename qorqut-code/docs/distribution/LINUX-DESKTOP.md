# Qorqbot for Ubuntu: desktop app

Target: Ubuntu 24.04 on Intel/AMD 64-bit (x86_64). The app is a `.deb` package
named `Qorqbot-VERSION-ubuntu24-x86_64.deb`. It shows Qorqbot in its own window,
using the same Electron shell as the Mac app. The terminal download described in
[README.md](README.md) stays available; the app is an alternative to it.

Status: the Linux release job builds and checks the package. It has not been
part of a published release yet, and a person has not yet accepted it on a real
desktop. Do not present it as a tested release until that is done.

## Install

The package contains the exact `qorqbot-VERSION-ubuntu24-x86_64.tar.gz` archive,
unchanged. Verify the download against its checksum, then install it with apt:

```sh
sudo apt install ./Qorqbot-VERSION-ubuntu24-x86_64.deb
```

apt also installs Python 3.12 (with `python3.12-venv`), git and tmux. Install
your chosen Codex or Claude Code client and sign in to it yourself; the app does
not do that.

## Start and stop

Open "Qorqbot" in the applications menu, or run `qorqbot-desktop` in a terminal.

- First launch unpacks the archive into `~/Qorqbot/qorqbot` and runs its offline
  set-up, which takes about a minute. Then the dashboard opens in the app's
  window. Later launches start the local server and show the dashboard.
- The app being open means Qorqbot is running. Closing the window, or Quit
  (Ctrl+Q), stops the local server.
- A server that you started from a terminal with `./qorqbot` is only attached
  to. It keeps running when the app quits.
- The app and a browser can use the same server. **View > Open in Browser** shows
  the dashboard in your browser.
- **Help > Install Terminal Command** writes a `qorqbot` command, the same as
  `./qorqbot install-command`. This is why the app's own launcher is called
  `qorqbot-desktop`.

## Update

Install a newer `.deb` the same way. On the next start the app offers
**Update Now** for the folder in `~/Qorqbot/qorqbot`. The updater has the same
limits as the terminal download: it supports only updates with unchanged schemas
and managed instruction contents, and it refuses updates with separately
installed connectors. Close agent terminals before updating. See
[README.md](README.md) and [UPDATES.md](UPDATES.md).

## Remove

`sudo apt remove qorqbot` removes the app. The folder `~/Qorqbot` stays, with
all your records. Delete it yourself to remove the data. Programs that apt
installed stay installed.

## Files

- `/opt/Qorqbot`: the app
- `/usr/bin/qorqbot-desktop`: the command that starts it
- `/usr/share/applications/qorqbot.desktop`: the applications menu entry
- an icon under `/usr/share/icons/hicolor`
- `~/Qorqbot`: the install folder (`qorqbot`), `launcher.log` and `server-port`

## Why a .deb, and the sandbox

The window is a Chromium window with its sandbox on. Ubuntu 24.04 restricts
unprivileged user namespaces for programs that are not installed system-wide.
So the package installs Chromium's `chrome-sandbox` helper owned by root with
the setuid bit. A package installed by apt can do that. An AppImage or a folder
to unpack cannot, which is why the app is a `.deb`.

## Qualification limits

The automated check covers:

- package contents and permissions
- install by apt on a clean Ubuntu 24.04 runner
- a fresh-home install, start, and the dashboard answering
- the window process starting with the sandbox on, under a virtual display
- a second launch with user namespaces turned off, so the window can only start
  through the root-owned `chrome-sandbox` helper, as on an Ubuntu 24.04 desktop
- stop on quit

It does not cover:

- a real desktop session with a person using it
- Wayland
- agent clients with real sign-in
- other distributions or other Ubuntu versions
- updating from an earlier `.deb`

## For the maintainer

The shell's source is shared with the Mac app, in
`qorqut-code/tools/distribution/macos_app/electron`. The values that differ per
platform are in `platform.js` there. Build the package from the exact Ubuntu
archive:

```sh
python3 qorqut-code/tools/distribution/linux_app/build_deb.py \
    --archive /abs/qorqbot-VERSION-ubuntu24-x86_64.tar.gz --output /abs/new-or-empty-dir
```

It writes `Qorqbot-VERSION-ubuntu24-x86_64.deb` and
`Qorqbot-VERSION-ubuntu24-x86_64.deb.sha256`. Install the package, then check it
and the installed app:

```sh
python3 qorqut-code/tools/distribution/linux_app/check_deb.py \
    --deb /abs/Qorqbot-VERSION-ubuntu24-x86_64.deb \
    --archive /abs/qorqbot-VERSION-ubuntu24-x86_64.tar.gz \
    --report /abs/desktop-check.json
```

Both run in the `linux` job of `.github/workflows/release-checks.yml`. See
[RELEASE-RUNBOOK.md](RELEASE-RUNBOOK.md).
