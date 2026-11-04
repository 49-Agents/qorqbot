// Shared agent roster presence: one compact activity read paints the navigation's working dots.
// Polling never opens a terminal or loads task histories; the backend activity read stays unchanged.
// Navigation presence reflects agent activity, not whether the browser's terminal stream is connected.
let agentNavNext = 0, agentNavPending = false;
const agentNavStates = new Map();
const agentNavObservedAt = new Map();
const AGENT_NAV_STALE_MS = 30000;
let agentNavObserved = 0;
function agentNavPaint() {
  document.querySelectorAll('[data-agent-presence]').forEach(dot => {
    // Running is trusted only while a successful observation is recent.
    const seen = agentNavStates.get(dot.dataset.agentPresence) || 'unknown';
    const value = seen === 'running' && Date.now() - (agentNavObservedAt.get(dot.dataset.agentPresence) || 0) > AGENT_NAV_STALE_MS ? 'unknown' : seen;
    dot.dataset.state = value;
    dot.hidden = value !== 'running';
    dot.setAttribute('aria-label', 'Working');
  });
}
async function agentNavRefresh(force = false) {
  const dots = document.querySelectorAll('[data-agent-presence]');
  // Stale running status clears even while a request is stuck or the page is hidden.
  agentNavPaint();
  if (!dots.length || document.hidden || agentNavPending || (!force && Date.now() < agentNavNext)) return;
  agentNavPending = true;
  const rows = [];
  try {
    let cursor;
    do {
      const data = coreData(await api('/me/agents/activity?limit=200'+(cursor?'&cursor='+encodeURIComponent(cursor):'')));
      if (!Array.isArray(data.agents)) throw new Error('Unavailable');
      rows.push(...data.agents); cursor = data.next_cursor;
    } while (cursor);
    agentNavStates.clear(); agentNavObservedAt.clear(); agentNavObserved = Date.now();
    rows.forEach(agent => { agentNavStates.set(agent.agent_id, agent.state); agentNavObservedAt.set(agent.agent_id, agentNavObserved); });
  } catch (error) {
    // A transient failure keeps the last successful presence; lost access clears it.
    if (accessLost(error)) { agentNavStates.clear(); agentNavObservedAt.clear(); }
  } finally {
    agentNavPending = false; agentNavNext = Date.now() + 4000; agentNavPaint();
  }
}
// Activity events refresh immediately; a short visible-page poll covers native
// state transitions that do not create operational records. No per-agent streams.
if (typeof onChange === 'function') onChange(['agents', 'action_logs'], null, () => agentNavRefresh(true));
document.addEventListener('visibilitychange', () => { if (!document.hidden) void agentNavRefresh(true); });
setInterval(agentNavRefresh, 5000);