# Project services: Cloudflare

Cloudflare is a standalone, explicit operation of a selected project. Nothing triggers it:
creating a project does not enable, provision or check it, and it reads no setup record.

A project is created empty (an empty `hq/` and `code/` folder, its record and the owner and system memberships; see
[Project creation](BUSINESS-CREATION.md)), and only the configured active Ordubashy may create one. Every record a
project later owns (Tasks, Actions, documents, tables, routines, Settings) belongs to that one project, and
tags are shared labels rather than access. The Project itself is also a readable operational record with an
authorized inventory, explicit cross-scope links and its one private owner–Ordubashy channel; see
[Project records](PROJECT-RECORDS.md). Every optional service below, like email, domains, repositories and sandboxes,
is a separate explicit request with its own authority; none is provisioned by a template. Projects have no resident
agents: global agents reach a project through ordinary scope and permissions.

## Cloudflare

`cloudflare_zone_token_ensure` (version 2), `cloudflare_dns_records_ensure` (version 2) and
`cloudflare_api_request`. Access is the installation owner or the configured Ordubashy, for a
selected, active project.

- `domain` is required on both versioned operations: a canonical lower-case DNS zone hostname
  (no scheme, port, path, wildcard or underscore). It names the zone; callers never supply zone or
  account IDs except as optional assertions on the token operation, which must match.
- **Token** (`cloudflare_zone_token_ensure`): the zone and account are discovered for `domain` from the
  installation's read binding or verified inventory (`zone_not_found` / `zone_discovery_unavailable`
  otherwise), a token limited to exactly that zone is created or a verified saved one reused, and
  `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID` and `CLOUDFLARE_ACCOUNT_ID` are saved host-side in the
  project's Settings. The result returns IDs and evidence, never secrets. A lost-secret token is never
  re-created (`recovery_required`); nothing is deleted.
- **DNS** (`cloudflare_dns_records_ensure`): reads only those saved Settings. Missing token, zone or
  account is `configuration_required`. Before any write the provider is asked afresh whether the token is
  active and whether the saved zone is exactly `domain` in the saved account (`verification_failed` for an
  inactive or unusable token, `zone_mismatch` for another domain or account). Records must be at `domain` or
  a true subdomain. Identical records are reused, conflicts are reported, existing records are never
  changed or deleted.
- `cloudflare_api_request` is unchanged apart from the project wording.
