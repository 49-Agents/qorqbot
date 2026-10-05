# M06 — Browser, VNC desktop and human input

Status: Planned. Owns R06/R13/R15 browser behavior. Depends on M01–M03/M05; conversation entries integrate M07, artifacts use M09 and the viewer uses M10.

## Outcome and boundary

Every agent has one persistent headed Chrome profile and application desktop. Browser commands, screenshots/downloads and owner intervention use that exact instance. Login state survives ordinary Chrome/VM restart because its profile is retained. An owner can answer a browser input request without opening a separate browser profile.

Run Chrome, TigerVNC/Xtigervnc and Openbox on a dedicated browser display inside the VM. Use fixed guest-local display/ports and agent-local ext4 profile state. Onbash runs the same supervisor locally with separately allocated persisted resources. Preserve this Chrome surface alongside the distinct general machine graphical console/desktop supplied by M02. The Browser viewer always targets Chrome's display, never substitutes the machine/firmware console.

## Browser supervision and automation

Supervise owned display/window-manager/Chrome processes and bind them to a browser instance ID plus OS/process fingerprints. Start only when the intended HOME/profile/storage mounts are ready. Reconnect attaches to current Chrome; it never creates a fresh profile. Shutdown closes Chrome and verifies exit/profile flush before removing the display.

Place desktop processes in an agent-owned systemd runtime unit independent of the runner and native CLI units. Actual runner-service restart must leave this unit and its process identities intact; ordinary daemonization is insufficient.

Adapt the pinned `agent-browser` external-CDP helper. Execute it inside the runner's environment against fixed loopback CDP. Expose a typed `browser_turn` action union for navigation, snapshots, click/fill/type, tabs, upload/download, screenshot and supported evaluation. Construct argv as data; arbitrary helper session/profile flags cannot accidentally select another profile. This is target integrity, not an operation permission policy.

Serialize commands per browser. Store command ID, expected instance/control epoch and outcome. Reads may retry when explicitly harmless. Actions such as click/submit with uncertain execution are not blindly repeated. Screenshot/large DOM results become agent-owned artifacts with bounded tool responses. Native tools remain available, but the managed coordination guarantee covers calls through this browser contract.

## Desktop control

The host serves noVNC and proxies WebSocket bytes through the verified runner to guest-local VNC. Default connections observe. Manual “Take control” calls `browser_request_input` with a manual-control prompt, reusing the single active request/control state machine; release uses Done or Cancel. Input-capable connections are tied to the current control epoch. Enforce view-only versus input-capable behavior at the server/runner VNC boundary using an existing protocol-aware facility or a narrowly tested RFB input filter; client JavaScript alone is not an enforcement mechanism.

A control transition waits for the currently executing managed browser command to settle. A timeout remains yielding/interrupted and must not claim automation is paused while an action is still running. If a browser process dies, retain request history and reconcile to a new instance rather than treating a recycled tab ID as the old target.

`surfaces_list` resolves browser/machine kinds and exact instance/epoch bindings for the inspector. `desktop_control` machine takeover coordinates terminal/browser holds under a fixed acquisition order; enable machine input only after both are confirmed. Conflicting existing human control stays intact; partial acquisition remains reconciling and can release only its own holds. Disconnect retains explicit control state. Reuse viewer/relay components, with M02 hosting the VM-console path and the local runner hosting onbash's separate general desktop.

Acquire an agent/environment-wide admission hold first, without creating a browser-request card. Verified-absent CLI/browser participants count as quiescent; unknown ones do not. This permits desktop setup when no managed run exists and retains the hold through CLI stop. Managed start/restart/switch replacement and automatic input wait until release. New boot/environment identity invalidates viewers and requires explicit hold rebind/release before automation. Tests include stopped-CLI takeover, failed/unknown participants, future-run admission and reboot during control.

Human control is cooperative: it blocks managed automation but cannot prevent an unrestricted agent from using raw shell/CDP. Do not market it as an OS security boundary.

