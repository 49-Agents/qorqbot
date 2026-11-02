# Private Email Archive

This is an encrypted archive of observed email communications for independent
research and data science. It does not require a Qorqut business, outreach campaign,
or training pipeline. Collection is opt-in through `EMAIL_HISTORY_FILE`; see
[configuration](CONFIGURATION.md#optional-features) for encryption keys,
the dedicated service principal, backups and initial state selection. The service
has no archive read, search, export or decryption API.

## Coverage And Start State

Use `capture_scope: "account"` for every changed message visible in the configured
Fastmail JMAP account. This includes incoming, sent, draft, spam, trash and personal
mail in any folder, regardless of business registration. `scoped` is the backward
compatible default and restricts message collection to registered business and
explicit private folders. Archive coverage never changes business API grants.

Each collection starts from the durable Email state originally selected in
`start_cursor`. It uses `Email/changes`, with no initial `Email/query` or historical
body import. A `provider.capture_configuration` event records the capture scope
and configured folder IDs with the cursor in use when that configuration was first
observed. Scope changes retain the same cursor and record a new configuration
event. Earlier untouched messages are not implicitly imported. Account, original
start state and public encryption key remain pinned to the archive.

This covers data available to the configured provider account. Connecting an
external sending identity does not itself make that provider's inbox or historical
Sent folder available. Any forwarding/import route has its own provenance and
coverage limitations; archived MIME is exactly the copy exposed by Fastmail.

## Independent Event Format

SQLite `events` contains `event_id`, a SHA-256 digest of the plaintext JSON event,
and a JSON encryption envelope. Existing events are immutable. The envelope uses
`version: 1`, `algorithm: RSA-OAEP-SHA256+A256GCM`, and fields `key_id`, `wrapped_key`,
`nonce`, and `ciphertext`; binary values use base64. Each event gets a fresh AES
key wrapped by the configured RSA public key. Authenticated associated data is
the UTF-8 string `key_id:event_id:digest`. Only a separately authorized principal
with the private key can recover the JSON object `{event_id, kind, content}`.

Fastmail provider event content includes `schema_version: 1`, `provider: fastmail-jmap`,
`account_id` and UTC `observed_at`. Earlier events remain valid in their original
format. No external application database is needed to interpret these records.
Auxiliary SQLite tables retain provider binding, collection cursor and last-observed
digests for restart/replay; they are not a second plaintext message store.

- `provider.message`: `message` retains JMAP message, thread and MIME blob IDs;
  full observed keywords and mailbox memberships; received/sent timestamps; size;
  all headers; parsed address and threading fields; body structure and body-part
  blob IDs, headers and attachment metadata. The provider-returned metadata is
  preserved without business normalization. `mime_base64` contains the original
  downloaded MIME bytes, including encoded attachments and inline parts;
  `mime_sha256` and `mime_size` allow independent byte verification. Parsed body
  values can contain provider truncation/encoding flags; raw MIME is retained in
  full within the response limit.
- `provider.mailbox_catalog`: `catalog` preserves the complete current
  `Mailbox/get` response, including state and folder IDs, names, hierarchy, roles,
  rights and counts exposed by the provider. A new encrypted snapshot records
  observed changes, including renames and folders disappearing from the catalog.
- `provider.capture_configuration`: records scope and configured folder IDs.
- `provider.collection_gap`: records the saved cursor, bounded error code, provider
  status, capture scope and, when known, a missing message ID. Repeated identical
  failures at the same cursor preserve the first event and observation time.

Message `provenance` includes created/updated/destroyed classification, the requested
Email state, returned changes state, message-fetch state and folder-catalog state
when available. Catalog/configuration provenance identifies the collection cursor.
Unchanged snapshots are deduplicated across retries; an observed A → B → A change
retains all three versions. Observation markers and their encrypted events commit
atomically. The collection cursor advances only after the page's records persist.

Metadata properties follow [JMAP Mail (RFC 8621)](https://www.rfc-editor.org/rfc/rfc8621.html#section-4.2).
Service credentials, provider session responses, configuration files, and HTTP
authorization headers are never collected. Email content itself is preserved
without redaction; a secret written in an email remains part of that email.

## Independent Outlook Source

The optional Outlook Web collector appends to the same encrypted event store under
`provider: outlook-web`, `account_address`, and an explicitly bound UTC `start_at`.
It does not change the existing Fastmail account binding, JMAP starting state,
collection cursor, or earlier encrypted events. `external_history_bindings` pins
each external provider/account cutover; `external_history_observations` stores
only the last digest for the namespace `(provider, account_address, kind, identity)`.
Identical retry snapshots deduplicate; an observed A → B → A retains every change.
New external event IDs use `external-provider:<UUID>`; stable `external-gap:` IDs
deduplicate the same source/cursor/error gap. The encryption envelope is unchanged.

External content includes `schema_version: 1`, provider/account identity, UTC
`observed_at`, `capture_scope: account`, and provenance. `provider.message` uses
`message`; other external event kinds use `snapshot`. Outlook message snapshots
retain native ID and metadata in `raw`, one complete `mime_base64`, MIME hash/size,
dates, read state, sent evidence, and the native change under `raw.qorqut_change`.
`raw.MimeContent.Value` is represented once by `mime_base64`; its CharacterSet and
other native metadata remain in `raw.MimeContent`, with an explicit provenance
reference to the MIME field. Conversation IDs, categories, flags, folder IDs,
change keys and absent native fields are preserved as observed.

`provider.quota_observation` preserves the complete recent-Sent metadata result
used before reserving a send, including its account and rolling-window cutoff.
This includes recent sends before the body-capture cutover without fetching their
MIME. Native IDs and Internet Message-IDs deduplicate known engine sends; this
observation does not expand historical body capture.

Draft and sent observations can legitimately differ. The controlled Outlook Web
send retained the Internet Message-ID but removed `X-Qorqut-Request` and resolved
display names through the directory. Retain both original observations and actual
sent MIME; never manufacture a stripped header in the archive. Sent reconciliation
may accept a missing request header only with native Sent Items evidence and the
saved payload's exact subject, body, sender/recipient addresses, Reply-To and
attachment hashes. This exception does not weaken draft verification. Native sent
evidence alone does not prove a completed Fastmail copy, final receipt or delivery.

The folder catalog includes all returned folder classes and their native metadata.
Message capture traverses mail folders (`IPF.Note` and subclasses), excluding
virtual SearchFolders. Initial enumeration fetches no historical message bodies
before the explicit cutover. Established native states capture later observed
updates/read flags/deletions, independently of unread status. A completed folder
does not imply the entire account has caught up. Durable cursors retain per-folder
states, baseline progress, and unprocessed native changes without storing MIME.
Collection permits 24 calls, ten MIME fetches, 500 emitted items, 25 MiB per message,
and 50 MiB aggregate MIME per page; a 15-second budget stops further sync/body
calls after preserving a completed slow call. Full catalog pagination precedes
those calls. Native protocol and process timeouts remain separate bounds.

Private capture commits before any business routing. Exact RFC Message-ID,
In-Reply-To and References must resolve to one confirmed business send and an
unchanged registered mailbox binding before original MIME can enter that business's
Fastmail folder. Pending originals delay exposure. Ambiguous/unowned messages stay
private; `provider.routing_issue` records the bounded reason. A durable routing
queue can retry already observed native IDs after capture has advanced. The
service holds only the public encryption key, so retries retrieve original MIME
from Outlook rather than decrypting the archive. Missing retry sources remain
explicit issues. Imports reconcile existing exact copies and uncertain attempts;
they never authorize a new send or move another business's mail.

A received Outlook copy of a confirmed Fastmail send may contain provider-added
content. Only an exact confirmed send-ledger Message-ID can correlate that copy
with the saved Fastmail original. The collector verifies the original's scoped
message/thread IDs and approved recipients, body, reply headers and attachments.
It maps that existing original without importing the received variant. Full native
MIME remains private; `provider.message_correlation` and the mapping's
`content_variant` flag record both content digests and the approved request.
Other incoming copies still require the existing strict MIME comparison.

`provider.business_copy` records native-to-Fastmail identity mappings. Operational
send receipts use the registered Fastmail copy's message/conversation IDs and keep
native Outlook evidence separately. `provider.capture_configuration`,
`provider.mailbox_catalog`, `provider.native_gap`, and `provider.collection_gap`
retain source setup, catalogs and failures without exposing them through business
mail tools. Fastmail and Outlook collection run independently; one source's
failure cannot reset the other's progress.

## Missing Data And Recovery

An observed deletion adds a `message` tombstone with `id` and `removed: true`;
prior versions remain intact. Account mode also retains deletion IDs whose bodies
were never observed. If a created/updated ID is already reported `notFound` at the
metadata lookup, it gets `unavailable: true`, `reason: not_found_after_change` and
a gap event. This distinguishes unavailable content from a confirmed deletion.
Scoped messages moved outside capture folders retain an
`outside_capture_folders: true` observation.

Download failures, messages disappearing during body retrieval, malformed results,
expired changes cursors and Fastmail provider responses over 50 MiB pause collection at the
saved cursor and record a gap where storage remains available. They never trigger
a silent baseline reset. No partial MIME is saved as a complete message.

Polling cannot recover versions or bodies deleted before observation. Catalogs
show observed folder states, not every intervening rename. An older message changed
after cutover can be captured, while an untouched historical message is not.
Neither receipt capture nor this archive proves delivery to every recipient.
These boundaries must remain visible in any later authorized export or analysis.

Back up both the operational send-ledger database and this separate archive using
SQLite's backup API, and preserve the recovery key separately. Never discard a
send ledger, alter the starting cursor, or clear archive digests to retry a send.

For Outlook recovery, preserve the operational `outlook_collection_*` tables,
`outlook_web_sends`, send ledger/quota observations, and the archive's
`external_history_*` bindings/digests together. These contain source identity,
opaque sync/pending work, import receipts and routing state, not a replacement
plaintext mail archive. Losing operational state while the archive's source
binding exists is a recovery error; startup cannot silently create a new baseline.
Changing account, account ref or cutover also fails. A native cursor expiration,
unavailable MIME or unsupported provider shape retains the last durable cursor
and an explicit gap; do not auto-reset it. Restore compatible SQLite backups and
reconcile missing intervals through separately authorized maintenance. Do not
clear pending routes, import receipts, or uncertain sends to force progress.

For an already observed routing conflict, an operator can request
`collector.retry_routing(native_id, reason="...")`, then run collection normally.
This appends `provider.routing_retry_requested`, retains prior issues/import
receipts and the native capture cursor, and rechecks current ledger ownership and
provider content. It cannot add an unobserved ID or authorize an uncertain import.

Outlook's web protocol is private and may change. Native change feeds can coalesce
versions, bodies may vanish before capture, and offset-based sent/draft lookup
does not provide snapshot isolation during equal-count folder mutations. A
`synced` capture state describes a completed observed folder cycle; it does not
prove all business imports finished, that no gaps ever occurred, or recipient
delivery. Exports must retain these distinctions and both providers' provenance.
