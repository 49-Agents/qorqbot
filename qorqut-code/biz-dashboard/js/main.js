// ---------------------------------------------------------------- init
// Stage 2 PR6: push replaces the 12s poll for both -- refreshSide's own
// `!state.biz`/`document.hidden` guard (js/side.js) and rtRefresh's own
// `rtGlobal()`/`rtVisible()` check make a global (every-business) subscription
// safe to over-fire; each still keeps a 60s safety poll (section 2.3: a
// missed event delays a view by at most 60s, never breaks correctness).
if (agentWorkspaceEmbedded()) {
  window.agentWorkspaceReady = agentWorkspaceBootEmbedded();
} else {
agentWorkspaceMenuMount();
onChange(['action_logs', 'signoffs', 'jobs_routines'], null, refreshSide);
setInterval(refreshSide, 60000);
onChange(['jobs_routines'], null, () => { if (rtGlobal() && rtVisible()) rtRefresh(); });
setInterval(() => { if (rtGlobal() && rtVisible()) rtRefresh(); }, 60000);
loadBiz();
replyNotifications.start();
connectEvents();
// The daily briefing asks the server after the shared owner session lands; a finishing report job is a change it cares about.
onChange(['jobs_routines'], null, () => drbCheck('change'));
drbStart();
}

// ---------------------------------------------------------- tab-chord shortcuts
// Hold Tab as a modifier: Tab+letter jumps around the app; a double-tap of Tab (no letter
// between) toggles the right pane.
(function doubleTabPane() {
  let lastTap = null;
  document.addEventListener('keydown', event => {
    if (event.key !== 'Tab' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || event.isComposing || document.querySelector('.overlay')) { lastTap = null; return; }
    if (event.repeat) return;
    const now = Date.now();
    if (lastTap !== null && now - lastTap < 400) {
      lastTap = null; event.preventDefault(); event.stopImmediatePropagation();
      openTasks(!tPanelOpen());
    } else lastTap = now;
  }, true);
  document.addEventListener('pointerdown', () => { lastTap = null; }, true);
  window.addEventListener('blur', () => { lastTap = null; });
})();

(function tabChords() {
  let tabHeld = false;

  const blocked = () => {
    const a = document.activeElement;
    if (a && a.closest && a.closest(".sg, .ac, #taskspanel .ttabs, #thandle")) return true;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
    if (a && a.isContentEditable) return true;
    if (document.querySelector(".overlay") || document.querySelector(".menu")) return true;
    if (document.querySelector(".cmenu")) return true;
    return false;
  };

  document.addEventListener("keydown", e => {
    if (state.scope === "global") { tabHeld = false; return; }
    if (blocked()) { tabHeld = false; return; }
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey) {
      e.preventDefault();
      if (e.repeat) return;   // holding Tab down auto-repeats keydown; only the initial press counts
      tabHeld = true;
      return;
    }
    if (!tabHeld) return;
    if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    const k = e.key.toLowerCase();
    if (k === "t") { e.preventDefault(); openTasks(true); tSetView("tasks"); }
    else if (k === "n") { e.preventDefault(); openTasks(true); tSetView("notes"); }
    else if (k === "u") { e.preventDefault(); openTasks(true); tSetView("uploads"); }
    else if (k === "r") { e.preventDefault(); openTasks(true); tSetView("routines"); }
    else if (k === "m") { e.preventDefault(); goTab("marketing"); }
    else if (k === "c") { e.preventDefault(); goTab("context"); }
    // Agents are global: Tab+A opens the Global Agents page.
    else if (k === "a") { e.preventDefault(); selectGlobal("agents"); }
  });

  document.addEventListener("keyup", e => {
    if (e.key === "Tab") tabHeld = false;
  });
})();
