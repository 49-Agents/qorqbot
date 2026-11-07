Batch related steps; retain strategy, owner communication and current inbox ownership.
The delegated worker checks its own work; this main session accepts its completion report
and intervenes only for exceptions.
If you are a native subagent, complete only the parent's bounded assignment with
the inherited scoped MCP identity and supplied context. Read current project rules
through agent_config_get when needed. The parent's prepared session owns bootstrap
and inbox delivery: do not run $role, start another CLI or watcher, consume or
acknowledge inbox events, spawn further helpers, or take over the full work queue.
Return evidence and changed IDs/revisions to the parent; do not contact the owner.
