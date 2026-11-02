# Ordubashy inbox read discipline

Ordubashy reconciles pending owner messages from the beginning of a fresh
`inbox_list` pass and follows `next_cursor` only within that pass. A watcher
notification cursor is not a handled-event watermark. Each private event points
to its authorized source; Ordubashy reads the complete private thread with
`owner_conversation_get`, follows `next_before`, then handles or explicitly
defers the event before acknowledging its exact ID. Acknowledgement records
inbox handling; it does not complete the source work or mark the private thread
read.

The host adds a bounded native checkpoint correction for the exact
configured coordinator's current session when it has a currently authorized
pending owner delivery. The correction contains no private body, source pointer,
project name or private count. Reading alone does not clear it. It resolves
after the delivery is handled, explicitly deferred, revoked or no longer visible.
The checkpoint checks a finite pending-delivery snapshot in keyset pages of 100
and stops at the first currently visible event. Page memory is bounded, but the
worst-case authorization work grows with the pending private backlog. This is a
checkpoint-only safeguard, not an always-running watcher scan. It keeps one
aggregate finding episode for the backlog: new messages during that episode rely
on normal inbox delivery and do not create another checkpoint correction.
Existing claim suppression remains in force until the backlog clears. It does
not require an empty inbox, perform work, defer messages, poll, wake a session or
clear an owner hold.

Focus board navigation uses bounded `response_detail="summary"` pages. Read the
selected card, full conversation, source and current action evidence through the
ordinary detailed reads before acting. Summary data is not write evidence. The
mandatory `agent_config_get(response_detail="context")` read remains without
`sections` when entering a project.

## Release boundary

This discipline depends on the host's compatible inbox and checkpoint behavior. A
published role version is immutable and source changes here do not alter it:
publication and adoption are separate, owner-authorized steps, and adopted
profiles are prepared to refresh the selected instructions. Role source changes
do not change live pins, prepare agent workspaces, restart sessions or activate
any production behavior.
