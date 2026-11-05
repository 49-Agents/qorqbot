# Resend sending

Resend sending is transactional-email infrastructure end to end: the sending
domain, its DNS, provider verification, a domain-restricted key in the project
Settings and an authorized delivery. A key by itself is incomplete. A key present
with the domain `not_started`, DNS missing and no accessible Cloudflare token is a
failed outcome, not a finished key task and not a manual owner follow-up.

Prerequisites: the domain the owner named and a verified Cloudflare zone token for it
(`cloudflare_zone_token_ensure` with that `domain`). The confirmed email identity is needed only
for the sender (`RESEND_FROM_EMAIL`) and the delivery check; domain, DNS and
key work proceed before it. Administer Resend in your own browser
(`browser_turn`) in the existing logged-in account.

1. **Domain.** Reconcile existing domains and keys first. Find or add the exact
   confirmed sending domain; never substitute another domain or sender. Record
   the domain ID, name, region and sending capability on the Task.
2. **Required records.** Read the provider's current sending/authentication
   records for this exact domain: names, types, values, priorities and
   verification states. Table text can be visually truncated even when it
   looks complete: take each value from the provider's copy control or
   accessibility value, never the abbreviated text. Never hard-code a record
   count, an old table or another project's DKIM values; Resend may offer
   different DNS arrangements.
3. **DNS.** Install them with `cloudflare_dns_records_ensure` (the same `domain`,
   purpose `resend`). Preserve Fastmail's inbound root MX and any existing deliberate
   DMARC policy. Return-Path records belong at the provider-specified host;
   never replace the Fastmail inbox with Resend receiving, and keep receiving
   disabled unless separately requested. Avoid duplicate SPF at one name and
   conflicting TXT/CNAME data; use DNS-only where the provider requires it.
   Compare the persisted DNS content with the provider's full values, and note
   each record's Cloudflare `dns_record_id` on the Task.
4. **Verification.** Trigger verification after the records exist and record
   the observed state. Verification is asynchronous: a trigger or `pending` is
   not verified. Record on the Task when to recheck, then recheck on later turns
   until the domain is verified, sending is enabled and every required record
   passes. Keep each record's status and observed time as evidence.
5. **Key.** Create or reuse a separate project-labelled key with
   `sending_access` permission restricted to this exact domain. A key may
   already be deployed in Railway or an application environment while absent
   from canonical Settings, and the key details page never reveals an existing
   secret. Check the identified app environment and Railway service variables
   privately, match the value to the provider key's prefix/ID and domain
   permission, and save that same value with `environment_set` as
   `RESEND_API_KEY`; do not create a duplicate because the page or `hq/.env`
   lacks it. A newly created key is shown once: save it before closing the
   dialog. Save `RESEND_DOMAIN_ID`, `RESEND_DOMAIN` and `RESEND_FROM_EMAIL` as
   nonsecret project Settings. `RESEND_FROM_EMAIL` is the owner-confirmed
   business sender unless a purpose-specific sender was explicitly chosen. A
   full-access key, a key for another domain or another project's key does not
   satisfy this procedure. Record `settings_receipt` `{keys, shared_hash?,
   private_hash?}` from the `environment_set` results; its keys cover all four.
6. **Delivery.** Prove the saved binding with one transactional verification
   email to the confirmed business address or the owner's registered main mailbox.
   This is outgoing mail: before sending, follow Email Management's approval
   rule, reusing an existing saved approval that names this recipient and copy,
   or an explicit owner waiver recorded there. The provider request is neither.
   Read `RESEND_API_KEY` with `environment_get`
   privately and send one message through Resend with `RESEND_FROM_EMAIL`.
   Never echo, print or log the key. Record `delivery` with `recipient`,
   `message_id`, `status` and `observed_at`. A different recipient requires an
   actual owner answer naming that recipient; never re-ask an answer already
   given.
   API acceptance alone is not delivery: confirm the provider's delivered event
   or the received message. Inspect uncertain sends before retrying.
7. **Evidence.** Record on the Task: `domain_id`, `domain_name`, `region`,
   `domain_status`, `sending_enabled`, the records
   `[{type, name, status, dns_record_id, observed_at}]`, `observed_at`, the next
   recheck time while verification is pending, `key_id`, `key_name`,
   `key_permission`, `key_domain_id`, `from_email`, the separate sender when one
   exists, the `settings_receipt` and the `delivery`.

The work is verified only with domain/DNS readiness, the restricted key saved
and delivery evidence. An environment handoff separately proves an application
can consume it. An owner deferral is reported as deferred and unready, never as
working email.

The sending key sends mail; it does not administer domains. Provider
administration stays with you in the logged-in account. Never give an
application a full-access Resend key to compensate.

If a key was created but the save is uncertain, check the `environment_set`
receipt, the `environment_list` flag and the provider's key list before creating
another. A lost value becomes an explicit recovery step for the owner; do not rotate
or revoke keys blindly.
