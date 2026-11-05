# BusinessSandbox full recovery

`FullRecoveryStore` creates one encrypted recovery generation for the whole
installation. It composes the existing `BackupStore` validation of authoritative
SQLite, registered HQ data, artifacts and credential projections with a pinned
[restic 0.19.1](https://restic.readthedocs.io/en/stable/) snapshot of the state
that `BackupStore` intentionally excludes: every registered business's `code/`
tree (including `.git` and worktrees), sandbox homes/downloads, browser profiles,
and private browser-proxy references. Restic records symbolic links as links and
does not follow them, so a checkout link outside the business mount is preserved
without making that external location part of the backup.

The caller passes `create(generation_id, quiescence=...)` a trusted context
manager. Its `__enter__` must return only after the service, every business
writer, an explicit legacy-host inventory, and browser writer leases are drained.
The store deliberately has no boolean, PID check or claimed token that could
assert consistency without causing it. The scope remains entered while the SQLite
snapshot and encrypted filesystem snapshot are made. If either capture or verification fails, no retention runs and
the short-lived unencrypted authority staging copy is erased.

Provision restic outside the business mounts and construct `ResticBinary` with
the executable and its SHA-256 from the official release's `SHA256SUMS`; the
module verifies that digest and requires exactly version 0.19.1 before use. The
repository, password file and authority staging directory must each be outside the
business mount, must not overlap one another, and cannot cross symlinks. The
password is supplied only with restic's password-file option; command output is
captured and never logged by this module. A repository must already be initialized
unless the trusted caller sets `initialize_repository=True` for a newly created
empty path; that prevents capture from initializing an unrelated directory. Every
restic command re-hashes the pinned executable and has a configured bounded timeout.
Repository verification runs `restic check --read-data-subset=100%` before a new
generation can cause `forget --prune` retention. Only tagged full recovery snapshots
participate in the bounded daily/weekly retention policy; they use a stable
installation host grouping so each generation's unique staging path does not defeat
retention.

Only named provisioner lifecycle files beside each sandbox home and Chrome's named
singleton sockets are excluded. Homes, browser profiles, downloads,
`sandbox-host.json`, `preview-host.json`, browser proxy references and private
configuration are otherwise captured as-is. The recovery-administration config is
intentionally external: it references the repository, password file, staging and
local socket, so it must never be captured inside the generation it controls. Do not broaden those exclusions without showing that the omitted
bytes are neither credentials nor reusable state.

`restore(generation_id, destination)` rehearses into an empty directory outside
the live businesses root. `restore_original_root(generation_id, quiescence=...)`
prepares an original-path restore after the trusted caller has retired that root:
the original root must be absent or empty, all installation writers must stay
quiesced, and the method refuses to merge or overwrite a nonempty root. Both
restore paths close SQLite maintenance and file-authority gates, retain original
absolute database bindings, and start no service. The API has no per-business
restore because SQLite is installation authority: restoring one business while
others continue would silently roll back their shared operational state.

`Runtime.recovery_quiescence()` supplies the in-process portion of the trusted
context. It closes mutation admission and waits for admitted applications, then
uses bounded `stop(timeout=...)` joins for signoffs and the attached background
service. Runtime integration must register a browser drain controller and the
sandbox reconciliation worker. When labeled sandbox containers are running, the
barrier refuses capture without that browser controller; after its lease drain it
pauses only the exact installation/business labeled containers through Docker and
reads back the paused state. It always unpauses them and reopens admission when
the recovery context exits. Legacy host terminals require a separate explicit
inventory/freeze controller and otherwise cause refusal.

The context manager yields the barrier's active proof (`qorqut_operations.storage.RecoveryBarrierProof`), issued only after
every drain and freeze succeeded, the boundary is closed, claims are clear and the exclusive installation lock is held. It
is the issuing window itself, not a flag: `verify_active()` refuses it outside that window, in another thread or process,
and after the frozen database file is replaced; `require_database(database)` also refuses any other database. It is
revoked before anything is thawed, resumed or unlocked, on every exit, and never valid in a later window. A second window
(for example recovery administration while the nightly capture holds one) is refused as retryable without touching the
running one. The nightly capture and recovery administration are recovery orchestrators: the barrier does not drain them,
and shutdown drains them first; one whose capture is still in flight keeps every service below it, write admission and
SQLite untouched and the shutdown incomplete. Shutdown's own refusal of new mutations (`begin_shutdown`) is independent of
the window's: a window thawing while the stop waits cannot reopen them, and no window starts once shutdown has begun.
See `docs/v1.2/RUNTIME_LIFETIME.md`.

## Local recovery administration

`RecoveryAdministration` is an inert library controller for the three safe online
operations: `create`, `verify`, and `list`. Composition supplies its real
`Runtime.recovery_quiescence` context factory and starts it only after the runtime
is fully composed. It does not accept a claimed-quiescent value, use application
mutation admission, or run as the background worker, so capture cannot wait for
itself. Restore is deliberately absent from this interface; rehearsal and
original-path activation remain offline all-installation procedures.

The controller listens only on the absolute `socket_path` in a small mode-0600
private host configuration, outside the businesses mount. It refuses symlinked,
FIFO, public-parent, or pre-existing socket paths, verifies Linux `SO_PEERCRED`
against the configured owner UID, and bounds each JSON line request and response.
It returns only generation, snapshot, and timestamp data and never returns command
output, repository paths, or password references. `python -m
bazarlyk.sandbox.recovery_admin --businesses-root ROOT --config PRIVATE.json
create GENERATION_UUID` is a local UDS client; it is not an HTTP/MCP command.

The config has exact schema version 1 and references the pinned restic executable
and SHA-256, repository, password file, authority staging directory, retention,
owner UID, and registered businesses root. All recovery paths must be absolute,
non-overlapping, and outside the businesses mount. Starting the controller creates
no repository implicitly beyond the explicit `initialize_repository` contract of
`FullRecoveryStore`; stopping waits a bounded time and reports an in-flight capture
instead of claiming it has stopped.
