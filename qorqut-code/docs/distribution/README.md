# Qorqbot for Linux

For Ubuntu 24.04 on an Intel or AMD 64-bit computer.

## What you need

Open a terminal and install Python 3.12 and Git with this one command. It asks
for your computer password.

```sh
sudo apt install python3.12 python3.12-venv git
```

Your agents also need Google Chrome to work on websites. If it is not on this
computer yet, get it from https://www.google.com/chrome/

If something is missing when you start Qorqbot, it tells you what, and the one
command that installs it.

## How to start

In the terminal, go to the folder you downloaded Qorqbot to (for example
`cd ~/Downloads`), unpack it and start it:

```sh
tar -xzf qorqbot-VERSION-ubuntu24-x86_64.tar.gz
cd qorqbot
./qorqbot
```

The first start sets Qorqbot up. That takes about half a minute.

## What happens next

Your browser opens Qorqbot. Sign in with your email address. Then connect
Claude Code or Codex from the dashboard: Qorqbot downloads them itself and you
sign in to your own account there, in your browser. Nothing is typed in a
terminal.

## How to stop and start again

Keep the terminal window open while you use Qorqbot. Press Ctrl+C in it to stop
Qorqbot. To start again, open a terminal in the same folder and run `./qorqbot`.
Your records stay in that folder; keep it where it is.

# Details

## Requirements

Ubuntu 24.04 on Intel/AMD 64-bit, with Python 3.12 (with venv support) and Git
from Ubuntu's package manager, as above. Git 2.43 is the Ubuntu baseline; Python
must be 3.12 for this wheel set. Desktop viewing additionally requires its
configured TigerVNC/Openbox/X11/session tools. These programs are not included or
installed by Qorqbot.

Agents' web browsing needs Google Chrome installed on Ubuntu 24.04. The package
ships the `agent-browser` tool (`runtime/tools/agent-browser`). Agents' browsers
run on the installed Google Chrome, never on Chromium or another Chrome. Without
it the dashboard says Qorqbot needs Google Chrome, with a Get Chrome link, and
the agents' browser is set up by itself once it is installed. Qorqbot never
turns Chrome's sandbox off.

## Install and run

Verify the archive's SHA-256 against the release's checksum, then extract it to a
short writable folder. Run `./qorqbot` inside that folder and open the printed local
address if your browser does not open it. First launch creates a local Python virtual environment and installs the
included application wheels with standard pip, without network access. It creates a fresh instance,
never adopts an existing development installation. The set-up output is written to
`instance/logs/setup.log`; if set-up fails, the terminal shows the error and the
log's path (`.run/setup.log` while the instance does not exist yet).

`./qorqbot status`, `./qorqbot stop` and `./qorqbot verify`
are available from another terminal. Agents run as background sessions that close
by themselves when idle; the updater closes any that are open, and they start again
when you next message them. With the optional Terminals (legacy) mode, agents run in tmux terminals instead: nothing
closes them or starts them by itself, and the updater refuses while they run. Stop Qorqbot before deleting the installation. Application files live
in `runtime/`; your records/work live in `instance/operations/`. The browser
tool is part of `runtime/` and is replaced with it; a folder first installed
from an older release keeps the copy it downloaded in `tools/` until then.
Qorqbot's own Chrome, which earlier versions downloaded for a computer without
Google Chrome, is in `instance/operations/.runtime/chrome/`, so updates keep it. Qorqbot's own
configuration, profiles, logs and caches stay here. So do the folders Claude Code
and Codex keep their sign-in and settings in (`instance/home/claude` and
`instance/home/codex`): Qorqbot has its own sign-in for each, apart from the one
you use yourself, and your own instructions, skills and memory for those
programs do not reach your agents. A folder initialized by an earlier version
keeps using your own folders (`~/.claude`, `~/.codex`). Explicit `CODEX_HOME`
and `CLAUDE_CONFIG_DIR` settings are respected.

Keep the initialized folder at the same location: generated workspace bindings
contain absolute paths. The folder's path must be short (roughly 50 bytes or
fewer), because the agents' browsers keep Linux sockets inside it; a folder in
your home or Downloads folder fits. The optional Terminals (legacy) mode also
keeps its tmux sockets there. This folder placement is not a filesystem sandbox.

## Optional features

The app can run without a browser or an agent client. Install the external tools
when you need those features. Tools are found on your PATH. You can specify
`QORQUT_CHROME_EXECUTABLE`, `QORQUT_AGENT_BROWSER_EXECUTABLE` and `QORQUT_PYTHON`
with explicit executable paths. `QORQUT_AGENT_BROWSER_EXECUTABLE` replaces the
shipped browser tool. `QORQUT_CHROME_EXECUTABLE` makes agents use that Chrome.

Without it, a new agent browser uses the Google Chrome found on your PATH
(`google-chrome-stable` or `google-chrome`), and where there is none no browser
is created until it is installed. An existing agent browser keeps the Chrome it
was created with, including Qorqbot's own Chrome from an earlier version, which
Qorqbot keeps at its pinned version for that browser. Every Chrome runs with its
normal sandbox; Qorqbot neither changes AppArmor nor disables the browser sandbox.

Google Calendar is included but inactive. With Qorqbot stopped, run:

```sh
./qorqbot calendar-connect --name main --google-client /path/to/client.json
```

You supply the account/client configuration. WhatsApp is built in and dormant: ask
Ordubashy to set it up, or use Settings > Connections; no terminal step. Fastmail and
Telegram are separate optional downloads; you handle accounts and connection setup. Fastmail
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
contents, with core and the bundled Google Calendar, treg, OpenRouter and WhatsApp
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
license scope and notices.
