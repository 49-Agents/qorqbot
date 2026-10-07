# Status-read recovery: Manager acceptance

2026-10-09. Accepted `d0ae01ccaaa3ac9b88999eec4aa366b3ddf2f339` after
complete source/test diff review and exact clean/pushed verification. The change
retains validated observations through transient read failure, labels them stale,
and keeps an existing live Terminal separate from failed HTTP observations.
New/reconnected sockets need a successful read. Identity/protocol/receipt rules
in TerminalClient are unchanged.

Manager independently ran the two new owning Chrome groups: both passed in
14.8 seconds. They exercise preserved socket/control and ordered pending/queued
input, no first/reconnect socket from errored cache, recovery, and teardown for
invalid or changed observations. Manager viewed the resulting warning screenshot;
the retained output/control, read warnings and stale labels are readable. The
worker's production TypeScript/Vite build and runtime-inspector check also passed.

Private Manager evidence is `manager-review325-chrome.log` and
`manager-review325-chrome/` in the task state directory. These are actual Chrome
checks against synthetic HTTP/WebSocket fixtures, not real core/shell acceptance.
The combined D49 core/UI flow remains pending. No deployment is claimed.
