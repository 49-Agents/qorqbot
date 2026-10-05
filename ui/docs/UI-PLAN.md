# Chat workspace and agent presentation

Status: planned, incorporating the owner's 2026-10-07 UI direction. This is a specification update, not a built interface. Reference images will refine spacing, proportions and icon artwork later. The backend-first delivery order remains; the first UI now has a concrete single-page chat scope.

Repository: **`qorqutbot-ui`**. This frontend consumes `qorqut-core` HTTP operations, events and WebSocket streams. Core owns all application coordination/configuration, durable requests, conversation records and desired control state. `qorqut-browser` has its own MCP server and local input arbitration, connected to core through a handoff/control API; UI never calls private browser/runner/CDP/VNC endpoints directly. Settings forms, when needed, edit core through its API. Drafts, selection and collapsed panes are UI state. Build independently and serve static assets under the same origin as core through a proxy or optional static mount. Core remains usable without this UI.

## 1. One page, three columns

| Area | Required behavior |
| --- | --- |
| Left: agents | Messenger-style list using each agent's icon and display name. Click selects its conversation. Creation includes a name and icon picker. Double-click the name area to rename. |
| Middle: conversation | Persistent, provider-independent conversation view with composer, browser-action cards and compact file-reference cards. It is a view over captured history and durable application records. |
| Right: inspector | Expand/collapse without leaving the conversation. Show current work/status, activity/freshness, Browser, Machine and terminal/recovery controls. Browser and Machine address different surfaces. |

On a wide screen, the center takes remaining space between a generous navigation column and an optional inspector. Initial proportions can start around 240 px / flexible / 360 px, subject to the owner's images. At narrower widths the inspector becomes an overlay/drawer and navigation collapses; preserve a usable chat width instead of squeezing three unusable columns. Keep per-agent scroll position and draft when switching agents.

The ordinary UI uses display names/icons. IDs remain routing keys and are available in details or disambiguation, not the primary labels. Renaming cannot change a selected conversation, stream target, folder or running process. Duplicate display names remain valid.

## 2. Dedicated browser versus machine control

Keep a dedicated **TigerVNC/Xtigervnc display running one maximized Chrome per agent**, including onbash. This browser uses that agent's persistent Chrome profile and the same Chrome instance targeted by browser tools. It remains a separate surface even inside a VM that has its own graphical console.

| Surface | Execution and viewing path |
| --- | --- |
| Browser | Agent-local TigerVNC display + Openbox + Chrome; host viewer proxies through the runner to guest-loopback VNC. All inline/expanded/inspector browser presentations target this same surface/profile. |
| Machine, VM agent | General guest desktop on the VM's graphical console, separate from Chrome's TigerVNC display. Default implementation uses private libvirt VNC over a host Unix socket and a usable guest desktop in the image; P0 must prove actual desktop interaction, not merely a boot screen. |
| Machine, onbash | An independently owned local general desktop with the agent's tools and terminal. It is separate from its Chrome display and does not attach to the owner's existing personal desktop. Label it Desktop in the UI because there is no VM. |

Each surface has a stable ID, kind, current instance/environment/boot binding, dimensions, availability and control epoch. `surfaces_list` resolves these from the selected agent ID. Never infer a browser target from a VM console port or fall back to another agent's working desktop. The same noVNC viewer/relay code can present both surface kinds; no separate Guacamole service is introduced.

Machine viewing begins read-only. Explicit `desktop_control` takeover holds managed terminal delivery and browser automation through their existing control gates before enabling machine input. It does not promise to suspend every arbitrary shell side effect of an unrestricted agent. Serialize acquisition, record partial acquisition/reconciliation, and release only holds belonging to this takeover; an existing browser request or human terminal session remains owned. One agent cannot have conflicting active human input surfaces. A conflicting takeover returns the existing control/request state for explicit resolution. Disconnect leaves a visible human-control hold until release/recovery, and stale viewer epochs cannot input.

Bind this machine hold to the agent/environment, independent of whether a CLI exists. Verified-absent participants are quiescent, so initial setup and stopped-CLI desktops remain controllable; unknown participants remain reconciling. Managed start/restart/switch replacement launches and automated input wait until release. CLI stop cannot clear the hold. A reboot invalidates the old viewer and requires explicit rebind/release before admission resumes. Use internal holds without creating an artificial browser-action card.

## 3. Agent tools describe intent

Do not teach agents layout, iframe URLs, modal placement, CSS or toast mechanics. Tool descriptions tell them **when to ask for user help or present a file**, and tool results report durable request/reference state. The frontend chooses the presentation from the resulting domain records.

| Operation | Agent-facing description | Inputs |
| --- | --- | --- |
| `browser_request_input` | “Request an action from the user in your browser. Use this when the user needs to sign in, enter information, or otherwise interact with a page. Describe the needed action, then wait for completion before continuing that browser action.” | Required prompt; optional observed tab/page reference. Agent defaults to the authenticated caller; explicit target agent remains possible under unrestricted v1. Shared mutation metadata includes idempotency key. |
| `files_reference` | “Reference a file to the user when you want them to see, review, or be notified about it. Provide the file path.” | Required path; optional short label/note and explicit target agent. Agent and main conversation otherwise resolve from the authenticated caller. Shared idempotency metadata applies. |

