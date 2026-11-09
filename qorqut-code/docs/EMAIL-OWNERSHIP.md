# Email ownership and transition (v1.3.2)

The core host has no email model. This page records what it does and does not own, where mail moved, and what a legacy
installation needs. It replaces the former `EMAIL*.md`, `DIRECT-EMAIL.md` and `FASTMAIL-ACCOUNT.md` pages, which described
the retired per-Project mailbox model (still readable in Git history).

## What core owns

- **No mail code, tables, configuration or worker.** A core-only composition has no `email_*` table, history trigger or view,
  no `business_email_*` / `email_*` operation, no mailbox provisioning, no email plugin tools and no Fastmail import. Nothing
  is shimmed: there is no empty `EmailOperations`, no always-empty read result and no old module kept under another name.
- **Email Management v3 is guidance only.** The `email-management` package (version 3.x, instruction-only, `owner_input`
  approval rule, no tools) is granted by default to the configured Ordubashy in ecosystem scope. Agent preparation validates it
  (`bootstrap/email_guidance.py`), renders its native discovery skill with the explicit `{{EMAIL_MANAGEMENT_HOME}}` path and,
  once, copies the package's `instance-template/` into `<operations>/extensions/email-management`. That directory is the
  owner-editable source of mailbox, provider and inbox guidance shared by every agent granted the package; re-preparation never
  overwrites it. Native connectors (for example Gmail) are preserved by preparation and are claimed usable only when exposed to
  the session. Agents pinned to an Email Management v2 package no longer prepare: re-grant them v3.
- **Generic Signoff is unchanged.** Internal changes and input requests work as before; there is no mail-specific Signoff
  adapter, approval operation or history. A stored platform-action Signoff keeps its recorded evidence and stays readable but
  cannot execute, retry or cancel.
- **Calendar keeps its API.** The `calendar_*` operations, the native screen, the permissions and validation, the provider-neutral
  `save_connection` and the common provider helpers remain in core; no provider adapter does. The host asks a `CalendarProvider`
  registered by an extension for each connection type (`google` from the optional `qorqut-google-calendar` package, whose OAuth
  helper is `python -m qorqut_google_calendar.connect`; `fastmail` from `qorqut-fastmail`), and reports `provider_unavailable`
  for one when its extension is not selected.

## Where mail went

The optional `qorqut-fastmail` package (`qorqut_fastmail.extension:create`, selected through the existing
`qorqut.extensions` mechanism) provides the `fastmail_*` account operations, the Fastmail CalDAV adapter and a narrow route
client for Paper. Its contract is in that repository's `docs/OPERATIONS.md`; configuration is `engine_url`, `admin_token_file`
and an optional `account_id`, with no business mailbox, folder or alias ownership. The separate email engine's account API
(`services/email-engine`) is what it talks to. A standard source export, preparation and installed-host check need neither;
`prepare-local.py --with-fastmail` adds both (the bridge to the host environment, the engine to its own). Paper loads without
the provider and keeps its sender routes only through the provider's explicit interface (see `qorqut-paper/README.md`).

## Retired from core

- Mandatory `business_email_*` provisioning, per-Project mailboxes, owned folders and aliases, and default sender inference
  from a Project.
- Mail threads, comments, controls, decisions, history and the selector maze of the old email plugin.
- Platform-dispatch of `email.send`, Ordubashy email approval (`signoffs_email_approve`) and incident reporting of failed
  account sends (a send's outcome is its job and engine receipt).
- The mail-specific agent-delegation machinery and the `ordubashy_mail_control` grant.

## Historical email data

Old records that point at mail stay readable as data and are never resolved against a mail service or Context record:

- **Email Tasks.** The Task `email` type and its `email_conversation_id` remain on existing Tasks. Ordinary Task writes (create,
  edit, type change) omit `email_conversation_id` entirely and need no mail lookup; an edit that keeps the binding does not
  look it up. Changing the type away from `email` clears the binding without the caller naming it. Naming a binding for another
  type is still a validation error, an Email Task cannot be created without a binding, and a binding that this host cannot
  resolve is refused cleanly, so no new linked Email Task can be made.
- **Links and note references.** Relationships and note references whose endpoint was an email conversation or message are
  neither fetched nor an error: the host no longer resolves those items, so relationship reads omit such an endpoint and note
  reads return the note. Showing the historical link is the display layer's job, from the metadata it already holds: a fresh core
  does not reconstruct imported legacy mail links, and the offline export (below) is not an instance migration.
- **Signoffs.** A stored platform-action Signoff keeps its recorded evidence and stays readable; it cannot execute, retry or cancel.
- **Progress.** `business_progress_get` and `global_progress_get` no longer return an `emails_sent` series or total (the count
  relied on the removed mail mirror, and a zero would imply tracked mail). No replacement metric is recorded.

## Deliberately retained names

These are historical or neutral vocabulary, not behavior: the `manage_fastmail` Global capability string (it gates the
provider's operations, and is granted explicitly) and the `email_message`
document kind with the context/email versioning exclusion.

## Legacy installations

Existing mail data is not lost, and nothing here migrates it:

- The retained `email_*` records are owned by the optional package as an offline archive schema, with a read-only export
  (`python -m qorqut_fastmail.export`, see `qorqut-fastmail/docs/LEGACY-MAIL.md`). It runs on a copy, never a live database.
- The core opens only a store whose recorded target identity and schema inventory equal its own composition and converts
  nothing, so it refuses a pre-v1.3.2 database. Quiesce and drain the instance, export the mail records from a checkpointed
  copy, and convert the remaining data into a store of the new target as a separate, deliberate step. Queued or approved legacy
  `email.*` jobs and Signoffs approved under the retired mail grant cannot run afterwards.
- No migration has been run against any real instance, and live-migration needs are for the operator to schedule.
