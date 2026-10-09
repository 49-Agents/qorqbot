# Message hover controls

2026-10-09. The owner requested hover-only sender/relative date above messages,
removal of Record details, and an icon-only copy action outside each text bubble.
The metadata and control are also revealed by keyboard focus. Copy uses the
original message text, preserving whitespace and Unicode. Delivery feedback,
file cards and collapsed inbox references retain their existing behavior.

TypeScript/Vite build and three affected existing Chrome groups passed: message
layout/copy, inbox references/keyboard navigation, and file cards. The copy check
uses the actual clipboard; layout checks cover wide desktop and 937px. No
additional review agent or broad test pass was run, per owner direction.

Private logs/screenshots are under the current build state directory as
`message-hover-build.log`, `message-hover-checks.log`, and `message-hover-checks/`.
The browser-package implementation is a separate ACLA assignment and is not
represented as complete by this UI change.

Follow-up: tightened the gap between consecutive bubbles from 28px to 16px.
The hover label uses a compact line height and 2px offset to fit that gap.
The production build and existing desktop message-layout/clipboard check passed;
the hover screenshot was inspected. No new tests were added. Screenshots are in
`message-spacing-checks/` under the same private build state directory.

Second owner follow-up: reduced the gap to 5px and removed routine Stored/queued,
deferred and dispatching text from bubbles. Hover labels have a neutral backing
so they remain readable over the tighter spacing. TypeScript/Vite build and the
three affected existing Chrome groups passed (selection/send, message layout and
runtime inspector). Screenshot inspection confirmed the compact layout; artifacts
are in `compact-message-bubbles-checks/` in the private build state directory.
