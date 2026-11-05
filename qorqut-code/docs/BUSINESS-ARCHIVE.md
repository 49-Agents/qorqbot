# Project archival

Ask Ordubashy to archive a project by name. It proposes
`business_archive` in Global with the project UUID, current `expected_revision`
and a rationale. The business UUID is the business's id; its current revision is the `revision` that
`business_config_get` returns for that selected business.

For this Global-only operation, MCP preserves the declared `business_id` as the
archive target, not a selected business scope. Direct and `operations_batch`
calls use the same parsing. Ordinary business-scoped reads still use their
`business_id` selector. Global agents' durable access is unchanged by archival.

Every proposal creates an **urgent human Signoff** in Global. Neither an agent's
`allow` override nor an owner-originated proposal skips review. Only the
installation owner can approve. A changed project or registered agent profile
invalidates the prepared target and requires a fresh proposal.

After approval, the service stops that business's registered terminal sessions
through its existing execution backend, then records archival. It does not launch
replacement agents. Other businesses and Global terminals are untouched. An
unavailable or failing terminal backend leaves the business visible and reports
failure; any sessions already stopped remain stopped. Missing sessions are safe
to retry, and a successfully applied Signoff does not repeat terminal reset.

Archived businesses disappear from the active workspace menu, normal agent
directory/scope, personal business task aggregate and attention overview. Their
dedicated agents cannot bootstrap or invoke business operations, and their
business routines are skipped. They no longer surface as active work. Existing conversations may retain historical references;
the current agent context explicitly identifies the active directory as authority.

SQLite retains business records, memberships and profiles. Folders
stay in place; domains, mailboxes and external services are unchanged. There is
no pause or deletion operation. Archival is not an
external-service shutdown or an OS process sandbox.

## Restore

The dashboard lists restorable businesses with the owner-only read
`GET /api/root/businesses/archived` (`business_archived_list`): `{items:
[{business_id, slug, name, revision, archived_at}]}`, newest archival first,
limited to businesses the installation owner still owns.

The installation owner restores from Global with `POST /api/root/business-restore`
(`business_id`, `expected_revision`, `idempotency_key`), which runs the owner-only
`business_restore` operation without a Signoff. Restore returns the
project's resources to the active project menu. No agent is created, reactivated or
prepared; historical agent profiles stay inactive, and every active global agent reaches
the restored project again through derived access. A prepared restore that carries a
roster change is invalid and must be prepared again. No session starts. The result is
`{business_id, restored}`.

Archive and restore need the project-archive migration of the installed schema and the
companion dashboard. Source changes alone do not repin live profiles, refresh running Global
conversations or archive any project. The installer for v1.3 is a separate pending pass.

Validation: `app/tests/test_host_business_archive.py` (stock host) exercises approval
and rejection, stale targets, terminal failures/isolation, retained records, scoped
discovery, forming another business after archival, the owner-only archived list and
restore (which creates no agent and refuses a roster-change plan), and the trusted terminal reset. The companion dashboard's
`tests/test_business_archive_browser.js` checks delayed application refresh.
