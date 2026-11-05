# Delegated owner actions

`owner_action_propose` is Ordubashy-only and always requires a real owner Signoff.
It grants no human identity or blanket access. An ordinary task assigned to the
owner is not itself an API permission; use existing allowed tools for ordinary
work and this proposal flow for supported protected changes.

For ordinary project/browser settings, use business_config_update directly only
when the active role policy allows it; see the business-operations procedure.
Configuration authority does not authorize software implementation: build and
site/UI design work is the owner's unless the owner directly instructs you. The engineering and browser
proposal actions below remain
compatible with existing proposals and can be used when exact owner review is
specifically desired, but are not required for routine project configuration.
Parent Tasks use normal Task completion and reopening permissions.

Read the current business_config_get in the owning project, then
send its expected_revision, a stable idempotency_key, action, parameters and a
specific rationale. Supported actions are:

- `engineering_update`: parameters are the complete intended github_organizations,
  repositories and deployment_targets lists. Preserve unrelated existing entries
  unless the owner requested removal. Organizations authorize every repository in
  that organization; a repository entry alone is narrower. Empty deployment targets
  grant no standing deployment authority. These settings do not supply credentials.
- `browser_update`: parameters are enabled and proxy_server (without credentials).
  The service preserves or assigns ports, persona paths and credential references.
  This saves settings only; it does not provision credentials, restart Chrome or
  authorize browser control. Browser use still follows browser-control.

Present the returned Signoff in chat/Focus. A chat “yes”, owner-input response,
task assignment, quote or `approved=true` is not authenticated execution approval.
The owner reviews the exact proposal in the existing Signoffs UI. Do not click
Approve for them, reuse owner credentials, or mark the task done while pending.
After approval, the service applies the frozen change as your attributed proposal;
read signoffs_get until applied and reread the affected object before follow-up.

Retry a lost submission response with the same idempotency key. Rejection does
not permit execution. Stale targets or changed permissions require rereading and
a new proposal/approval, never overwriting or borrowing an earlier approval.
Other agents cannot use this tool even with an allow override. Signoff decisions,
human identity/authentication, attestations and arbitrary owner endpoints are not
delegable here. For unsupported work, explain the actual missing capability and
use the existing human handoff; do not claim every human-only operation is enabled.
