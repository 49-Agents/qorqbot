# D63 — Finish local-agent file, browser and UI support

Manager decision, 2026-10-13, following the owner's D62 local-folder/tmux choice
and request to fix the desktop QA findings. This is a small extension of the
existing operations, not a new runtime or service. Implementation is pending.

## Files

Any prepared `kind='host'` agent may use `files_reference` with an absolute host
path. Remove the reserved-onbash identity restriction only. An absolute path
inside the target agent's own workspace becomes a workspace-relative reference;
other absolute paths remain host references. Relative paths retain their current
meaning. Legacy `vm` targets still have no absolute guest-path mapping.

Preserve metadata-only observation, no-follow checks, registered actor/target
attribution, initialized workspace and lifecycle checks, installation/recovery
binding, exact-request replay and automatic conversation/daily publication. No
file content copying, download API, new operation or schema change.

## Browser attachment

Any prepared local host agent may attach its own explicitly configured browser
service through `browsers_attach`. Remove the reserved-onbash identity check;
keep the host-kind, lifecycle, workspace, agent ID, recovery epoch, environment,
boot and service identity checks. Per-agent attachments, control receipts,
handoff cards and completion messages keep their existing semantics.

Never borrow onbash's browser, profile or attachment for another agent. Creation
does not start a browser service, provision a desktop, copy a profile or start a
provider CLI. A missing attachment remains unavailable. Existing replacement,
uncertain-effect, restart and recovery behavior is unchanged.

## Desktop presentation

- Creation copy: "Creates a local folder and terminal. Start and configure your
  CLI in Terminal."
- Successful registration notice: "<display name> created." It reports the
  durable creation fact without retaining a stale provisioning label or claiming
  a connected CLI. Keep current per-agent runtime observations authoritative.
- More details calls host-kind agents "Local agent" and historical VM-kind
  agents "Legacy VM agent".
- Keep the Details, Library and Computer tabs. Computer shows its real Browser
  component and Terminal status. Remove the inert Machine/Desktop placeholder;
  D57's instruction to keep that unused card is superseded for this version.
- Existing synthetic UI fixtures default to local host agents with
  `start_requested=false`; icon choice stays independent of agent kind. Do not
  make fixture output evidence of a working native provider.

## Focused verification

Check ordinary-agent absolute paths inside and outside its workspace, exact
replay after source deletion, correct conversation/daily publication and existing
invalid-path refusals. Check two local agents with two isolated browser peers:
distinct attachments/requests, wrong-agent identity refusal, independent control
and completion notification/card routing. Simulated peers must be labeled as
such; they do not establish actual Chrome/VNC acceptance.

For UI, update the existing creation and Computer/browser scenarios, build once,
and inspect the changed views in disposable desktop Chrome. Preserve existing
retry, uncertainty, manual CLI startup and no-auth behavior. No mobile redesign,
new test framework, broad repeated QA or observer-performance redesign.
