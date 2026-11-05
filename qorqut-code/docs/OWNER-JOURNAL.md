# Owner journal

The owner journal is edited in Global Notes in the dashboard. Existing Global
and business entries retain their document IDs, backing files, assigned dates,
body, checklist state and revision history. The interface combines them; it
does not move or rewrite their source files. New entries are Global and named
**Daily**.

## Ordubashy access

Ordubashy can read entries only when the owner explicitly asks in conversation.
The only operations are `journal_entries_list` and `journal_entry_get`. They are
direct reads: there is no review marker, polling, reminder, reconciliation,
automatic context filing or follow-up. Listing returns visible entry metadata;
opening one returns its current full content and checklist state. Reads do not
change the entry or record that it was reviewed. An owner request to read does
not by itself authorize any other action described in journal text.

Each Global journal entry has an Ordubashy visibility switch, ON by default.
Turning it off removes the entry from the direct-read list and makes direct reads
fail as not found. The dashboard checks the current setting on every read. This
switch governs these direct reads only; it does not remove a saved filer upload
or facts already stored in context.

Generic document and context readers do not provide a way around this boundary.
Only the configured Ordubashy identity can use the two direct-read operations.
Other principals remain denied even if a role grant is added. The dashboard
owner retains ordinary edit, archive and visibility controls with revision
conflict checks.

## Explicit filer upload

The owner can independently choose **Upload to filer** in Global Notes. The
dashboard saves edits first, then submits the exact current revision and content
hash to the existing Global context filer. This creates an immutable Global
snapshot containing the journal text, checklist state, assigned date and source
document/revision/hash. The original journal file and document revision are not
changed by filing. Visibility may be ON or OFF; it does not gate an owner upload.

Filing is an explicit owner action. It creates no task, Focus item, plan,
Signoff, or inferred follow-up. A queued response is not a completed filing;
the dashboard shows the submission state and filer receipt. If the request
outcome is uncertain, the dashboard retains the same idempotency key and source
revision for retry. A later deliberate upload creates a separate snapshot.

The Global journal upload is informational source material. Existing filer
processing may extract developments or context under its configured behavior,
but the journal text itself is not an operational instruction or permission.

## Retired designs

The earlier journal review-marker and Luna reconciliation design was retired
before adoption. Those routes, grants and procedures are not part of the
current interface. This document describes source changes; merge,
deployment and role adoption require their own owner authorization.
