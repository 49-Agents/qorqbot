# Optional email engine configuration

This separately installed service supplies the Fastmail extension's account API and private integration APIs. A core Qorqbot
installation neither starts nor configures it. The removed Fleet launcher and persona lease module are not required.

Run `python -m engine_email` with a private environment containing these settings. No provider credentials belong in source,
source exports or distributable packages. The administration, read and send tokens must be distinct and at least 32 ASCII characters.

| Setting | Purpose |
| --- | --- |
| `FASTMAIL_API_TOKEN` | JMAP account credential held only by the engine. |
| `FASTMAIL_ACCOUNT_ID` | Optional account selection; otherwise the primary account is selected and pinned. |
| `EMAIL_DATABASE` | Absolute path of the durable SQLite ledger. |
| `EMAIL_READ_TOKEN`, `EMAIL_SEND_TOKEN` | Separate trusted client credentials. |
| `EMAIL_ADMIN_TOKEN` | Required by the optional Fastmail extension's account API; separate from read/send credentials. |
| `EMAIL_HOST`, `EMAIL_PORT` | Listen address and port; use a private local binding. |
| `EMAIL_SYNC_SECONDS` | Poll interval, default 120 seconds, minimum 30. |

Leave `EMAIL_MAILBOXES_FILE` unset for account-only use. There are then no registered business mailboxes, the business-folder
sync loop is idle and `/mailboxes` is empty; `/fastmail` still serves the owner account through the administration credential.
The Fastmail extension receives the explicit engine URL and the path of its administration token file.

## Optional features

These settings name private JSON files. Their schemas live in `engine_email.models`; none is enabled by installation.

- `EMAIL_MAILBOXES_FILE`: registered business mailbox mappings. Existing account/folder/address ownership cannot be reassigned.
- `EMAIL_SHARED_SENDERS_FILE`: shared sender profiles, disabled until explicitly enabled. Exact approved transport, recipients,
  footer and expiry remain binding. Shared accounts use atomic rolling limits and a separate installation-wide ceiling.
- `EMAIL_HISTORY_FILE`: encrypted private history configuration, including archive database and key file locations. See
  [history](HISTORY.md) for its format, scope and recovery limits. Enabling account-wide history does not grant other agents access.
- `EMAIL_OUTLOOK_WEB_FILE`: optional Outlook browser transport settings; requires an independently configured authenticated session.
- `EMAIL_INTEGRATIONS_FILE`: scoped trusted integration clients. See the [integration contract](INTEGRATION-CONTRACT.md).

The optional Outlook bridge requires the pinned `playwright-core` dependency beside its own bridge file and a supported Node
runtime. `python -m engine_email.outlook_dependencies check --bridge <bridge-file> --require` checks these dependencies offline,
without opening a browser or contacting a provider. Missing or unloadable dependencies produce specific readiness errors and
never switch to a different sender. The opt-in `stage` subcommand prepares dependencies in a new directory for a separate build;
it does not change or start an installed service.

## Receipt and recovery contract

Keep request IDs and the send ledger. A provider-accepted submission awaiting archive reconciliation is not permission to send
again. Reconcile the same request; never reset its ledger or manufacture a replacement ID. Existing historical payload hashes,
mailbox bindings and transport choices remain unchanged. Back up SQLite with its backup API, including the separate engine ledger.

### Draft edits and unrelated incoming mail

Draft replacement advances the caller's Email state only after a bounded
`Email/changes` read proves the selected draft was not created, updated or
destroyed since that state. It checks again immediately before importing the
replacement and deleting the old draft. A definite method-level `stateMismatch`
can repeat that conditional write once; uncertain writes are never repeated.
Missing change history or an incomplete delta refuses the edit. This preserves
concurrent-edit protection without making unrelated incoming messages block it.
The account read/update API and request IDs are unchanged. JMAP email bodies are
immutable, so the existing replacement-draft ID is returned; callers must adopt
that ID and inspect `old_draft_retained` before further editing.
See RFC 8620 section 5.2 and RFC 8621 section 4.8.
