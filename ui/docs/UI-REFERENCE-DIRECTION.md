# Reference-driven chat and inspector

Owner follow-up, 2026-10-09: retain the previous 16px gap between consecutive
messages; the 5px request was withdrawn. Hide routine queued,
deferred and dispatching labels (including composer-unknown reasons) in bubbles;
retain confirmed submission, handling and actual delivery problems.

Owner update, 2026-10-09: message name/time appear only on hover (or keyboard
focus), subtly above the bubble, with relative minutes/hours/days. Remove the
Record details line. A hover/focus copy icon sits outside the bubble on its right
and copies only the original message text. Leave room for this control without
horizontal scrolling. This supersedes the always-visible metadata below.

D56 visual amendment, 2026-10-09. The owner supplied chat/sidebar and right-pane
reference images while the minimal-workspace implementation was running. Use
their layout/density, retaining the previously requested monochrome palette,
Qorqut v1 toggle icons, removed headers/footer, direct Terminal control and 1px
scrollbars. This is presentation over current APIs, not new backend features.

## Chat and navigation

Use the reference's messenger layout: agent replies in subtly rounded charcoal
bubbles aligned left; owner messages in a distinct lighter neutral bubble aligned
right. Keep white/off-white text, readable line height, comfortable but compact
spacing and bounded bubble width. No pink/purple accents or copied bot branding.
Preserve message roles/provenance and the existing safe literal text rendering;
no Markdown package is required in this change. Native user input stays clearly
attributed as Terminal input, and inbox references retain their disclosure.

Reduce repeated metadata visually: one small timestamp/sender line, with extra
recording/provenance in a disclosure if needed. Preserve meaningful pending,
deferred, uncertain or failed delivery feedback and all mutation errors. Do not
suggest an unconfirmed response was processed. Existing file-reference cards
remain functional and can use a compact neutral attachment-card treatment.

Use a low rounded composer dock near the bottom: one initial text row that grows
with multiline text up to a bounded height, existing Enter/Shift+Enter/IME rules,
and an accessible icon send button. Remove the routine keyboard-help line and
composer explanatory footer; keep byte-limit/validation/pending errors when
relevant. No voice/attachment/search/connect-app controls without functionality.

Sidebar retains per-agent icon, name and one clipped status-preview line, plus a
small attention indication when actually reported. Avoid repetitive 'Activity
unknown' clutter. Keep the selected row highlight, existing selection and draft
behavior, and Create at the bottom. No new agent avatars/catalog or fake unread
counts. No branding header is reintroduced.

## Right pane

Match the reference hierarchy: a larger centered agent icon, centered editable
name, then a compact horizontal Details / Library / Computer tab bar. Retain
click-name inline autosave and click-icon picker from the original D56 plan.
Keyboard/touch accessible tab selection with visible active state; no header
branding. Preserve responsive desktop pane and mobile/tablet drawer behavior.

Details: current work, activity/attention when meaningful. Keep existing runtime,
publication and identity observations under an expandable More details area so
information remains accessible without filling the default pane with internals.

Library: list validated existing file references from this agent's loaded chat
pages, deduplicated by reference_id, reusing the existing FileReferenceCard and
Copy path behavior. Label these as 'Files shared in chat'; if hasOlder is true,
show Load earlier shared files using existing refreshConversation('earlier') so
older references remain reachable. Show an empty state when none are loaded and
retain conversation-loading/error feedback. No filesystem scan, all-files claim,
new API, automatic full-history drain, download or direct-open feature. File
opening remains deferred. Switching tabs must not lose Chat draft/scroll or
mount another Terminal connection.

Computer: present Browser and Machine/Desktop as compact surface cards with
clear unavailable state until connected, plus existing TerminalSummary. A small
monitor icon and neutral preview-shaped box may express the surface layout but
must not imply a live screen or be a dead interactive control. Keep Browser and
Machine distinct. Do not invent a VNC endpoint, launch services or add takeover
functionality in this visual change.

Retain inspector mutation feedback even when a rename fails or the tab changes.
Default to Details; reset tab to Details when selecting a different agent.

## Scope/checks

UI worker may additionally adjust FileReferenceCard presentation (preserving
copy-path, complete path/note access, observed-size semantics and validation),
and the related existing UI tests. No core worker scope changes. Extend the
planned grouped Chrome checks for compact bubbles/composer, inspector tabs,
shared-file selection/earlier pagination, and disabled unavailable surfaces. Use
screenshots at 937x779 and 390px, plus a wide desktop, to inspect relative sizing,
readability and overflow. No second test suite or broad test matrix.
