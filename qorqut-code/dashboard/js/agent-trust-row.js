// Settings rows for agents that run in terminals (js/agent-mode.js says so): "Starting an agent" and "Trusted agent folders".
// Nothing is drawn and nothing is requested while agents run as managed sessions. A server that does not have
// GET/POST /root/agent-trust (404 or 405) hides both rows. The consent dialog is js/agent-start.js.
const agentTrustRow = (() => {
  const ROUTE = '/root/agent-trust';
  const START = 'Open the agent, switch to its Terminal with the terminal icon above Chat, and run claude there. An agent that is not running receives nothing until it is started.';
  const ON = 'On. Each agent’s own folder is marked as trusted in Claude Code, so it opens straight into the session. Claude Code can still ask you to trust a larger folder around an agent, such as the git repository it sits in; you decide that in the terminal.';
  const OFF = 'Off. The first time an agent starts in its folder, Claude Code asks whether to trust it.';
  const UNREADABLE = 'Could not read this setting.';

  const defaultRead = async () => coreData(await api(ROUTE));
  const defaultRevoke = async () => coreData(await post(ROUTE, {granted: false}));
  const defaultConsent = () => agentStart.consent();
  const routeMissing = error => !!error && [404, 405].includes(error.httpStatus);
  const active = () => typeof agentMode !== 'undefined' && agentMode.terminals();

  // What the two rows say. `trust` is {granted} from the server, or null; `failed` when the read failed (not a missing route).
  function rows(trust, failed = false) {
    const granted = !!(trust && trust.granted);
    return [
      {id: 'start', label: 'Starting an agent', help: START, keywords: 'start launch terminal claude restart', action: null},
      {id: 'trust', label: 'Trusted agent folders', keywords: 'trust consent hooks prompts folder security',
        help: failed ? UNREADABLE : trust ? (granted ? ON : OFF) : 'Checking…',
        action: failed ? {id: 'retry', label: 'Try again'} : trust ? {id: granted ? 'revoke' : 'grant', label: granted ? 'Turn off' : 'Turn on'} : null},
    ];
  }

  const rowHtml = row => `<div class="st-row" data-atr-row="${esc(row.id)}" data-search="${esc((row.label + ' ' + row.help + ' ' + row.keywords).toLowerCase())}">
    <div class="st-row-text"><div class="st-label">${esc(row.label)}</div><div class="st-help">${esc(row.help)}</div></div>
    <div class="st-control">${row.action ? `<button type="button" class="quiet" data-atr-action="${esc(row.action.id)}">${esc(row.action.label)}</button>` : ''}</div></div>`;

  // Draws the rows into `container` while agents run in terminals, and again when the mode changes under it.
  // Options (all optional): read, revoke, consent, isActive, onChange, toast.
  function mount(container, options = {}) {
    const read = options.read || defaultRead, revoke = options.revoke || defaultRevoke, consent = options.consent || defaultConsent;
    const isActive = options.isActive || active;
    let seq = 0, trust = null, failed = false, missing = false, busy = false, stopped = false;
    const live = () => !stopped && container.isConnected;
    function paint() {
      const show = isActive() && !missing;
      container.style.display = show ? '' : 'none';
      container.innerHTML = show ? rows(trust, failed).map(rowHtml).join('') : '';
      options.onChange?.();
    }
    async function refresh() {
      if (!isActive()) { paint(); return; }
      const mine = ++seq;
      try { const next = await read(); if (mine !== seq || !live()) return; trust = next; failed = false; missing = false; }
      catch (error) {
        if (mine !== seq || !live()) return;
        if (routeMissing(error)) missing = true; else failed = true;
      }
      paint();
    }
    container.addEventListener('click', async event => {
      const button = event.target.closest('[data-atr-action]');
      if (!button || busy) return;
      const id = button.dataset.atrAction;
      if (id === 'retry') { failed = false; trust = null; paint(); void refresh(); return; }
      busy = true; button.disabled = true;
      try { if (id === 'revoke') await revoke(); else await consent(); }
      catch (error) { options.toast?.(error.message); }
      busy = false;
      void refresh();
    });
    const onMode = () => {
      if (!live()) { document.removeEventListener('agent-mode-changed', onMode); return; }
      void refresh();
    };
    document.addEventListener('agent-mode-changed', onMode);
    paint();
    void refresh();
    return {refresh, stop: () => { stopped = true; document.removeEventListener('agent-mode-changed', onMode); }};
  }

  return {rows, mount, START, ON, OFF};
})();
if (typeof module !== 'undefined') module.exports = agentTrustRow;
