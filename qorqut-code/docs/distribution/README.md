# Qorqbot for Linux

Ubuntu 24.04 on Intel/AMD 64-bit. Install Python 3.12 with venv support and Git
using Ubuntu's package manager before running Qorqbot:

```sh
sudo apt install python3.12 python3.12-venv git
```

Qorqbot downloads Claude Code and Codex itself. You sign in to your own account
from the dashboard, in your browser; nothing is typed in a terminal.
Desktop viewing additionally requires its configured TigerVNC/Openbox/X11/session
tools. These programs are not included or installed by Qorqbot. Git 2.43
is the Ubuntu baseline; Python must be 3.12 for this wheel set.

Agents' web browsing needs Google Chrome installed on Ubuntu 24.04. The package
ships the `agent-browser` tool (`runtime/tools/agent-browser`). Qorqbot also
downloads its own Chrome (a pinned Chrome for Testing build, about 200 MB) in the
background once the app runs, and verifies its checksum before use. Ubuntu 24.04
as installed does not let a program outside the system folders use Chrome's
sandbox, so that Chrome does not start there and Settings says so; agents then
use the installed Google Chrome. Qorqbot never turns the sandbox off. Where its
own Chrome can start, nothing needs installing. If the computer is offline,
Qorqbot tries the Chrome download again by itself while the app runs.

## Install and run

Verify the archive's SHA-256 against the release's checksum, then extract it to a
short writable folder. Run `./qorqbot` inside that folder and open the printed local
address. First launch creates a local Python virtual environment and installs the
included application wheels with standard pip, without network access. It creates a fresh instance,
never adopts an existing development installation.

```sh
tar -xzf qorqbot-VERSION-ubuntu24-x86_64.tar.gz
cd qorqbot
./qorqbot
```

Press Ctrl+C to stop. `./qorqbot status`, `./qorqbot stop` and `./qorqbot verify`
are available from another terminal. Agents run as background sessions that close
by themselves when idle; the updater closes any that are open, and they start again
when you next message them. With the optional Terminals (legacy) mode, agents run in tmux terminals instead: nothing
closes them or starts them by itself, and the updater refuses while they run. Stop Qorqbot before deleting the installation. Application files live
in `runtime/`; your records/work live in `instance/operations/`. The browser
tool is part of `runtime/` and is replaced with it; a folder first installed
from an older release keeps the copy it downloaded in `tools/` until then.
Qorqbot's own Chrome is in `instance/operations/.runtime/chrome/`, so updates
keep it. Qorqbot's own
configuration, profiles, logs and caches stay here. Existing vendor clients keep
using their user-owned account configuration. Explicit `CODEX_HOME` and
`CLAUDE_CONFIG_DIR` settings are respected.

Keep the initialized folder at the same location: generated workspace bindings
contain absolute paths. Use a short path (roughly 50 bytes or fewer) for Linux
socket limits. This folder placement is not a filesystem sandbox.

## Optional features

The app can run without a browser or an agent client. Install the external tools
when you need those features. Tools are found on your PATH. You can specify
`QORQUT_CHROME_EXECUTABLE`, `QORQUT_AGENT_BROWSER_EXECUTABLE` and `QORQUT_PYTHON`
with explicit executable paths. `QORQUT_AGENT_BROWSER_EXECUTABLE` replaces the
shipped browser tool. `QORQUT_CHROME_EXECUTABLE` makes agents use that Chrome,
and Qorqbot then does not download its own.

Without it, a new agent browser uses Qorqbot's own Chrome once it is downloaded
and has started once on this computer. Until then, or when it cannot start here,
it uses an installed Chrome found on your PATH (`google-chrome-stable`,
`google-chrome`, `chromium` or `chromium-browser`). An existing agent browser
keeps the Chrome it was created with. Qorqbot's own Chrome does not update itself;
it changes only with a Qorqbot update. Every Chrome runs with its normal sandbox;
Qorqbot neither changes AppArmor nor disables the browser sandbox.

Google Calendar is included but inactive. With Qorqbot stopped, run:

```sh
./qorqbot calendar-connect --name main --google-client /path/to/client.json
```

You supply the account/client configuration. Fastmail, Telegram and WhatsApp are
separate optional downloads; you handle accounts and connection setup. Fastmail
requires your separately configured email engine URL and private token file.
Install a trusted connector while Qorqbot and its agents are stopped:

```sh
./qorqbot extension-install /path/to/connector.tar.gz --sha256 VERIFIED_HASH
```

Use `--config /path/to/config.json` when required. The wrapper uses standard pip
for the connector wheels and Qorqbot's own migration/configuration routines for
registration. Interrupted installation requires the same command with `--resume`.
It does not install vendor programs, log into accounts, or send provider work.

## Backup, update and removal

Back up the whole folder while Qorqbot and its agents are stopped. Update with:

```sh
./qorqbot update /path/to/qorqbot-VERSION-ubuntu24-x86_64.tar.gz --sha256 VERIFIED_HASH
```

This release supports only updates with unchanged schemas and managed instruction
contents, with core and the bundled Google Calendar, treg and OpenRouter
extensions only. Updates with separately installed
connectors are refused until compatible updates are qualified. Instance data is
never replaced. An interrupted update can be recovered with
`./qorqbot update --recover` before the new app is admitted to run.

To remove Qorqbot, stop it and its agents, retain any desired backup, then remove
its folder. This deletes its data. It does not uninstall your external tools.

## License and support

First-party Qorqbot is source-available under the PolyForm Noncommercial License
1.0.0, by 49 Agents LLC, in LICENSE. Personal and noncommercial use is free.
Commercial use needs a per-person commercial license, free until 2027-01-01:
https://qorqbot.com/commercial-license/. Dependencies retain their own licenses. See the source release's
license scope and notices. A private candidate is not a published release.

Source build/acceptance instructions are in RELEASE-RUNBOOK.md and PUBLICATION-PIPELINE.md.
