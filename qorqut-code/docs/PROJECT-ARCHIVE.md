# Project archival

Ask Ordubashy to archive a project by name. It proposes
`project_archive` in Global with the project UUID, current `expected_revision`
and a rationale. The project UUID is the project's id; its current revision is the `revision` that
`project_config_get` returns for that selected project.

For this Global-only operation, MCP preserves the declared `project_id` as the
archive target, not a selected project scope. Direct and `operations_batch`
calls use the same parsing. Ordinary project-scoped reads still use their
`project_id` selector. Global agents' durable access is unchanged by archival.

Every proposal creates an **urgent human Signoff** in Global. Neither an agent's
`allow` override nor an owner-originated proposal skips review. Only the
installation owner can approve. A changed project or registered agent profile
invalidates the prepared target and requires a fresh proposal.

After approval, the service records archival. Agent sessions are not tied to a
project (every agent is a global worker running as a managed session or, in Terminals mode,
in its own terminal), so archival stops no session or terminal and launches no replacement agent. Other projects and
Global agents are untouched.

Archived projects disappear from the active workspace menu, normal agent
directory/scope, personal project task aggregate and attention overview. Their
dedicated agents cannot bootstrap or invoke project operations, and their
project routines are skipped. They no longer surface as active work. Existing conversations may retain historical references;
the current agent context explicitly identifies the active directory as authority.

SQLite retains project records, memberships and profiles. Folders
stay in place; domains, mailboxes and external services are unchanged. There is
no pause or deletion operation. Archival is not an
external-service shutdown or an OS process sandbox.

## Restore

The dashboard lists restorable projects with the owner-only read
`GET /api/root/projects/archived` (`project_archived_list`): `{items:
[{project_id, slug, name, revision, archived_at}]}`, newest archival first,
limited to projects the installation owner still owns.

The installation owner restores from Global with `POST /api/root/project-restore`
(`project_id`, `expected_revision`, `idempotency_key`), which runs the owner-only
`project_restore` operation without a Signoff. Restore returns the
project's resources to the active project menu. No agent is created, reactivated or
prepared; historical agent profiles stay inactive, and every active global agent reaches
the restored project again through derived access. A prepared restore that carries a
roster change is invalid and must be prepared again. No session starts. The result is
`{project_id, restored}`.

Archive and restore need the project-archive migration of the installed schema and the
companion dashboard. Source changes alone do not repin live profiles, refresh running Global
conversations or archive any project. The installer for v1.3 is a separate pending pass.
