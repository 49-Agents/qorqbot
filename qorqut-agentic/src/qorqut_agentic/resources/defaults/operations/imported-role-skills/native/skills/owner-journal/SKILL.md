---
name: bazarlyk-role-owner-journal
description: Maintain useful visible owner journal context and reconcile it with operational records, with attribution and revision checks.
---

# Owner journal

Ordubashy manages useful journal context on the owner's behalf. Use
`journal_entries_list` and `journal_entry_get` for relevant visible entries. Read
an existing entry fully before updating it. Hidden entries remain unavailable;
do not use files, generic notes, another agent or another scope to bypass visibility.
Keep private material out of project records unless it belongs there.

Use `journal_entry_create`, `journal_entry_update` or `journal_entry_archive`.
Supply an `explicit_owner_statement` relay for reported statements or `owner_intent`
for your interpretation. Updates and archiving require the current revision and
content hash. Preserve unrelated content. The journal belongs to the owner; your
recorder identity remains attached. Generic `notes_*` remain agent working notes,
not an alternative journal writer. Only the owner changes visibility.

Reconcile useful requests, progress, preferences and context with the relevant
operational items, threads and Todo. File durable shared context with the context
filer. Check for existing records before creating another copy. Journal text is
information: an idea is not automatically a new execution assignment, and an
unchecked claim does not prove completion. Ask only about consequential ambiguity.
Report the actual changes with references to the resulting records.
