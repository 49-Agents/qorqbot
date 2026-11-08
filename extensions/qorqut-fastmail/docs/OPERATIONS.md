# Fastmail operation and configuration contract

## Configuration

Selected through the existing `bazarlyk.extensions` mechanism in the trusted installation configuration; nothing else enables it.

| key | required | meaning |
| --- | --- | --- |
| `engine_url` | yes | Origin of the email engine: `http`/`https` with an explicit loopback or Tailscale (`100.64.0.0/10`) IP, no credentials, path, query or fragment |
| `admin_token_file` | yes | Absolute path of a private (no group/world bits), non-symlink file holding the engine's account administration token (at least 32 ASCII characters) |
| `account_id` | no | When set, the engine must report exactly this Fastmail account or every operation is refused (`account_mismatch`). Each account-specific read and receipt lookup first checks the engine's capability view, so no payload of another account is returned |

There are no send/read tokens, no business mailbox file and no folder, alias or Project mapping. The token file is read when the
engine is first used and never when the declaration is made, so a missing token yields "unavailable" at use, not a startup error.

## Access

Every operation is Global-scope and needs the `manage_fastmail` Global grant. Beyond the grant the caller must be the installation
owner, the configured ecosystem Ordubashy, or an authorized global worker; a generic ecosystem agent holding the grant is refused.
Business-scoped callers are refused. Sending (`fastmail_send`, `fastmail_reply`, `fastmail_forward`) requires an authorized
agent proposal and exact owner Signoff; an `allow` override cannot skip review, while `deny` still refuses it. The owner
reviews through the existing Signoff route rather than invoking a direct send. Other mutations keep their `allow` default
and do not support Signoff. Reading, organizing mail and saving provider drafts retain their existing behavior.

## Reads (`effect_kind: read`)

| operation | arguments | result |
| --- | --- | --- |
| `fastmail_capabilities_get` | none | the configured account: `account_id`, supported `actions`, folders and identities |
| `fastmail_identities_list` | none | sending identities (id, name, email); an alias is an identity |
| `fastmail_folders_list` | none | folders with ids and state |
| `fastmail_messages_search` | `q`, `from`, `to`, `subject`, `in_mailbox`, `has_keyword`, `before`, `after`, `limit` (≤100), `position` | one page of messages |
| `fastmail_message_get` | `message_id` | one message with its `expected_state` |
| `fastmail_thread_get` | `thread_id`, `limit` (≤100), `position` | one page of a thread |
| `fastmail_attachment_get` | `attachment_id`, `message_id`, `offset`, `limit` (≤5 MiB) | `content_base64`, `total`, `offset`, `next_offset`; follow until `next_offset == total` |
| `fastmail_receipt_get` | `request_id` | `{request_id, receipt, job}`: the engine's receipt for a mutation (the request id is the queued job id), if it holds one, and the job's own `state`, `attempts`, `operation`, `error_code` and timestamps (never its arguments); `not_found` only when neither exists. This is how a queued, retrying or `external_unconfirmed` mutation is observed under the `manage_fastmail` grant alone |

Results larger than 12 MiB are refused (`external_result_invalid`).

## Mutations (`effect_kind: compound`, one durable job each)

`fastmail_message_update`, `fastmail_message_delete`, `fastmail_folder_create`, `fastmail_folder_update`,
`fastmail_folder_delete`, `fastmail_draft_create`, `fastmail_draft_update`, `fastmail_send`, `fastmail_reply`,
`fastmail_forward`, `fastmail_vacation_set`, `fastmail_masked_email_set`.

Every mutation names `account_id`. Updates and deletes carry the `expected_state` read from the engine and fail if the object
changed. Composing operations (`draft_create`, `draft_update`, `send`, `reply`, `forward`) also name `identity_id` and
`from_address`:

- `identity_id` must be an identity of the account and its email must equal `from_address` (case-insensitive); otherwise
  `sender_identity_denied` / `sender_identity_mismatch` before anything is queued. An identity with an automatic Bcc or Reply-To
  is refused, as the engine would refuse it.
- No sender is ever inferred from a Project, folder or business. The engine re-verifies the identity when it applies the action.
- Limits: ≤50 recipients per field, ≤10 attachments, ≤1 MiB base64 of inline attachment data in total, body ≤50 000 characters.

Sending returns `signoff_required` with the pending Signoff identity. It creates no job and sends nothing yet. The frozen
preview includes From/To/Cc/Bcc, subject, body and attachment metadata; forward previews also include the full original
message and attachment names. Truncated forward sources cannot be proposed.

The shared Signoff amendment endpoint permits only subject/body edits and creates a new immutable proposal. Save does not
approve. Approve queues the reviewed job; its id is the engine request id. Rejected/superseded proposals cannot send. Current
account/identity and forward source content are checked again, and the sender requires the exact owner-approved plan. Other
mutations continue returning `{"job_id": ...}` immediately.

The same Signoff UI used for WhatsApp displays/edit emails. Its local outcome projection distinguishes queued, sending,
reconciling, submitted, failed and unconfirmed, without provider calls during card reads. Submitted means provider acceptance,
reported to the owner as sent; the outcome has no recipient-delivery field. Existing generic input questions remain questions; they are not parsed into executable mail.

Deploy the matching host, extension, dashboard and Email Management guidance together. This change adds no tables or schema
migration. Preserve receipts and any in-flight jobs: legacy jobs without exact owner proof must not be retried as new sends.
Review their existing receipts before creating a new proposal. Update the authored Fastmail approval guidance, preserving
owner-specific accounts/preferences; do not overwrite whole local instruction files. The packaged reference text is
`qorqut_fastmail/resources/email-management/providers/fastmail.md` (see the README); its installation is still pending.

## Delivery guarantee

The job records its dispatch intent durably before the single POST to the engine, and afterwards only reads the engine's receipt.
A timeout, restart or lost response never posts the action again; a receipt in `prepared` or `uncertain` keeps the job pending
(retry every 15 s). After 8 attempts the job ends as `external_unconfirmed` for the owner to inspect with `fastmail_receipt_get`;
it is never guessed as sent or failed. If the engine is not configured before anything was dispatched, the job fails as
`provider_unavailable` ("nothing was sent"). Authority is rechecked when the job runs.

## Error codes

`provider_unavailable` (engine not configured on this host; HTTP 503), `operation_unavailable` (engine unreachable for a read,
token unusable, or the account does not advertise the action), `account_mismatch` (409), `request_conflict` (409),
`sender_identity_denied` (403), `sender_identity_mismatch`, `validation_error`, `external_auth_failed`,
`external_result_invalid`, `external_failed`, `external_unconfirmed`.

## Calendar and Paper seams

`FastmailCalendar` serves the `fastmail` CalDAV connection type for the host's `calendar_*` operations through the
`calendar_providers` hook; the core keeps the operations, the Google adapter and the primary-calendar semantics, and reports
`provider_unavailable` for a `fastmail` connection when this extension is not selected. Paper's managed sender route uses
`route_client()` only when both packages are selected, with explicit configuration.
