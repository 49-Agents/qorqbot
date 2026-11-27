// ---------------------------------------------------------------- init
// Stage 2 PR6: push replaces the 12s poll for both -- refreshSide's own
// `!state.project`/`document.hidden` guard (js/side.js) and rtRefresh's own
// `rtGlobal()`/`rtVisible()` check make a global (every-project) subscription
// safe to over-fire; each still keeps a 60s safety poll (section 2.3: a
// missed event delays a view by at most 60s, never breaks correctness).
// The record types extensions add (js/tasks.js), read once so their labels show; a type editor reads them again.
if (typeof taskRecordTypesLoad === 'function') void taskRecordTypesLoad();
if (agentWorkspaceEmbedded()) {
  window.agentWorkspaceReady = agentWorkspaceBootEmbedded();
} else {
agentWorkspaceShellStart();
// How agents run (js/agent-mode.js): read at once and given 1.5 s at most. A slow read starts the page as managed sessions and
// repaints through `agent-mode-changed` when the answer says terminals.
if (typeof agentMode !== 'undefined') void agentMode.load();
onChange(['action_logs', 'signoffs', 'jobs_routines'], null, refreshSide);
setInterval(refreshSide, 60000);
onChange(['jobs_routines'], null, () => { if (rtGlobal() && rtVisible()) rtRefresh(); });
setInterval(() => { if (rtGlobal() && rtVisible()) rtRefresh(); }, 60000);
// The owner signs in to Qorqbot (js/account-gate.js), then gets Claude Code or Codex ready (js/agent-clients.js),
// before the walkthrough can open.
// The agent screen never keeps the walkthrough from starting: whatever it does (even a missing script), boot() runs.
loadProject().then(() => accountGate.boot()).then(() => { try { return Promise.resolve(agentClients.preflight()).catch(() => null); } catch (_) { return null; } }).then(() => onboarding.boot());
replyNotifications.start();
connectEvents();
}

// Tab navigation stays native. The retired double-Tab drawer has no shortcut.
