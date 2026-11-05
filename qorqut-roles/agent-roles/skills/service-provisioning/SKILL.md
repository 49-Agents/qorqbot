---
name: bazarlyk-role-service-provisioning
description: Provision or repair provider services for a project only when the owner or a Task the owner authorized explicitly asks for them - a Cloudflare zone token and DNS, a Railway project, Resend sending or an OpenRouter key - using the native Cloudflare operations and your own browser, and saving secrets only through environment_set. Creating a project never starts any of it.
---

# Provider services

Use this procedure only when the owner, or a Task the owner authorized, explicitly asks for provider
work for a project: a Cloudflare zone token or DNS records, a Railway project, Resend
sending or an OpenRouter key. Creating a project provisions none of it and there is no
checklist that implies it. A mail address or alias follows Email Management's Fastmail guide. There are no owner-question forms or readiness protocol
here: confirm the project, domain and purpose with the owner when they are unclear (owner-input
for an asynchronous question), keep the work as ordinary Tasks in the project, and record
progress on the Task (key names, provider IDs, receipts and observed times, never
values).

## Resume first

1. Select the project and read its Task, comments and current state. `environment_list`
   shows the Settings key names with `configured` flags only.
2. Resume from Task IDs, provider IDs, evidence, settings receipts and the current
   provider API or UI state. Never repeat a creation merely because a turn ended, a
   session restarted or a result was not read: find the existing resource first and
   reconcile.
3. Read the project's context (the core through `context_search` and `context_get`, plus
   the shared graph through `context_find` and `context_object` when relevant) and reuse
   answers the owner already gave; never ask the same question again.

## Execution methods and authority

- Cloudflare: its API from qorqut through `cloudflare_zone_token_ensure` and
  `cloudflare_dns_records_ensure`, each with the required exact zone `domain` the owner
  named ([Cloudflare](references/cloudflare.md)); Ordubashy's existing policy and
  active identity gate them, and a project's creation never calls them. This is the
  specified method, not a browser fallback. Do not extend API automation to Fastmail,
  Railway, Resend or OpenRouter.
- Fastmail, Railway, Resend and OpenRouter administration: your own configured browser
  through `browser_turn` (lease-free), reusing the logged-in control accounts
  ([Railway](references/railway.md), [Resend](references/resend.md),
  [OpenRouter](references/openrouter.md)). Get a handle with `acquire`
  (`protocol: "handles"`; `consumer_id` is optional, an unverified caller-chosen UUID that
  proves no session identity, so do not look up a session ID for it); omit `business_id`
  and `browser_key` (project context is not a browser selector). Operate the current
  observed controls and follow browser-control for commands and timeouts. Never use the
  client's browser or launch a replacement Chrome.
- Qorqut records and settings: existing MCP operations
  (`environment_set`, Tasks and Context).
- `environment_get` in this project only for two reads: `RESEND_API_KEY` in the sending
  context of an authorized Resend verification send, and the values you inject into a
  registered application target during an environment handoff. Never echo, log or copy a
  value into Tasks, evidence, notes, messages or Context.
- An explicit request is the owner-adopted procedure for exactly its provider actions: do
  not ask again for consent to perform them. It does not authorize unrelated account
  work, purchases, deletions or renames; preserve existing resources. Real provider login,
  owner-only decisions and release approvals remain. A verification email is outgoing mail: it
  follows Email Management's approval rule, which the provider request does not waive.
- Run each browser step and its directly associated Task, settings and context
  bookkeeping yourself as one coherent operation. Do not create a worker per click.
  Separately delegated non-browser work keeps the durable-assignment rules and gains no
  software/design build authority.
- Login, 2FA or a lost provider session: create the urgent blocking owner-input Signoff
  described in ecosystem-review and note the blocker on the Task.

## One-time secrets and interrupted creation

`cloudflare_zone_token_ensure` persists its token host-side; you never see it. Railway,
Resend and OpenRouter show a new secret once. Before closing the dialog or navigating
away, save it with `environment_set` (key, value, stable `idempotency_key`), then record
the key name, the provider identifier and the write receipt on the Task. Never put a
secret in Task text, notes, evidence, Context, messages, source control or shared
screenshots. Tool arguments and private browser artifacts may still contain values; do
not promise otherwise.

If creation succeeded but saving is uncertain, check the `environment_list` flags, the
`environment_set` receipt and the provider's current list before any new creation. If the
value is lost and cannot be recovered, record an explicit recovery step on the Task and
ask the owner; never create, rotate or revoke credentials blindly, and nothing is revoked
automatically. Describe browser observations and configured flags exactly.

## Waiting on DNS and verification

After records exist, trigger the provider check and record the observed status. While it
is pending, record on the Task what to recheck and when, and move on; never wait in the
browser while DNS propagates. On a later turn recheck, then record the new status and a
later recheck time or clear it. A trigger, `pending`, `not_started` or `failed` is not
verified. Propagation is your dependency, never a task for the owner.

## Environment and application handoff

When an application needs the saved keys, the canonical environment is the registered
project Settings target (its `hq_path` `.env`) written through `environment_set`. Never
infer it from a slug or assume an application `.env` such as `code/landing_page/.env` is
populated.

1. Confirm every provider key is `configured` in `environment_list` and the sender and
   domain metadata are present. Expose only safe metadata: the config binding, key names,
   sender and domain IDs, verification status and time, and the access method authorized
   for the consuming agent. Never values.
2. The consumer is an active registered global agent authorized for the project whose own
   role view of Settings contains every provider key and whose policy allows
   `environment_get` (Ordubashy qualifies; several may). Projects have no resident
   agents. Check with `agents_get`, `permissions_get` and `environment_list`. A missing
   grant is an authority gap to record on the Task; never widen grants yourself.
3. Where an application repository or Railway service already exists, register its actual
   configuration target and inject the required values through the approved configuration
   path, preserving unrelated entries: read each value with `environment_get` in this
   project and write it straight to the target. Secrets live only in ignored, untracked,
   protected remote files or the authorized provider settings. Verify presence without
   echoing values and record each target (`railway_service` or `app_env`, its reference,
   the sync time and the consumer variable names) on the Task.
4. Record each target's exact consumer variable names. Names are not interchangeable
   without an explicit mapping: the inspected apps read `RESEND_FROM`, which maps to
   `RESEND_FROM_EMAIL`. Preserve an authorized purpose-specific login sender and keep it
   distinct from your default sender.
5. A `railway_service` target needs a verification that the deployed service already has
   the matched value, because a local `.env` is not proof Railway received it. Never
   redeploy automatically to synchronize settings.
6. Where no application exists, record the canonical source and the loader or injection
   contract. Do not invent a repository or deploy a placeholder service; the normal
   application bootstrap consumes the contract.

Applications receive the Resend sending key and sender/domain settings. Agents that need
DNS use the exact-zone Cloudflare token through their project environment; the central
issuer credential never leaves installation storage. Application integration is
development: the owner's unless the owner directly instructs a Global (business-operations).

## Report

Report verified, waiting, not-applicable and deferred work separately, and never show
email as ready while a mailbox, sender or environment handoff is deferred or unverified.