Browser bindings, conversation IDs and viewer URLs are resolved by the backend; agents do not construct them. If the agent supplies an observed tab reference, validate its original instance and fail clearly if stale. Return request/reference ID, state and target identity, without presentation instructions. Saving a reference does not claim the user has opened it. A pending browser request blocks the relevant managed browser actions while the agent can continue unrelated work.

## 4. Browser-action card inside chat

1. The tool commits a request, its conversation entry/event and the control-transition job atomically. Publish only after commit; a tool-name match in a transcript is not the trigger.
2. Add one durable browser-action card in that agent's chat with the prompt and request state. While yielding/disconnected, show the actual waiting state instead of enabling premature input.
3. When the runner confirms human control, display the live browser viewer at the full width of the chat's content area. The user can click/type/touch directly inside it without expanding first.
4. A small top-right expand control opens the same viewer in a near-fullscreen modal. Closing the modal returns to the inline card. Neither action completes/cancels the request or opens another Chrome/profile.
5. Done and Cancel remain adjacent to the viewer, with an optional user note. Use the existing exact-request/epoch/atomic notification contracts. Completion means the user finished their input; the agent observes the page again to determine its actual result.

Render a same-origin viewer shell in an iframe, or an equivalent reusable noVNC host. It embeds the VNC client, **not the destination website**: app login and frame-embedding restrictions do not require third-party pages to permit iframe access. The backend creates viewer routes; tool arguments cannot supply arbitrary frame HTML/URLs or transport credentials.

Fit the browser framebuffer proportionally to the chat width, with a height bounded by the viewport and the stream's aspect ratio. Keep pointer/touch mapping correct after resizing. Start with client-side scaling; switching between inline/modal/inspector must not make multiple viewers fight over remote display geometry. The expanded view provides readable interaction when the inline view is small. Preserve keyboard focus deliberately; a terminal update must not steal focus from a sign-in field.

Use one mounted viewer/input connection per surface in this UI, moving or safely transferring its presentation between inline, modal and inspector. A transfer may reconnect the viewer but must retain the browser/profile/request and revoke the prior input connection before enabling a replacement. Independently connected observers remain view-only. Do not mount live streams for every historical card. Completed/cancelled cards remain in the conversation as records and do not reopen input; the inspector provides a separate current-browser entry point.

Refresh, event replay or late native transcript parsing must find the same `request_id` card, not produce another. Opening another agent's conversation disposes its viewer only; the pending request and Chrome persist. A request while the user is elsewhere records attention on that agent's list row without forcing navigation or stealing focus.

## 5. File-reference card, previews deferred

`files_reference` saves one attributed record and conversation entry containing reference ID, target agent/conversation, normalized file location/path, display label, optional note, observed metadata and creation time. It does not rely on parsing a Markdown path out of assistant text.

The first UI shows a compact, persistent card/chip in chat: file icon, filename/label, note if present and a way to copy its path. A transient toast may draw attention to a new reference, but is never the only record. Reconnect/history replay does not re-toast old references. Inactive conversations can show an attention indicator.

**Direct file opening and in-chat preview are deferred by the owner.** Do not ship a dead “Open” control or implement renderers in this increment. The eventual interaction is click the card to open the file inside the chat, using the same saved reference; this can be added without changing the agent's tool instructions.

Resolve relative paths against the target agent workspace. Accept native absolute paths by resolving them in that target's environment; `/workspace/...` maps to its host workspace, while other guest-local paths are inspected through the bound runner. An absolute guest path is never interpreted as a host path. Onbash uses its host environment. Validate regular-file metadata through structured local calls; no shell interpolation. Target/path checks preserve routing integrity, not a new permission tier.

The record is a reference to a file, not an implicit full copy/version of every mentioned file. Save observed size/mtime and hash only when computed; later access must expose missing/changed or unmapped files rather than silently serving another target. Snapshotting selected immutable artifacts already has a separate history/export contract. Persist references across provider switches and include them in exports; imported unmapped paths retain provenance. A missing/unreachable source at creation returns a typed error without a successful card. The agent can put durable deliverables in its workspace when permanence is needed.

## 6. Chat projection and replay

M07 owns a small `conversation_list` read projection combining normalized native conversation records, owner-to-agent messages, browser requests and file references. It is not a second model runtime or a raw-transcript replacement. M05 owns owner message submission/delivery; M06 owns browser request state; M09 owns file references; M10 renders their common read model.

Each entry has a stable ID, conversation ID, kind, source record reference, original occurrence time, recorded time and a stable per-conversation insertion cursor. Platform cards receive a cursor in their operation transaction. Native entries receive one on first normalization; late imports append with their original timestamp instead of changing previously issued cursors. An optional verified native tool/message link can group related entries visually; equal text is never enough to merge them.

