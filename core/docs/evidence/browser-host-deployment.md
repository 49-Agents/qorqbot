# Standalone browser host deployment

2026-10-09. Browser `19c4577795f20ffd4261ca41464afb63767cb046` is on its
repository's main branch and deployed beside the existing onbash test agent.
The owner explicitly waived another review. No additional code review or
test-suite rerun was performed by Manager. The completed worker's
[evidence](https://github.com/49-Agents/qorqutbot/blob/1902b159d9e3aa860414004b46be6777478ca911/browser/docs/evidence/browser-desktop.md)
records the real installed-wheel Chrome/TigerVNC/MCP flow and focused checks.
The earlier provider-blocked review remains unperformed; its material and actions
were not resumed or used as acceptance evidence.

The deployment uses a dedicated Python environment, new onbash Chrome profile,
separate foreground desktop and MCP user services, and the pinned native helper.
It does not modify the original Qorqut installation or reuse its browser data.
The existing core/native session was not restarted by this deployment.

Deployment observations confirmed the supervisor and its Chrome, Openbox and
TigerVNC processes ready, the configured loopback listeners, and official MCP
initialization/discovery with `browser_turn` and `browser://identity`. Initial
automation admission was explicitly reconciled from the new empty installation.
There are no application credentials. No real account or browser action was used
for the deployment observation.

Operational state stays outside Git under the existing local test installation.
Browser runtime has a short persistent directory; profile, downloads and
artifacts belong to the onbash agent. The desktop service has no automatic
restart: a new Chrome lifetime requires fresh explicit instance configuration.
Package [operations](https://github.com/49-Agents/qorqutbot/blob/1902b159d9e3aa860414004b46be6777478ca911/browser/docs/OPERATIONS.md)
describe lifecycle and recovery constraints.

This is a standalone browser component deployment. Core still needs persisted
browser configuration/lifecycle coordination, native-CLI registration of the
separate MCP endpoint, durable handoff/control transactions and surface routing.
UI/noVNC, RFB input fencing, machine holds, actual login takeover and VM integration
remain open. `browser_request_input` is correctly unadvertised without a bridge;
the UI's Computer panel does not yet show this browser. Terminal-view disposal
releasing terminal control does not change browser human-hold semantics.

Private deployment records and health observations:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/browser-deployment-19c4577/`.
