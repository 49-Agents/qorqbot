# Ordubashy operational coordination

Ordubashy coordinates authorized operations across its permitted projects and
handles the shared owner/self intake queue identically for intake. Ordubashy
routes implementation work to the responsible worker using inbox/tasks and may
request work or clarify facts, but does not become the builder or supervise a
worker's terminal. It may maintain configured operational settings with
`business_config_update` where existing grants permit it; this does not grant
human build authority or transfer ownership of software and design work.

The application uses refreshed registered role identity, current business scope
and active membership, never agent names or caller-supplied role claims. Only the
human owner and scoped Ordubashy may change the reserved `engineering`
block. This is checked when preparing and validating application of the change.
Other agents remain blocked even with an allow override. Current revisions,
idempotency, audit attribution, configuration validation and record transactions
are unchanged. Config is replacement-style: read first and preserve unrelated
fields. Omitted reserved blocks retain existing values.

Engineering lists are credential-free. Organizations authorize all their repos;
repository entries are narrower exceptions. Ordubashy may update those lists and
deployment targets for verified business-owned resources. Credentials do not
establish ownership. Empty deployment targets remain unconfigured. Projects have no browser settings: a
`browser` section cannot be set or changed (an older installation's stored section is kept inert).

Registered business UUID/slug, owner and storage paths remain immutable settings.
Signoff approvals, Story closure/reopening, agent grants, global platform controls
and separately protected operations do not inherit engineering authority. Existing
`owner_action_propose` engineering actions are retained for compatibility
and optional exact owner review; routine Ordubashy edits use the direct config
operation. Story lifecycle proposals still require a real human Signoff.

Both native projections include each active, authorized business's validated
`code/` directory, including when Ordubashy starts in Global. Claude receives Bash,
Edit and additional directories; Codex receives code write roots. Neither gets the
entire businesses root as a write directory. HQ records and application credentials
retain generated protections. Preparation rejects unsafe/symlinked code roots,
creates code directories only during application, and never deletes code on scope
or role revocation. Current-business bootstrap includes engineering config and code
metadata, not every business's full config. Reprepare after business/scope changes
to refresh native roots; server tools always check current scope. Native CLI policy
is a workflow guardrail, not same-user OS isolation.

## Activation

Source edits do not upgrade live profiles. Publishing/adopting a matching Ordubashy
role digest, reconciling shared instruction revisions and re-preparing affected native
profiles are separate release steps through supported APIs, preserving overrides and
unrelated sessions. Existing conversations may need context/native-permission refresh.
No database migration is required. Native project authority does not supersede a
repository's own review and release rules.

Tests: `test_ordubashy_engineering.py`, `test_owner_actions.py`,
`test_business_code_preparation.py` and companion role policy tests.
They use temporary installations, not production repositories, browsers or deploys.
