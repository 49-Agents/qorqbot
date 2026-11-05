# Task type icons

- `story.svg`: an open book, for a Story dashboard.
- `customer.svg`: a handshake, for a Customer record.
- `email.svg`: an envelope, for a whole email conversation represented as a Task.

All use a 24×24 viewBox, rounded 1.6-unit strokes and `currentColor`.
They remain legible at 16–20px. Use the same subdued text color as other Focus
controls, without a surrounding badge, border or permanent text label.

Focus and Task rows use these SVGs as centered CSS masks, inheriting the UI color.
The existing Task state button stays separate and clickable. Icons follow the
stored `task_type`, including nested Tasks; ordinary Tasks receive no extra icon.
Focus receives the type through its existing authorized projection, with no
additional reads per card. The icon has an accessible type label and a tooltip.
