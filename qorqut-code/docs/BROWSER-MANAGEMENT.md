# Worker browser management

Ordubashy can set up, check, repair, restart and stop any active permanent worker's own browser with one MCP operation,
**`agent_browser_manage`**, without asking the owner for approval. It is routine maintenance of the worker's own configured
browser, not a new privilege: it grants no authority to spend, submit forms, act as the owner or touch another service.

Workers never run it. A worker whose browser is missing or broken gets a structured error
(`browser_not_configured` or `browser_configuration_invalid`) telling them to ask Ordubashy; they report it once and keep using
`browser_turn` afterwards.

## Operation

```json
{"agent_id": "<worker UUID>", "action": "status"}
{"agent_id": "<worker UUID>", "action": "ensure",  "idempotency_key": "setup-atlas-1"}
{"agent_id": "<worker UUID>", "action": "repair",  "idempotency_key": "fix-atlas-1"}
{"agent_id": "<worker UUID>", "action": "restart", "idempotency_key": "restart-atlas-1"}
{"agent_id": "<worker UUID>", "action": "stop",    "idempotency_key": "stop-atlas-1"}
```

There is no path, executable, endpoint, flag, port, service name or shell argument: the host derives all of them. Mutations
need an `idempotency_key` (1–200 printable characters); `status` needs none and changes nothing.

| Action | Effect |
| --- | --- |
| `status` | Reports `missing`, `healthy`, `stopped`, `busy`, `target_missing`, `conflict`, `invalid` or `unsupported`, the ownership mode and the next action. |
| `ensure` | Creates a dedicated managed browser when the worker has none; starts a stopped owned one; leaves a healthy browser untouched. Repeating it never duplicates a browser. |
| `repair` | Diagnoses; restores a missing generated `browser.json` from saved ownership; starts a stopped owned browser; restarts an owned browser that does not answer. A healthy browser is a no-op. |
| `restart` | Restarts exactly the owned browser. Refused with `browser_in_use` while a browser command is running. |
| `stop` | Stops exactly the owned browser. Same refusal while a command runs. |

A receipt names the target, action, whether anything changed, the resulting state, that the profile was preserved, a bounded
diagnostic and the verification result. A managed browser is reported `healthy` only when a real, bounded CDP health check
succeeds *and* the answering process is the exact supervised Chrome that owns the port (recorded boot-bound identities plus
listener ownership); another process answering CDP on that port is reported as such (`stopped`), never adopted, stopped or
called healthy. A failed or timed-out start is reported as a failure, never as ready. No cookies, page content, credentials or arbitrary logs
are returned. A repeated key replays the saved receipt (`"replayed": true`); a restart or stop whose earlier attempt did not
report completion is reported `"outcome": "uncertain"` with the current state and is not repeated blindly.

Only the configured Ordubashy (and the installation owner, outside MCP) may manage, and only an *active permanent global
worker*, including workers created later. Ordubashy himself, the Learning Applier, inactive workers and business agents are
refused; a worker with an `allow` override is still refused, and an explicit `deny` override on Ordubashy is honored.

## Managed headless browsers

`ensure` on a worker without a browser creates a **dedicated headless managed browser**:

* one persistent headless Google Chrome (`/usr/bin/google-chrome-stable`, normal sandbox — never `--no-sandbox`), loopback
  CDP only, starting on `about:blank`;
* a separate profile per worker at `operations/.runtime/agent-browsers/<agent UUID>/profile`, kept private (`0700`);
* a CDP port from the reserved range **20400–21399** (business browsers use 19400–20399). The port is chosen *and* the
  worker's ownership record written as one step under a global allocation lock (released before Chrome starts), so two workers
  can never pick the same free port; a port saved for another worker, configured in any workspace or deployment file, or with
  a live listener is skipped. A reservation interrupted before the browser ran is picked up again by the next `ensure`/`repair`;
* a host-owned supervisor (`python -m qorqut.browser.agent_supervisor --state <dir>`) run as a **transient systemd user
  service** (`systemd-run --user --unit=qorqut-agent-browser-<installation hash>-<worker UUID>.service --collect`, fixed argv and
  environment, no shell). It therefore lives outside the application service's cgroup and **survives application restarts and
  releases**. A unit that is already running is reused, never duplicated. The supervisor holds a per-worker process lock, records
  boot-bound identities for itself and Chrome, and publishes `ready` only after the CDP endpoint answers *and* that exact Chrome
  owns the listener;
