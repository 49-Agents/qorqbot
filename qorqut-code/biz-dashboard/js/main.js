// ---------------------------------------------------------------- init
setInterval(refreshSide, 12000);
loadBiz();

// ---------------------------------------------------------- tab-chord shortcuts
// Hold Tab as a modifier: Tab+letter jumps around the app; a double-tap of Tab (no letter
// between) steps through agents when the Agents tab is showing.
(function tabChords() {
  let tabHeld = false, lastTabAt = 0, sawLetter = false;

  const blocked = () => {
    const a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
    if (a && a.isContentEditable) return true;
    if (document.querySelector(".overlay") || document.querySelector(".menu")) return true;
    if (document.querySelector(".cmenu")) return true;
    return false;
  };

  document.addEventListener("keydown", e => {
    if (blocked()) return;
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey) {
      e.preventDefault();
      if (e.repeat) return;   // holding Tab down auto-repeats keydown; only the initial press counts
      const now = Date.now();
      if (tabHeld === false && now - lastTabAt < 400 && !sawLetter) {
        // double-tap: step through agents in nav order
        if (state.tab === "agents" && typeof agentNavOrder === "function") {
          const order = agentNavOrder();
          if (order.length) {
            const i = order.indexOf(state.agent);
            state.agent = order[(i + 1 + order.length) % order.length];
            if (typeof renderAgents === "function") renderAgents($("#content"));
          }
        }
      }
      tabHeld = true; lastTabAt = now; sawLetter = false;
      return;
    }
    if (!tabHeld) return;
    if (e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    const k = e.key.toLowerCase();
    if (k === "t") { e.preventDefault(); sawLetter = true; openTasks(true); tSetView("tasks"); }
    else if (k === "j") { e.preventDefault(); sawLetter = true; openTasks(true); tSetView("journal"); }
    else if (k === "n") { e.preventDefault(); sawLetter = true; openTasks(true); tSetView("notes"); }
    else if (k === "m") { e.preventDefault(); sawLetter = true; goTab("marketing"); }
    else if (k === "c") { e.preventDefault(); sawLetter = true; goTab("context"); }
    else if (k === "a") { e.preventDefault(); sawLetter = true; goTab("agents"); }
  });

  document.addEventListener("keyup", e => {
    if (e.key === "Tab") tabHeld = false;
  });
})();
