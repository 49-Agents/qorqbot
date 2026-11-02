# Qorqbot on Windows with WSL2

First target: Windows 11 on Intel/AMD 64-bit, with WSL2 and Ubuntu 24.04 installed
by the user. This runs the same Linux package. Qorqbot does not install WSL,
change its global settings or distribute a private Linux image.

If WSL is not set up, follow [Microsoft's WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install).
The explicit Ubuntu installation command in administrator PowerShell is:

```powershell
wsl --install -d Ubuntu-24.04
```

Restart if Windows requests it, open Ubuntu, and finish its normal user setup.
Confirm that `wsl --list --verbose` reports version 2 for the chosen distro.
Inside Ubuntu install the application prerequisites:

```sh
sudo apt update
sudo apt install python3.12 python3.12-venv git
```

Download/extract the supplied Linux TAR.GZ **inside your Ubuntu home**, for example
`~/qorqbot`, rather than running it from `/mnt/c`. The ZIP is only a convenient
Windows handoff containing the unchanged Linux archive and this guide. It is
not a native Windows executable or an installer.

```sh
cd ~
# Replace the example path with the downloaded archive's actual Windows path.
tar -xzf '/mnt/c/Users/YOUR_USER/Downloads/qorqbot-VERSION-ubuntu24-x86_64.tar.gz'
cd qorqbot
./qorqbot
```

Open `http://localhost:1049` in your Windows browser. If you change the app port,
use that port. Standard WSL localhost forwarding is required; no firewall or
network configuration is changed by Qorqbot. Ctrl+C stops the foreground app.
Use `./qorqbot stop`, `status` or `verify` inside the Ubuntu folder as needed.

For later starts, open Ubuntu and run `cd ~/qorqbot && ./qorqbot`. All application
state stays under that Linux folder, including `instance/operations/`. Keep it
at the same path after setup. Back up the stopped folder before updates/removal.
Do not unregister the WSL distro as an application-uninstall method: that would
delete other Linux data too.

Install tmux and your chosen Linux Codex/Claude Code client **inside Ubuntu** for
agent terminals. The same applies to Chrome/agent-browser and optional desktop
tools used by agents. The Windows browser displays the dashboard; it does not
replace the agent's Linux automation browser. Accounts are user-managed.
Fastmail, Telegram and WhatsApp use the same separate Linux connector bundles.
The normal Linux guide covers provider setup and supported updates.

The Linux payload is qualified independently. Actual Windows/WSL launch,
localhost forwarding, restart and data preservation still need a Windows check;
a ZIP built on Linux is not proof of Windows host acceptance. No source upload
or repository visibility change is part of this preparation.

Microsoft also recommends keeping Linux work in the
[WSL filesystem](https://learn.microsoft.com/en-us/windows/wsl/filesystems).