* the user manager does not share the application service's mount namespace. When the service sees its instance through a
  private bind mount (`BindPaths=`), the supervisor is started with the directory's path on the underlying filesystem and is
  told the path the ownership record was written with (`--recorded-state <dir>`), so both sides read and lock the same files;
* Chrome's temporary directory is `<dir>/tmp` when its singleton socket path fits a Unix socket address, and otherwise a
  private `qorqut-browser-<hash>` directory under `/run/user/<uid>` (Chrome aborts on a longer socket path);
* a generated `.qorqut/browser.json` (unchanged schema: `schema_version`, `cdp_endpoint`, `runtime_dir`) written only after
  the browser is verified, using the installation's pinned browser runtime.

Ownership (`meta.json`) is written before launch, so a timeout can never lead to a second browser. A random listener on the
reserved port is never adopted; processes are signalled only by their recorded identity, never by port, name or unverified PID.
Chrome's profile lock files are never removed. A worker's own browser commands re-check the managed identity under the use lock,
so a worker never talks to a foreign process that took over a dead browser's port; it gets `browser_not_ready` pointing to
Ordubashy. After a host reboot (or any other loss) `status` reports `stopped` and `repair`/`ensure` starts the same profile
again. `stop` ends exactly that supervisor (and, only if it ignored the stop, exactly its derived unit).

## Preservation

Profiles, cookies, logins, `browser.json` and ownership metadata survive `restart`, `stop` and `repair`. There is no delete
action: nothing wipes a profile, replaces a configured target or silently assigns a different port or profile. A target file
that differs from the saved record is reported `conflict` and left untouched.

## Existing browsers and the legacy binding

A healthy existing `.qorqut/browser.json` is never replaced, migrated, cloned or restarted by `ensure`. The file alone does
not prove which OS service runs the browser, so `status` and a healthy `ensure` work, but `repair`, `restart` and `stop` of an
**unbound** existing browser answer `browser_management_unconfigured`. That is an installation step, not a missing permission.

The operator binds the exact unit once, from a shell on the host, as the application's OS user:

```sh
python -m qorqut.browser.agent_binding bind --instance-root <instance> [--component NAME=PATH ...] \
    --agent-id <worker UUID> --scope user|system --unit <name>.service \
    --profile <absolute profile directory> --endpoint http://127.0.0.1:<port>
```

It refuses anything it cannot prove: the workspace `browser.json` must name exactly that endpoint; the unit must exist and its
static definition (unit file, drop-ins, referenced JSON) must name both the profile and the endpoint port; the unit, profile and
endpoint must not belong to another worker. It stores a fingerprint of the unit definition and copies no credential or profile.
Registration takes the same per-worker lifecycle and global allocation locks as a manager action, so it cannot race one or a
second registration (`browser_management_busy` if one is running).
Afterwards `repair`, `restart` and `stop` act only on that exact unit through a fixed `systemctl` argv, re-verifying the binding
and fingerprint first (a changed unit is a conflict), and a missing `browser.json` is restored from the saved record.

**Before promising a restart of an already running agent browser, bind it.** Until then only `status` and a healthy
`ensure` can be offered.

## Installation requirements

* Chrome installed at `/usr/bin/google-chrome-stable` and the installation's pinned browser runtime prepared; otherwise
  `status` reports `unsupported` and `ensure` returns `browser_capability_missing` (nothing is downloaded).
* User namespaces available to the application user so Chrome's normal sandbox works.
* A running **systemd user manager for the application user**, reachable from the application service: lingering enabled
  (`loginctl enable-linger <application-user>`) and `/run/user/<uid>` available. Otherwise managed `ensure`/`repair` report
  `browser_service_unavailable` naming the user manager; nothing is started inside the application service as a fallback.
* Nothing root-owned is needed for new managed browsers: the application service runs as the configured application user and creates them itself, and the
  application unit itself is unchanged.
* A legacy unit with `--scope user` is controlled with `systemctl --user` in the application user's session. A `--scope system`
  unit needs a noninteractive sudoers rule for exactly `systemctl start|restart|stop <that unit>`; without it the operation
  returns `browser_privilege_unavailable`.
