# Cloudflare access and DNS

Cloudflare work runs through native operations that call its API from qorqut.
Use the two below; they are the specified method, not a fallback for a
failed browser. Routine token and DNS work needs no browser control. Anything
else in Cloudflare goes through `cloudflare_api_request` (any API v4 method and
path with the installation's full-access token, which stays on the host).

The installation's central token issuer is bootstrapped once by the operator in
protected installation storage. Never read, copy or request it, and never
create another issuer. It never enters a project environment or application.
If the operation reports it unavailable, record that exact blocker on the
Task and raise it to the owner once; it is an installation dependency,
not a per-project credential-copy task.

## Token and zone: `cloudflare_zone_token_ensure`

Input: a required `domain`, the exact Cloudflare zone name the owner named for this
work, as a canonical lower-case DNS hostname such as `example.com`: no scheme,
port, path, wildcard or underscore label. A missing or malformed `domain` is a
`validation_error`; correct the argument and never guess a domain. Optional input:
`zone_id`, `account_id`, `replace_saved_token` and `rationale`. A recorded purchase,
an email-name answer or any other setup evidence is not required, and none is
written.

1. It discovers the zone and account matching exactly the supplied `domain`,
   only through the installation's central zone-read binding or its central
   verified zone inventory. A `zone_id` and `account_id` you pass are assertions
   that must match that discovery; they never replace it.
2. No token is created before the zone and account are verified. It then
   reconciles the saved project key and the issuer's token inventory, and
   verifies the existing project token or creates one.
3. The token's only resource is `com.cloudflare.api.account.zone.<zone ID>`,
   with every zone-scoped permission group.
4. It persists `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID` and
   `CLOUDFLARE_ACCOUNT_ID` host-side into the registered project Settings.
5. It returns verified nonsecret evidence: IDs, the actual permission list and a
   settings receipt (key names and file hash), never the secret; record them on
   the Task.

`zone_discovery_unavailable` means the installation has neither a central
zone-read binding nor a central verified inventory for this domain. It is an
installation dependency: record it on the Task and report it to the owner
once. It is not a per-project chore; never ask the owner for a zone ID for each
project, and never pass a guessed one.

Running it again is the resume path: it verifies instead of duplicating. Check
the returned zone against the intended domain and account before continuing.
A token name is not scope evidence; the resource and permission list are. Never
accept an all-zones, account-wide or user-token-administration substitute. If
an account-level permission becomes necessary, report the exact unavailable
scope instead of widening the token.

Nothing is deleted or revoked automatically:

- `verification_failed`: a token was created and saved but did not verify as an
  active exact-zone token. The saved secret is kept and the token ID is in the
  error details. Record it on the Task and ask the owner; do not create another.
- `recovery_required` has two meanings. A project token exists but its saved
  secret is missing, unusable or was never returned: record the explicit
  recovery step on the task and ask the owner; do not create, rotate or revoke
  anything. Or Settings already hold another `CLOUDFLARE_API_TOKEN`: rerun with
  `replace_saved_token: true` only once replacing that saved token is
  established as intended; if its purpose is unclear, ask the owner.

## DNS records: `cloudflare_dns_records_ensure`

Input: the required `domain` (the same exact zone name), `purpose` (`fastmail` or
`resend`) and `records` `[{type, name, content, priority?, ttl?}]`, with types MX,
TXT or CNAME only. It uses this project's own saved zone token, zone and account
keys (written by `cloudflare_zone_token_ensure`) and takes no zone or account ID.
Before any write it verifies that the token is active and queries the saved zone
with it; that zone's exact name must equal `domain` and its account must equal the
saved account. Every record must sit at `domain` or at a true subdomain of it. A
zone is never inferred from record text.

- `configuration_required`: the saved token, zone or account key is missing. Run
  `cloudflare_zone_token_ensure` for this project and `domain` first.
- `zone_mismatch`: the saved zone or account is not this `domain`'s. Stop and
  record the difference on the Task; do not retry with other values, and ask the owner
  whether the domain or the saved zone is wrong.
- `verification_failed`: the saved token is not an active exact-zone token. Record
  it on the Task and ask the owner.
- `validation_error`: `domain` is missing or malformed, or a record is outside it.
- A provider error keeps its provider code: report it as given.

- Copy every record exactly from the provider's current instructions for this
  domain (names, types, values, priorities). Take full values from the
  provider's copy control or accessibility value; table text can be visually
  truncated, and content containing an ellipsis marker is rejected. Never
  hard-code values, reuse an old table or copy another project's records.
- Identical existing records are reused and missing ones are created. Existing
  records are never modified or deleted, so unrelated DNS is preserved. Record
  each result's Cloudflare record ID (`dns_record_id`) on the Task.
- A `conflict` result (duplicate SPF at the same name, existing DMARC, a
  different DKIM TXT at the same `_domainkey` name, a CNAME collision, or a
  different MX at the same name) is not a failure to work around. Keep the
  existing deliberate DMARC policy and Fastmail's root MX. For any other
  conflict, record the existing and requested record on the Task and ask the owner
  how to resolve it; never delete or overwrite records another way.
- After a timeout or uncertain result, rerun the same call with the same
  `idempotency_key`; it reconciles instead of duplicating.

Propagation and provider verification are then rechecked on later turns (see
"Waiting on DNS and verification" in service-provisioning).
