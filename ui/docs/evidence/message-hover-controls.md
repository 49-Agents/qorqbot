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
