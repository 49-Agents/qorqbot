# Trusted Business Sandbox Adoption And Rollback

Adoption is a trusted local-operator action, never an HTTP, MCP, dashboard, or
automatic fallback path. Before calling it, ensure the production authority and
write admission are current and the selected business has usable sandbox
configuration and its assigned proxy credentials. `SandboxAdministration.ensure`
establishes the first intent only after taking installation admission exclusively
and proving the *selected* business has no native broker tmux sessions (using its
trusted session prefix) and no legacy browser unit, process, or held profile lock.
It does not drain, stop, or otherwise disturb another business. Production
composition rechecks authority during this admission; a missing inventory or any
ambiguous host writer refuses adoption. A committed intent never falls back to
host execution.

For a live pilot, set `enabled: true` and `provision_new_businesses: false` in
the private host configuration. The operator can adopt the selected existing
business while new business creation keeps its current host behavior. The
optional provisioning flag defaults to `true` for existing enabled deployments.
It does not change or undo any committed sandbox intent.

An old Chrome profile can retain `SingletonLock`, `SingletonSocket` and
`SingletonCookie` symlinks naming its former host process. If Chrome refuses to
open the adopted profile, first request a sandbox stop and wait for the committed
`stopped` state. Verify the legacy browser unit and all owned containers are
stopped, then acquire `service.lock` exclusively before archiving those stale
symlinks outside the profile. Preserve the profile itself and the archived lock
targets. Never remove locks from a running or ambiguously owned browser. Request
start again and require normal native browser readiness to pass.

Rollback is likewise explicit and local. First request a stop and let lifecycle
reconciliation publish the exact unchanged `stopped` row; it must have no active
lease. `rollback_to_host` then takes installation admission exclusively, drains
the selected listener, requires all exact owned containers to be stopped and
unpaused, requires the browser profile lock to be clear, removes only the owned
containers/networks and their bridge firewall rules, and re-inspects for absence
before deleting the intent at the supplied revision. Workspace files, persistent
home/profile data, agent UUIDs, and credentials remain in place. Any failed or
ambiguous proof retains the intent; there is no automatic rollback. This guide
does not authorize a live migration, deployment, or model session.

```python
# Trusted local operator code after config/proxy preparation and host-writer drain.
row = sandbox.administration.ensure(business_id)
stopping = sandbox.administration.request(
    business_id, "stop", expected_revision=row["revision"]
)
stopped = sandbox.lifecycle.ensure_business_environment(business_id)
assert stopped["state"] == stopped["desired_state"] == "stopped"
sandbox.administration.rollback_to_host(business_id, expected_revision=stopped["revision"])
```

Focused checks cover refusal, revisions and the legacy-browser startup
interleaving, including another business remaining usable during adoption.
The complete disposable `assemble()` rehearsal on core `a974b52` also verifies
actual adoption, stop and rollback against Docker, preserving files and agent
identities before the separate original-path restore. This is not live-business
migration acceptance.
