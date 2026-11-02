# Fastmail

Provider guide for Email Management. It supplements `instructions.md`, `providers/README.md` and the
account's `inboxes/` file; it never replaces the owner's recorded sender choice or approval setting,
and it grants no access.

## How Fastmail is reached

Fastmail is reached with the optional Fastmail tools (`fastmail_*`) or the managed browser, as the
owner chose in the account's `inboxes/` file. The tools exist only if the optional Fastmail package
is installed and selected for this installation, your grants allow Global Fastmail operations, and
the tools are visible in this session. Do not promise them otherwise. If a call is refused for
authority, report that to the owner; do not work around it. If the tools are not available, use the
managed browser only when the owner's saved route in `inboxes/` allows that fallback; otherwise
report it and ask.

One Fastmail account can host many aliases, and an alias is an identity of that account. Which alias
suits which Project is the owner's recorded guidance in `inboxes/`: use it, and never invent an
association. No Project, folder or business implies a sender. Every `fastmail_*` call names the
account (`account_id`, as reported by `fastmail_capabilities_get`).

## Reading and organizing

- Reads: `fastmail_capabilities_get`, `fastmail_identities_list`, `fastmail_folders_list`,
  `fastmail_messages_search`, `fastmail_message_get`, `fastmail_thread_get` and
  `fastmail_attachment_get` (follow `next_offset` until it equals `total`). Results come in pages;
  ask for the next page rather than assuming the first is everything.
- Updates and deletes carry the `expected_state` read from the engine and fail if the object has
  changed since. Read the object again before trying again; do not guess a state.
- Organizing changes (message updates and deletes, folders, drafts, vacation reply, masked email)
  queue one background job each and return its `job_id`; the outcome is read with
  `fastmail_receipt_get`. Saving a draft sends nothing.
- Do not delete or rename addresses, folders or messages, or move existing mail, unless that is
  part of the request.

## Sending with the native tools

- Name the account and the sender identity explicitly: `identity_id` and the matching
  `from_address`, which must be the identity's own address. Use a real provider identity for From. If
  the address and identity do not match, or the identity has an automatic Bcc or Reply-To, the call is
  rejected before anything is queued; ask the owner instead of picking another sender.
- Call `fastmail_send`, `fastmail_reply` or `fastmail_forward` with the exact sender, recipients,
  subject, body and attachments. The result is `signoff_required` with the pending Signoff; nothing is
  queued or sent yet. The owner reviews a frozen preview (From, To, Cc, Bcc, subject, body and
  attachment details; a forward also shows the original message).
- The owner can edit only the subject and body on the Signoff. Save creates a revised proposal and
  does not approve it. Approve queues exactly that proposal for sending. A rejected or superseded
  proposal cannot send. Recipients, sender and attachments stay frozen; changing one needs a new
  proposal. A forward whose source was truncated cannot be proposed.
- Native Fastmail sending always requires this Signoff; a prose waiver, an `allow` override or an
  earlier "send without asking" cannot bypass it. Never substitute a generic `signoffs_input_create`
  question, and never send again after approval. For the managed browser, follow `instructions.md`.
- Limits: at most 50 recipients per field, 10 attachments, 1 MiB of inline attachment data (base64)
  in total, and a body of at most 50,000 characters.

## Delivery and receipts

- The Signoff shows the send's progress: queued, sending, reconciling, submitted, failed or
  unconfirmed. **Submitted means Fastmail accepted the message, not that the recipient received it.**
- Read the outcome on the Signoff, or with `fastmail_receipt_get` and the request id (the queued job
  id). It returns the engine's receipt, if any, and the job's own state, attempts and `error_code`;
  `not_found` means neither exists yet.
- The engine records its intent before a single dispatch, and a timeout, restart or lost response never
  dispatches the action again. A `prepared` or `uncertain` receipt keeps the job pending, and the
  receipt is read again every 15 seconds. After eight attempts the job ends as `external_unconfirmed`: it is
  never guessed as sent or failed. Inspect the receipt and the Sent folder, and tell the owner what
  you observed. `provider_unavailable` with "nothing was sent" means the engine was not configured
  before anything was dispatched.
- After a timeout or unclear result, look up the receipt or Sent copy. Never resend with a new
  request, and never call the message delivered until you see it. A legacy send job without exact
  owner approval must not be retried as a new send; review its receipt first.

## Setting up an address or alias (only when the owner asks)

1. Agree the address first. Propose `<name>@<domain>` and wait for the owner's answer, unless they
   already named it.
2. **Domain and DNS.** Add or reuse the domain in the existing account. Read the exact records
   Fastmail currently requires for it, taking full values from its copy control. Add them at the
   domain's DNS provider if the owner has given you access to it, or give the owner the exact records
   to add; no particular DNS provider or helper is assumed. Keep unrelated records, avoid a duplicate
   SPF record and keep any deliberate DMARC policy. Note when verification succeeds; if it is
   pending, recheck on a later turn rather than waiting in the browser.
3. **Alias and identity.** Create or reuse the alias and its sender identity in the existing account;
   check existing aliases and identities first. A separate provider login or user seat is a different
   request: do not look into seats for an alias request, and a limit on users is no evidence that an
   alias cannot be added.
4. **Folder and routing (optional).** Only if the owner wants the address's mail kept apart, create a
   folder and set the alias's incoming rule and the identity's sent-mail folder. A new alias does not
   copy an older alias's folders, so set and verify both.
5. **Overrides.** Inspect automatic BCC and Reply-To on the alias and identity. The native tools
   refuse to send as an identity that has either.
6. **Verify.** With the owner's approval, send one test message to a recipient they name and confirm
   it arrived and sits in Sent. Creating an alias is not evidence of delivery.
7. Record the address and what it is for in `inboxes/`, with a Project association only if the owner
   gave one.

The owner does their own login, verification and consent in the browser; if one blocks you, ask
through owner-input. Never type or record a password or code.
