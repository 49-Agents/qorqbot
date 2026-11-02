# Native Linux distribution

The first download target is Ubuntu 24.04 on Intel/AMD 64-bit machines. Qorqbot
runs directly from one writable folder. Its code and bundled tools live in
`runtime/`; records and work live in `instance/operations/`. Configuration,
profiles, logs, caches and local sockets also stay inside this folder.

Extract the archive to a short path, then run `./qorqbot`. Open the local address
printed by the launcher. Press Ctrl+C to stop the application. `./qorqbot status`
and `./qorqbot stop` work from another terminal. Agent terminals have independent
lifetimes; close them before updating or deleting an installation.

First launch installs the bundled Python wheels without network access and
creates a new instance. It never adopts an existing development installation.
Keep the chosen folder at the same location after initialization: generated
native hooks and authored workspace bindings contain absolute paths. Linux
socket limits require an installation path of roughly 50 bytes or fewer.

Users authenticate their own coding clients and connect their own accounts.
Core includes calendar operations and email guidance. Google Calendar is bundled
without an account or active connection. With the app stopped, connect it using
`./qorqbot calendar-connect --name main --google-client /path/to/client.json`.
The local OAuth helper stores the resulting account configuration under
`instance/config/`. Fastmail, Telegram and WhatsApp are separate optional installs.
Paper and content creation are outside this distribution.

Install a downloaded connector bundle while the app and its agent processes are
stopped: `./qorqbot extension-install /path/to/connector.tar.gz --sha256 VERIFIED_HASH`.
The trusted distribution channel must supply that checksum separately. The
installer verifies the bundled wheels, installs offline, backs up affected state,
and adds the connector's managed instructions and storage before selecting it.
If interrupted, repeat the same command with `--resume`; startup stays blocked
until installation completes. Existing package replacements are refused. This
local installer does not select a hosting service or provide an extension updater.

Browser automation has an additional operating-system prerequisite. Stock Ubuntu
24.04 can block the bundled Chrome sandbox when it runs from an arbitrary extracted
path, because AppArmor restricts unprivileged user namespaces. Core application
startup does not need Chrome. Browser use needs an administrator-approved namespace
sandbox policy for the chosen executable, or an explicitly configured compatible
installed browser. This package does not change host policy or automatically find
an installed-browser fallback. See [Chromium's explanation](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md)
and the [Ubuntu 24.04 release notes](https://documentation.ubuntu.com/release-notes/24.04/#unprivileged-user-namespace-restrictions).

Browser checks on a configured host or inside Docker do not establish browser
acceptance on a fresh stock Ubuntu host. Record that qualification separately.

The folder placement is not a filesystem sandbox. It does not introduce a new
authorization system or prevent an authorized agent from using another folder.

See [the release runbook](RELEASE-RUNBOOK.md) for builds and qualification, and
[updates](UPDATES.md) for the deliberately bounded offline update path.
An application license and public distribution destination must be selected
before publication. A development candidate is not a qualified public release.
