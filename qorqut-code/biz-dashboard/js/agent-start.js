// Starting an agent from Chat: the owner's one-time trust consent, the Start button and the automatic start of Ordubashy.
// `agentStartPlan` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`); the rest calls the dashboard API.
// A start is the existing owner-authorized `agent_terminal_start` operation, dispatched by the service's terminal controller.
const agentStartPlan = (() => {
  // Offline reasons Chat cannot fix by starting a terminal: another conversation is being connected, or the agent is unsupported.
  const NOT_STARTABLE = new Set(['capture_disabled', 'unsupported_runtime', 'native_only', 'other_conversation',
    'selection_required', 'registering']);
  const STARTING_FOR_MS = 90000;
  // `session` is Chat's session view; `trust` the /root/agent-trust view (or null when unread); `local` this Chat's own state:
  // {startedAt, autoTried, error, isLead, now}. Returns what Chat shows while the agent is offline.
  function plan(session, trust, local = {}) {
    if (!session || session.state === 'live' || NOT_STARTABLE.has(session.reason)) return {kind: 'none'};
    const now = local.now ?? Date.now();
    if (local.startedAt && now - local.startedAt < STARTING_FOR_MS) return {kind: 'starting'};
    if (local.error) return {kind: 'failed', error: local.error};
    if (local.startedAt) return {kind: 'failed', error: 'The agent did not come online. Open the terminal to see what it is waiting for.'};
    const auto = !!(local.isLead && trust?.granted && trust.autostart && !local.autoTried);
    return {kind: 'start', auto, needsConsent: !trust?.granted};
  }
  return {plan, NOT_STARTABLE, STARTING_FOR_MS};
})();
if (typeof module !== 'undefined') module.exports = agentStartPlan;

const agentStart = (() => {
  const url = agentId => agentId ? `/root/agent-trust?agent_id=${encodeURIComponent(agentId)}` : '/root/agent-trust';
  const read = async agentId => coreData(await api(url(agentId)));
  const grant = async agentId => coreData(await post(url(agentId), {granted: true}));
  // The owner's consent, in plain language. Resolves to the new trust view, or null when the owner declines.
  function consent(agentId, label) {
    return new Promise(resolve => {
      let settled = false;
      const finish = value => { if (!settled) { settled = true; resolve(value); } };
      modal({
        title: 'Let Qorqbot start your agents',
        width: 520,
        body: `<p>Qorqbot starts ${esc(label)} for you in Claude Code or Codex, in the folders it prepares for its agents.</p>
          <p>To do that without stopping at prompts, Qorqbot will:</p>
          <ul><li>mark its own agent folders as trusted in Claude Code and Codex</li>
          <li>run the hooks Qorqbot installs in those folders without asking Codex to review them each time</li></ul>
          <p class="muted">Your other projects are not changed. You can turn this off in Settings.</p>
          <div class="ferr" role="alert" hidden></div>`,
        onClose: () => finish(null),
        actions: [
          {label: 'Not now', onClick: close => close()},
          {label: agentId ? 'Allow and start' : 'Allow', primary: true, onClick: async (close, el) => {
            const error = el.querySelector('.ferr');
            try { const view = await grant(agentId); finish(view); close(); }
            catch (failure) { error.textContent = failure.message; error.hidden = false; }
          }},
        ],
      });
    });
  }
  const start = async agentId => coreData(await post('/root/operations/agent_terminal_start', {
    target_agent_id: agentId, rationale: 'The owner started this agent from Chat', idempotency_key: newRequestId()}));
  return {read, consent, start};
})();
