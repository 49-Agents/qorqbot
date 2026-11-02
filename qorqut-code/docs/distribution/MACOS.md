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
macOS may hide another process's environment. If safe ownership inspection is
unavailable, updates and connector installation refuse; ordinary app startup,
status and stop use the saved process identity. This requires native acceptance.
The Linux X11/systemd desktop stack is not available on Mac; ordinary Chrome
browser automation is separate. Shared Codex app-server writer-lock attribution,
legacy recovery administration and managed Agentic upgrade paths retain explicit
platform limitations where Linux kernel facilities have no verified equivalent.
The Mac check must record these limits; passing static checks is not native
acceptance. Existing Linux archives remain unchanged.

For the maintainer: export one reviewed source commit, run prepare-local.py and
native_build.py with `--target macos14-arm64`, then run `check_posix.py` against
the exact archive on an Apple silicon Mac. The prepared Mac workflow performs
that sequence. This command-line distribution does not add an app bundle,
installer service, updater feed, VM or a new signing/notarization system.