## Request and completion flow

`browser_request_input` describes the needed user action without UI mechanics. It persists prompt/requester, resolved target agent/conversation/browser, optional validated tab reference, idempotency key and one conversation entry/event atomically with the transition job. At most one input request actively owns a browser in v1; another request returns current request/conflict rather than hiding an overlapping handoff.

The request becomes ready only after the runner confirms human control. The chat then displays a full-width, directly interactive browser card with a top-right expand control for a near-fullscreen modal, optional note and Done/Cancel. Inline/modal/Browser inspector reuse the same surface/profile and one UI input connection. Preserve request state during layout/viewer transfers; never duplicate Chrome or create competing resize/input owners. Closing a viewer changes no request state. Historical resolved cards stay records, not automatically reactivated streams.

Done commits request completion, response attribution, control-epoch advance and one message to the target agent in a single transaction. The runner must acknowledge the new automation epoch before managed commands resume. Repeated Done returns the saved completion and message IDs. If the CLI is offline, its completion message stays queued. The agent reads the current page again; completion does not prove authentication or the requested external action succeeded.

Cancel accepts an unresolved requested/ready/interrupted request. Commit its terminal cancellation, control-epoch advance and exactly one attributed cancellation message atomically; never emit successful completion for cancellation. Concurrent Done/Cancel has one winner, with incompatible later transitions rejected. Revoke old input-capable viewers immediately. During yielding, cancellation still waits for the earlier command to settle; during disconnection, retain reconciling state until the runner confirms the new epoch. Neither cancellation nor completion undoes an external browser action. Closing a viewer never implicitly performs either transition.

A host/runner restart preserves pending human control and request state. VM/Chrome restart preserves the request but invalidates old tab/instance references. Rebind the viewer explicitly and keep automation paused until the same request is resolved.

## Implementation sequence

1. **M06.01 Supervisor:** extract process ownership, display startup and ordered Chrome shutdown/profile persistence; support VM and host targets.
2. **M06.02 Automation adapter:** port the bounded external-CDP command mapping and artifact outputs with browser instance/epoch checks.
3. **M06.03 Viewer:** build the direct noVNC relay, reconnect, view-only/input mode and server-side control fencing. Reuse pinned upstream assets.
4. **M06.04 Requests:** implement intent-only tool records and atomic conversation entries, yielding/ready/completed/cancelled transitions, manual takeover reuse and atomic Done/Cancel message logic. Coordinate machine holds with existing input gates.
5. **M06.05 Recovery:** cover pending handoff after host/runner/Chrome/VM restart, stale viewer closure and uncertain browser commands.
6. **M06.06 Acceptance:** test independent profiles, synthetic login cookies, downloads, actual browser interaction and human-control arbitration in two real guests plus onbash.

## Source decision and tests

P08–P10: reuse Qorqut's current `agent-browser` mapping, desktop process lifecycle and atomic legacy Done pattern. Replace business/proxy ownership, Guacamole deployment binding and retired request-creation operation.

Tests must include cookie survival, distinct same-site cookies between agents, detach leaving Chrome alive, profile-lock collision, missing mount, browser instance replacement, a command still running during takeover, duplicate/concurrent Done/Cancel, cancellation during yielding/disconnection, manual takeover/release, offline completion delivery, stale viewer input after release, and pending request after restart. Browser fixtures prove actual DOM/cookie behavior; viewer-routing stubs alone are insufficient.

Add inline-to-modal-to-inspector transfers with live keyboard/pointer/touch, correct scaling/focus and one input owner; durable card replay without new handoff; separate machine/browser targets and partial machine-control acquisition/release. [UI-PLAN.md](../UI-PLAN.md) owns presentation details.

**Done:** G4 browser acceptance passes and the minimal UI can complete a real isolated login-style handoff on the exact agent profile without losing state or racing automation.
