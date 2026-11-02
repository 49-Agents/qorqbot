// ---------------------------------------------------------------- init
// Stage 2 PR6: push replaces the 12s poll for both -- refreshSide's own
// `!state.biz`/`document.hidden` guard (js/side.js) and rtRefresh's own
// `rtGlobal()`/`rtVisible()` check make a global (every-business) subscription
// safe to over-fire; each still keeps a 60s safety poll (section 2.3: a
// missed event delays a view by at most 60s, never breaks correctness).
if (agentWorkspaceEmbedded()) {
  window.agentWorkspaceReady = agentWorkspaceBootEmbedded();
} else {
agentWorkspaceShellStart();
onChange(['action_logs', 'signoffs', 'jobs_routines'], null, refreshSide);
setInterval(refreshSide, 60000);
onChange(['jobs_routines'], null, () => { if (rtGlobal() && rtVisible()) rtRefresh(); });
setInterval(() => { if (rtGlobal() && rtVisible()) rtRefresh(); }, 60000);
loadBiz();
replyNotifications.start();
connectEvents();
}

// Tab navigation stays native. The retired double-Tab drawer has no shortcut.
