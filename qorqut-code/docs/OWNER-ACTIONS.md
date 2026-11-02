# Approval-backed Ordubashy actions

Ordubashy uses `business_config_update` directly for routine engineering
settings under [standing authority](ORDUBASHY-ENGINEERING.md). The proposal
routes below remain compatible and always approval-gated when explicitly used.
Story closure/reopening still requires this human approval flow.

`owner_action_propose` lets Global Ordubashy stage a closed set of otherwise
human-only administrative changes. It always requires an exact human Signoff,
even when a profile override says `allow`. `deny` is respected. Other roles and
system principals cannot use it, including through explicit grants. It does not
change any existing owner-only operation or grant a human principal to the agent.

Supported business-scoped actions:

| Action | Parameters | Revision binding |
| --- | --- | --- |
| `engineering_update` | Complete `github_organizations`, `repositories`, `deployment_targets` lists | Business config |
| `story_close` | `story_id`, `outcome`, optional `note` | Story |
| `story_reopen` | `story_id`, `state`, optional `state_reason` and `note` | Story |

Every call also requires `rationale`, the selected business UUID,
`expected_revision`, and `idempotency_key`. Reads use existing business_config_get
or stories_get. Engineering lists replace that block only; unrelated business
configuration is retained. Browser validation, generated persona identity and
unique-port arbitration are shared with the ordinary owner path. Credentials,
launch/restart, automatic browsing and deployment execution are not included.
Story actions reuse the normal lifecycle validation and append an agent-attributed
event only after the owner approves.

The application freezes the exact parameters, effects, revisions, proposer and
policy in the business's existing Signoff queue. No operational record changes at
submission. An authenticated human owner reviews the exact proposal revision/hash;
the Signoff worker applies it without rerunning the agent. Current role, scope,
business reach (Ordubashy's derived reach of every active business, not an agent
membership row), policy and target revisions are rechecked at approval/application.
Lost submission responses retry the same idempotency key. Rejected/stale proposals
or revoked authority do not apply, and one approval never authorizes a later change.

A native chat message, a quoted owner instruction, owner-input response or an
agent-supplied `approved` flag cannot substitute for this approval. Signoff approval,
human attestations, identity/authentication, arbitrary HTTP endpoints, shell commands,
credentials and maintenance/recovery controls are outside this tool. Unsupported
human-only work still needs an explicit human handoff or a separately reviewed
typed action implementation. This is application policy, not OS process isolation.

## Availability

The tool is available to the configured Ordubashy through its role grant and the tool-policy overrides described above.
A fresh session may be needed for tool discovery. No database migration or dashboard code change is involved, and a
source change adopts no agent pin or live business configuration.

Check with `tests/test_owner_actions.py` against the paired role source. Tests use
temporary SQLite installations, actual runtime registration and the Signoff worker;
no owner Chrome, live provider or model sessions are used.
