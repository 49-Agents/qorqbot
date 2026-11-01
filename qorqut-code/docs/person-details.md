# Person details

People retain their existing context identity and relationships. Contact documents
store `affiliation` (`internal` or `external`), `contact` (`email`, `phone`, `notes`),
and the existing `summary` in registered metadata. Human accounts store these
business-specific details on their existing business membership (`person_json`),
without duplicating the person or modifying identity or access rights.

The installation owner defaults to internal; everyone else defaults to external.
No backfill infers affiliation from names, roles or relationships. Contact fields
are optional; partial updates preserve omitted fields, and empty strings clear
values. Existing email and phone metadata remain readable. Authored summaries
retain ownership and are not replaced by generated summaries.

`context_items_get` exposes these fields for either kind of Person.
`context_people_update` accepts contact, summary and affiliation updates using
`document_id` (including a human Person ID) and the current revision. Human
account identity fields remain outside this operation.

`context_people_affiliation_set` uses ordinary role defaults and explicit profile
overrides, including for compound person edits. There are no role-name exceptions:
`allow` applies directly, `signoff` proposes an exact change, and `deny` blocks it.
The business-agent role denies Person creation, updates, affiliations and merges
by default; an explicit owner override can allow them or require signoff.
Current scope, policy and target revisions
are still checked, including when applying an existing signoff. Classification
never changes account identity, membership or access rights.

Apply migration 025 with the service stopped and a database snapshot before
restarting the updated service. It only adds the membership details JSON column;
existing memberships, owner flags and contact documents remain intact.
