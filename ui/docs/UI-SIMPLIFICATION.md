The owner's subsequent [reference direction](UI-REFERENCE-DIRECTION.md)
also applies to chat bubbles, compact composer and inspector tabs.

# Minimal workspace feedback

Decision D56, 2026-10-09, from the owner's seven annotated layout requests and
follow-up removal of the Terminal footer. This supersedes conflicting header,
rename-on-blur and manual viewer-control details in UI-PLAN and
TERMINAL-IMPLEMENTATION. No dependencies or application operation changes.

## Presentation

Remove the entire navigation branding header (q, Qorqut, top plus). Keep the
agent list and its small label/count; move Create agent to the bottom area as
an accessible plus button. Preserve mobile access to navigation.

Remove the conversation header/name/status strip. The center pane extends the
full workspace height. Place compact floating icon buttons at its top right:
Chat, Terminal and Details; maintain accessible names, title tooltips,
aria-pressed/expanded, focus outlines and touch hit areas. These remain visible,
not hover-only. Keep a mobile Agents button reachable in this floating control
area. No full-width header background/border/row. Use a small content inset as
needed to keep the first message/terminal line from sitting under the controls.
Retain drawers/responsiveness, per-agent drafts/scroll/view and all error notices.

Use the actual Qorqut v1 toggle artwork from globalChatViewToggle in pinned
components/dashboard/cfbbd53c77758d50b521a32d1d4ce19b06ae5a54/code/js/global-chat.js:
Chat SVG path M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z;
Terminal path m4 17 6-6-6-6M12 19h8. Both viewBox 0 0 24 24, size18,
stroke1.75, currentColor, rounded ends/joins, no fill. Use a small local
presentation icon component; do not change the twenty agent icon keys. Preserve
applicable attribution when adapting these vectors. Details may use a matching
simple right-pane outline. No icon package or backend/UI source coupling.

Remove visible Rename and Change icon buttons from inspector. Clicking its
name edits it in place; save on blur or Enter, Escape cancels, no-op unchanged
or blank names do not submit. Trim and honor existing 1-120 character validation.
Guard Enter followed by blur against duplicate calls and ignore composing Enter.
The existing revision/idempotency/pending/conflict path remains the authority;
show pending/failure/attempted value feedback in the inspector where editing
started. Keep the name keyboard accessible. Clicking the icon itself opens the
existing icon picker, with its current Save/Cancel behavior. Disable edits while
a prior mutation for that agent is pending. Sidebar double-click rename can
remain, use the same blur/Enter save semantics, and remove the standalone row
Rename ellipsis. Never rename/select a different agent from a stale edit.

Remove horizontal scrolling from conversation history/composer and Terminal.
Wrap long Chat text/paths; bound flex/grid children. Terminal uses its existing
fit/resize logic after control acknowledgement and on container resize rather
than exposing a wider grid scroll. Keep native input/resize order intact.
Vertical scrollbars use the existing gray thumb (#414141 for chat), 1 CSS px
wide, transparent track with no visible rail, no top/bottom arrow buttons and no
corner. Apply consistently to conversation, composer and terminal scrolling;
inspect actual xterm6 custom scroll DOM/styles rather than only styling an unused
native scrollbar. Keep wheel/touch/keyboard scrolling functional. Chrome is the
acceptance browser; use a reasonable thin fallback where exact sizing differs.

Remove the entire Terminal footer, including 'Start and configure your CLI here.
Input receipts confirm submission only.' No replacement footer/help row.

## Automatic Terminal control

Use the existing terminal protocol. After exactly one validated hello per fresh
socket, request take with its current observed epoch. Input/resize remain disabled
until acknowledged control for this viewer. No toolbar or normal control status.
Show Connecting or disconnected/conflict feedback only when useful, with
Reconnect terminal for a failed/lost-control connection. Do not retry take in a
loop after a different viewer takes over, and never replay queued/uncertain input.
Reconnect deliberately opens a fresh connection and requests control once.

Core D56 guarantees stream.close releases only its own human hold after settled
effects; switching back to Chat or another agent disposes the socket as before.
No separate release button, UI cleanup RPC or new lifecycle state machine is
needed. Update TerminalSummary text that describes permanently retained holds.
Preserve the native CLI, recording, history and agent inbox ownership.

## Scope and focused verification

UI worker owns main.tsx, icons.tsx or a small view-icons sibling, style.css,
terminal.tsx, terminal-client.ts, existing UI/synthetic terminal fixtures and
concise evidence. No store/business-rule rewrite. The core worker separately
owns stream close. SyntheticTerminal should reflect release on owned disconnect
without clearing another viewer. Update existing UI checks/selectors changed by
header removal/auto-control; retain meaningful input, identity, recovery and
pending mutation assertions. Do not add a second suite for the same behavior.

Run production build and affected owning Chrome checks: desktop and 937x779
layout, 390px mobile/drawers, click-name blur/Enter save once/cancel/conflict,
icon picker, moved Create action, one auto-take per socket, no input before ack,
release on Chat switch, fresh control on return, takeover/conflict and reconnect
without input replay. Check overflow and actual 1px/no-arrow scroll styling,
long text and terminal fit. Keep original logs/screenshots privately, commit only
concise evidence. Manager tests one combined real-core flow with disposable
workspace before publishing to the owner URL; never send test keystrokes to the
owner's active native CLI. User changes are not deployed until this pair passes.
