# Email Engine

Fastmail JMAP business mail service with an optional owner-configured Outlook web
sender and private reply collector for Qorqut. No legacy email repository is
read, imported, or required. This is a service with a send worker, not an agent
plugin. Agent procedures live in the separate email-management and Paper
(email-outreach) repositories; Qorqut owns assignments and approvals.


In Qorqut v1.32 this service is the optional transport and send ledger of the separate `qorqut-fastmail` package. Qorqut
core neither starts, packages nor configures it. The ordinary owner-account path (`/fastmail`, authenticated with the
distinct administration token) needs **no business-mailbox registration**: `EMAIL_MAILBOXES_FILE` may be omitted or an
empty array, in which case the business-folder sync loop is idle and `/mailboxes` lists nothing. Business mailboxes,
shared senders and managed Outreach routes remain optional engine features.

```text
Claude agents / owner dashboard
             |
    Qorqut HTTP/MCP + owner review
             |
    durable Qorqut background jobs
             |
    this service on dashboard host
             |
    Fastmail JMAP -> dedicated business folders
```

## Run And Test

Python 3.12 or later:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
.venv/bin/python -m engine_email
```

The tests use local fixtures and mock HTTP. They do not authenticate Fastmail,
send real mail, or run Claude. Controlled live acceptance is still required.
See [configuration](docs/CONFIGURATION.md) before running the service with credentials.

## Send Guarantees

Only the send credential accepts a fixed outgoing message. The read credential
cannot send; it can cancel a queued, not-yet-started send. Both belong to trusted services, never agents. A request ID can
identify only one exact payload. Sender and business must match the registered
mailbox. Account, business-folder and address bindings cannot be reassigned.

Optional owner-configured shared sender profiles select an external connected
Fastmail identity or the explicitly configured local Outlook browser session,
while retaining the original business mailbox folder. Profiles
can grant selected businesses or all current and future registered mailboxes. One
all-business profile may supply the outreach default and shared writing context.
Default selection is drafting metadata; execution retains the exact approved mail.
The approval and durable ledger bind the transport; missing historic transport
means Fastmail. A configuration change cannot reroute an uncertain send. A configured
required footer must already be the exact final nonblank line in approved text.
Profiles remain disabled until their external authenticated sending path passes acceptance.
All configured aliases for an actual external account share an atomic rolling
24-hour allowance (default 250), with an additional installation-wide ceiling of
220 across all shared senders and businesses. The lower available allowance wins. Unresolved reservations hold capacity until
reconciled. Queued sends resume in the engine before their exact approval deadline;
Qorqut reads the existing receipt. See
[shared senders](docs/CONFIGURATION.md#optional-features).

The service saves a provider draft's immutable ID, verifies approved recipients,
body and attachments, records its sending intent, then creates one JMAP
EmailSubmission. The creation response is saved immediately: JMAP permits the
provider to remove a completed submission before a later lookup. A saved `final`
receipt remains valid across restarts; pending receipts are reconciled by lookup.
Acceptance alone is not completion. Network uncertainty remains
unconfirmed; it never permits a blind retry. `submission_confirmed` means the
provider has irreversibly relayed the message to at least one recipient. It does
not prove delivery to every recipient's inbox.

One Fastmail account can hold many business addresses. Native address actions
file their mail into dedicated business folders; the engine never guesses
ownership from visible To/From headers. Ambiguous folders are excluded. The
Fastmail token can access the account, so application folder isolation is not a
provider-level permission boundary. Keep the token private to this service.

Provider mail is authoritative. Cached messages, folder cursors and Qorqut's
read-only context can be rebuilt. **The send ledger and mailbox bindings cannot
be discarded.** Back up the database using SQLite's backup API, not a live copy
of just the database file. To rebuild mail, reset saved folders with
`EmailService.reset_sync`; preserve the revision counter, bindings and send ledger.

The opt-in private archive can capture future observed changes across the entire
Fastmail account, including personal and nonbusiness folders. It retains raw MIME,
attachments, provider metadata and folder catalogs in an encrypted SQLite archive
for independent research and data science. Its collection scope does not grant
business inbox access. See the [archive format and limits](docs/HISTORY.md).

## Deliberately Small

One business mail provider, an optional bounded Outlook adapter, one polling
service, a durable SQLite ledger and its own reconciliation loop.
No campaign automation, agent waking, forwarding classifier, generic provider
proxy, webhook infrastructure, account administration API or plugin execution
framework. Body format is plain text; attachments are limited to ten files and
20 MiB combined, and outgoing messages to fifty unique recipients.

Useful later simplifications: remove unused provider wrappers when live testing
identifies them; centralize the few duplicated wire limits only if they drift.
Do not weaken approval snapshots or uncertain-send handling to shorten code.

### Managed outreach routes

A trusted core caller can give each business an archive-only mailbox bound to one
shared sender, without a public address or another login. See
[managed outreach routes](docs/INTEGRATION-CONTRACT.md#managed-outreach-routes).

### Managed mailbox registration

The optional `EMAIL_ADMIN_TOKEN` enables `POST /mailboxes/configure` for the
trusted core service. Use a separate random token of at least 32 ASCII characters,
distinct from read/send credentials, in private service environment files only.
Read and send tokens cannot administer routes. Leave it unset to disable this API.

The request contains `request_id`, `expected_revision` (0 for an unmanaged/new
route), and `mailbox` with `ref`, `folder_id`, `business_id`, `address`, `aliases`.
Core supplies the authorized business ID. The address and aliases must already
have valid Fastmail sending identities; the folder must be dedicated and readable.
The API does not create provider accounts, DNS records or send test messages.

Routes and request receipts are stored atomically in the existing SQLite database.
`GET /mailboxes` includes `mailbox_revisions`. Retry an uncertain request with its
original ID and payload; reread revisions before making a different change.
Existing mailbox/folder/address ownership is immutable, including historical
alias bindings. Managed alias settings override static aliases after restart;
conflicting static ownership fails closed. Back up this database along with the
static configuration; managed routes cannot be recovered from the JSON file alone.

Registration resets only the account listing cursor for a normal provider replay.
Existing cached messages, send receipts, history and shared quota reservations
remain intact. Core imports the new route through its ordinary business email
projection. Deploy this engine and core's business email tools together, configure
the same private administration token on both services, then adopt the updated
Ordubashy role. Registration itself does not restart services or agent terminals.

### Business mailbox views and ownership hints

The read-credential `/mailboxes/{mailbox}` surface also serves read-only
provider-side business views: `search`, `drafts` (listing only), and ranged
`attachments`/`attachment` pages mirroring the `/fastmail` semantics
(offset/limit, at most 5 MiB per request). A business owns exactly the mail in
its bound folder; for mail a workflow moved into the account's shared
Drafts/Sent/Trash, the trusted core service passes explicit `owned_ids`
ownership hints. A hint never opens another business's folder or a
private-history folder, and provider-wide totals are withheld from
shared-folder views so foreign counts do not leak. These views never mutate
provider state and do not extend the read credential's write powers: moves,
trash, restore, flag updates and draft creation stay on the admin-token
`/fastmail` account surface with its state pins and protected-folder guards,
with business-scope authorization enforced application-side in core. See
`engine_email/business.py` for the exact contract.

### Owner account mail controls

The separate `EMAIL_ADMIN_TOKEN` also gates the explicit `/fastmail` account
surface used by the owner-authorized Ordubashy capability. It lists the pinned
Fastmail account, folders and sender identities, and supports bounded account
search, message/thread reads and attachment ranges. Account mutations use one
strict action schema and a request ID; the SQLite operation ledger is written
before any provider write and stores completed, failed or uncertain receipts.
Retries replay a saved receipt and never resend an uncertain submission. The
surface does not expose the read/send credentials or arbitrary JMAP calls.
Registered business and private-history folders cannot be renamed or deleted;
ordinary Fastmail aliases and rules remain provider settings.

### Integration envelope for automated cold-outreach callers

An optional `EMAIL_INTEGRATIONS_FILE` registers scoped trusted-service
credentials (e.g. the Paper outreach service) with their own `/integrations/*`
routes: idempotent send-intent submission, lookup, cancellation, and thread
handoff. These credentials cannot use the read/send/admin routes, and legacy
tokens cannot call `/integrations/*`. Submission always queues for the worker
rather than dispatching synchronously, sharing the existing send ledger,
capacity reservations (including the installation-wide 220 ceiling), and
shared-sender caps with direct send clients. A reply correlated to a sent
cold-outreach intent cancels every other still-queued intent for that same
contact before the worker's final send claim; ordinary conversational sends
are never touched. See [the integration contract](docs/INTEGRATION-CONTRACT.md) for
routes, schemas, the receipt state machine, and idempotency/conflict rules.
