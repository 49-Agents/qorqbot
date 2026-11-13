# Offline updates

The native launcher accepts an explicitly downloaded local package:

```sh
./qorqbot update /path/to/qorqbot-VERSION-ubuntu24-x86_64.tar.gz --sha256 EXPECTED_SHA256
```

Verify the expected checksum through the trusted distribution channel separately.
An embedded checksum is integrity metadata, not publisher authentication. The
dashboard tells the owner when a newer release exists and links to the download
page (`docs/ACCOUNT.md`), but there is no automatic download, automatic apply or
extension marketplace.

Stop the application and its agent terminals first. The updater takes both the
launcher and instance lifetime locks, refuses running terminals, validates the
archive paths and payload hashes, refuses detached installation-owned browser/helper processes, including system executables,
and checks the current database identity.
It replaces only `runtime/`. It never replaces `instance/`, restores a database,
replays provider work or starts an agent.

The first updater supports unchanged database schemas and unchanged managed
instruction contents with core and the bundled Google Calendar provider. A
read-only managed plan checks the retained binding before the new application
can start. A new build always changes the provenance its catalogs record for packaged
sources (commit, tree, snapshot and manifest hashes, source root) even when no
instruction changed; that provenance-only difference is admitted without editing the
installed catalogs on either Linux or macOS. Their per-file digests still prove the
bytes unchanged. Admission never starts a managed update, so runtime recovery cannot
leave a partial managed update behind. Any other instruction adoption/conflict, schema changes and separately installed connectors need
a compatible, qualified update bundle/migration; unsupported updates are refused
before replacing the runtime. Future migrations must preserve historical dates,
provider receipts, authored instructions, credentials and native-session identity.

The old runtime and `.run/update.json` are retained. A setup failure before the
new application starts restores the old runtime, preserving failed bytes for
diagnosis. A machine interruption fences application startup. Run
`./qorqbot update --recover` to restore the prior runtime before the replacement
has ever started. This never restores or changes the database. Completed records
and previous runtimes move into `.updates/` before the next update; keep them
until the replacement has been verified. There is no automatic post-start rollback
and no fallback to an older binary against a newer schema.

Before publishing a new version, exercise a same-schema upgrade with synthetic
records and verify their identity and contents after restart. Exercise refusal of
incompatible schemas, bad checksums, escaping archive paths and active sessions.
Only advertise additional upgrade cases after their acceptance checks pass.