Owner composer sends use the existing durable `messages_send` path, not a new model SDK. Preserve the stored/submitted/uncertain/handled distinctions. A verified native echo can link to the stored owner message using a supported adapter's exact native/attempt identity; without that evidence, keep the native record labeled separately rather than dropping text on a guess. Browser/file tool call records remain in the archive, but their parsing never creates a second request/reference or repeats a side effect.

Expose bounded cursor pagination and current card state. SSE/reconnect invalidates caches using event sequence/recovery epoch; clients reread records. The projection is reconstructable from raw normalized sources plus permanent platform records, retaining platform insertion coordinates on rebuild/export. Only the active conversation's live viewer connects. File bytes and unbounded terminal output are not embedded in chat event payloads.

## 7. Display names, icons and rename

Use `agent_id` internally, `display_name` and `icon_key` for presentation. Replace the earlier plan's mutable `name` field with the single canonical `display_name`; do not maintain two competing names. Keep the existing trimmed 1–120-character validation and revision-checked `agents_update`. Name/icon changes have attributed history and update every UI location without restarting execution.

The reserved host agent is initialized with display name `onbash` and icon `terminal`. Its reserved identity remains the same even if its display name is edited. Other creations default to `circle` when no icon is supplied, including agent-created agents. Users choose from the twenty fixed keys in [the core agent contract](CONTRACTS.md#2-agent-record-and-layout). Core publishes and validates the catalog; UI supplies its monochrome line artwork.

Use a small pinned SVG collection or local source-controlled vectors when implementing, with required attribution. Store the stable key, not arbitrary SVG/HTML, a remote URL, an emoji color or a model-specific logo. The catalog is fixed in v1; user-provided image uploads are outside this increment.

Double-clicking the name region enters an inline text editor; single-click selects the conversation. Enter commits, Escape cancels; blur cancels an unsaved edit so navigation does not rename accidentally. Include a keyboard/touch-accessible Rename action for users who cannot double-click. Preserve focus and drafts, show validation/conflict errors inline, and rerender only changed labels. Icon selection is a separate control, not text embedded into the display name.

## 8. Visual direction

Use an ultra-minimal dark interface: near-black backgrounds, white/off-white primary text, gray secondary text and thin separators. No purple, neon, glowing effects, gradients or futuristic dashboard styling. Prefer flat surfaces, restrained spacing and low visual noise.

Initial color tokens may use background `#0b0b0b`, surface `#111111`, primary text `#eeeeee`, secondary text `#aaaaaa` and separator `#2b2b2b`; validate text/focus contrast in implementation. Status and selection must remain understandable through labels, weight, shape and focus indicators, not color alone. These are working tokens, not a completed visual design. Owner reference images determine the final proportions and styling.

## 9. Implementation increments and acceptance

1. **UI.01 Contracts:** agent display/icon fields; durable browser/file records; resolved surface identities; conversation entry/cursor contract and generated intent-only tool instructions.
2. **UI.02 Surfaces:** retain Chrome TigerVNC; prove distinct machine console/desktop and private routes in P0/M02/M06. Establish input ownership across inline/modal/inspector and machine takeover.
3. **UI.03 Shell/chat:** implement the one-page responsive shell, agent selection/creation/icon picker/rename, bounded conversation history and durable composer.
4. **UI.04 Cards/inspector:** inline interactive browser card, top-right expansion, Done/Cancel and recovery; persistent file chip with copy path; collapsible status/browser/machine/terminal inspector.
5. **UI.05 Integrated verification:** actual noVNC pointer/touch/keyboard control in two isolated guests plus onbash; refresh/replay/late parse without duplicated cards/effects; stale-agent/frame response rejection; name/icon revision conflict; small-screen and focus behavior.

M10 spans core's API/operator layer and this separate UI repository; its presentation work belongs here. Add only the necessary core records/operations to M01/M02/M03/M06/M07/M09. Core integrates the independent browser MCP service without a UI-event broker or widget/plugin framework. Keep UI tests focused on interactions and boundary failures, sharing integrated G4/G6 evidence from [ACCEPTANCE.md](ACCEPTANCE.md) rather than duplicating backend tests. No usage examples or example apps are required. File preview/rendering remains a later task. This amendment does not authorize deployment or assert any working UI exists.

## 10. Qorqut references actually inspected

Pinned source revisions are in [PORTING.md](PORTING.md). The dedicated desktop helper declares one TigerVNC display and one assigned maximized Chrome, builds `Xtigervnc` + Openbox + profile-bound Chrome, and shuts Chrome down before the display to flush state. Qorqut's dashboard browser pane already distinguishes configured browser surfaces from a full desktop, validates agent identity and disposes stale frames. Its Chat has attachment chips and on-demand preview cleanup. Legacy browser Done atomically stores completion and a message, although the old request-creation operation is retired.

Reuse these lifecycle, binding, focus and cleanup patterns; replace business/role routing and Guacamole presentation coupling. The new explicit file-reference operation and automatic inline browser-action card are qorqutbot work. The inspected code is evidence of mechanisms, not proof that every current agent has a healthy dedicated live display. No existing sessions were changed for this planning update.
