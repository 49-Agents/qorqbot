// Starting an agent from Chat: the owner's one-time trust consent and the Start button. Nothing starts by itself.
// `agentStartPlan` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`); the rest calls the dashboard API.
// A start is the existing owner-authorized `agent_terminal_start` operation, dispatched by the service's terminal controller.
const agentStartPlan = (() => {
  // Offline reasons Chat cannot fix by starting a terminal: another conversation is being connected, or the agent is unsupported.
  const NOT_STARTABLE = new Set(['capture_disabled', 'unsupported_runtime', 'native_only', 'other_conversation',
    'selection_required', 'registering']);
  const STARTING_FOR_MS = 90000;
  // `session` is Chat's session view; `trust` the /root/agent-trust view (or null when unread); `local` this Chat's own state:
  // {startedAt, error, now}. Returns what Chat shows while the agent is offline.
  function plan(session, trust, local = {}) {
    if (!session || session.state === 'live' || NOT_STARTABLE.has(session.reason)) return {kind: 'none'};
    const now = local.now ?? Date.now();
    if (local.startedAt && now - local.startedAt < STARTING_FOR_MS) return {kind: 'starting'};
    if (local.error) return {kind: 'failed', error: local.error};
    if (local.startedAt) return {kind: 'failed', error: 'The agent did not come online. Open the terminal to see what it is waiting for.'};
    return {kind: 'start', needsConsent: !trust?.granted};
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
        title: 'Trust Qorqbot’s agent folders',
        width: 520,
        body: `<p>Claude Code and Codex ask before they work in a new folder. Allow this once, and they can open straight into
          the session in the folders Qorqbot prepares for its agents${agentId ? `, starting with ${esc(label)}` : ''}.</p>
          <p>Qorqbot will:</p>
          <ul><li>mark each agent’s own folder as trusted in Claude Code and Codex</li>
          <li>run the hooks Qorqbot installs in an agent’s own folder without asking Codex to review them each time</li></ul>
          <p class="muted">Nothing else is trusted. Claude Code or Codex can still ask you about a larger folder around an agent,
          such as the git repository its folder sits in: Qorqbot does not answer that for you, and you decide in the terminal.
          Your other projects are not changed, and claude and codex run as usual anywhere outside an agent’s own folder.
          Nothing starts by itself. You can turn this off in Settings.</p>
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
