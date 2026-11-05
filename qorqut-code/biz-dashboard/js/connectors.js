// Connectors: the tools the owner connects so agents can use them (browser, Playwright, Gmail and more).
// Claude agents and Codex agents reach online services differently, so every connector shows a Claude column and a
// Codex column. `connectorsModel` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`);
// `connectorsPanel` renders the Settings panel. Status comes from /root/connectors (bazarlyk.connectors).
const connectorsModel = (() => {
  const CLAUDE_CONNECTORS_URL = 'https://claude.ai/settings/connectors';
  const CHROME_GUIDE_URL = 'https://code.claude.com/docs/en/chrome';
  const icon = path => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  const SERVICE = icon('<path d="M9 7V3.5M15 7V3.5"/><rect x="6" y="7" width="12" height="6" rx="2"/><path d="M12 13v3.5a3.5 3.5 0 0 1-3.5 3.5H7"/>');
  const ICONS = {
    browser: icon('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>'),
    chrome: icon('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/><path d="M9.5 14.5l2 2 3.5-4"/>'),
    playwright: icon('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/><path d="M10 12.5l4 2.5-4 2.5z"/>'),
    gmail: icon('<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M4 7l8 6 8-6"/>'),
    google_calendar: icon('<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>'),
    google_drive: icon('<path d="M4 18.5h16M8.5 4.5h7L21 14l-2.5 4.5M8.5 4.5L3 14l2.5 4.5M8.5 4.5L14 14H3"/>'),
    whatsapp: icon('<path d="M20 11.6a8 8 0 0 1-11.8 7L4 20l1.4-4.1A8 8 0 1 1 20 11.6z"/>'),
    notion: icon('<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>'),
    slack: icon('<path d="M9 3.5v17M15 3.5v17M3.5 9h17M3.5 15h17"/>'),
    linear: icon('<circle cx="12" cy="12" r="8.5"/><path d="M6 15.5l2.5 2.5M5 11l8 8M7.5 6.5l10 10"/>'),
    atlassian: icon('<path d="M8 11.5L3.5 20h7l1.5-3M12 4c-2 3.2-2.3 6.6 0 10.5L15 20h5.5L13 4.5"/>'),
    sentry: icon('<path d="M12 4l9 15.5h-4.5A8.5 8.5 0 0 0 9 12"/><path d="M12 4L3 19.5h3.5A4.5 4.5 0 0 1 11 15"/>'),
    asana: icon('<circle cx="12" cy="7.5" r="3.5"/><circle cx="6.5" cy="15.5" r="3.5"/><circle cx="17.5" cy="15.5" r="3.5"/>'),
    hubspot: icon('<circle cx="15.5" cy="14" r="4"/><path d="M15.5 10V5.5M15.5 5.5h.01M12.5 16.8L5 21M5.5 6.5l7.3 4.8"/>'),
    figma: icon('<path d="M9 3.5h6a3 3 0 0 1 0 6H9a3 3 0 0 1 0-6zM9 9.5h3v6H9a3 3 0 0 1 0-6zM9 15.5h3v2a3 3 0 1 1-3-2z"/><circle cx="15" cy="12.5" r="3"/>'),
    canva: icon('<circle cx="12" cy="12" r="8.5"/><path d="M15 9.5a3.5 3.5 0 1 0 0 5"/>'),
    stripe: icon('<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3 10h18M7 15h4"/>'),
  };
  // Owner-facing copy. `does` is what agents can do with it, `why` why it helps, `privacy` what it can see and where it runs.
  const service = (name, does, why) => ({name, does, why, privacy: `You sign in on ${name}’s own page. Qorqbot never sees your password and keeps no copy of your data.`});
  const COPY = {
    browser: {name: 'Helper browser', does: 'Helper agents Ordubashy hands work to get a separate, signed-out browser of their own for web tasks.',
      why: 'Lets background helpers research and fill in forms without touching your own browser.',
      privacy: 'Runs on this computer in a separate browser profile. Nothing you are signed in to is shared with it.',
      needs: 'Needs Google Chrome and the free agent-browser command.'},
    chrome: {name: 'Your Chrome', does: 'Ordubashy opens and reads sites in your own Chrome, where you are already signed in (LinkedIn, Gmail on the web, your dashboards), only where you allow it.',
      why: 'Most useful sites need your login. This way an agent never needs your password.',
      privacy: 'Runs in your Chrome on this computer. You choose in the extension which sites Claude may use.',
      needs: 'Needs Claude Code on a paid Claude plan and the Claude in Chrome extension.',
      steps: ['Install the Claude in Chrome extension and sign in to it with your Claude account.', 'Press Turn on here.', 'Your agents use it from their next start.'],
      install: 'https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn'},
    playwright: {name: 'Playwright', does: 'Agents drive a separate, clean browser step by step: test your website, repeat a web task, save pages.',
      why: 'The most reliable way to check a site or do the same web task many times.',
      privacy: 'Runs locally through npx with its own browser. Your Chrome profile and passwords are never used.',
      needs: 'Needs Node.js on this computer. Uses Google Chrome when it is installed; otherwise the agent installs Playwright’s own browser on first use.'},
    gmail: {name: 'Gmail', does: 'Ordubashy finds and reads your email, sorts what needs you and drafts replies for you to send.',
      why: 'Most requests arrive by email. This is how your agents keep up with them.',
      privacy: 'You sign in with Google on Google’s own page. Qorqbot never sees your password and keeps no copy of your mail.'},
    google_calendar: {name: 'Google Calendar', does: 'Agents see your schedule, suggest times and add events when you ask.',
      why: 'Planning work around your real day.',
      privacy: 'You sign in with Google on Google’s own page. Qorqbot keeps no copy of your events.'},
    google_drive: {name: 'Google Drive', does: 'Agents find and read your documents and spreadsheets.',
      why: 'Your agents can work from your own files.',
      privacy: 'You sign in with Google on Google’s own page. Qorqbot keeps no copy of your files.'},
    whatsapp: {name: 'WhatsApp', does: 'Ordubashy reads your chats to catch you up. Sending a message always needs your approval.',
      why: 'Catch up on chats without scrolling through them.',
      privacy: 'Runs on this computer and links like WhatsApp on a second device. Your messages stay in a private folder of this installation.'},
    notion: service('Notion', 'Agents read and update your pages and databases.', 'Keep your notes and wikis in the loop.'),
    slack: service('Slack', 'Agents read channels and draft messages.', 'Follow team conversations without living in Slack.'),
    linear: service('Linear', 'Agents find, create and update issues and projects.', 'Turn requests into tracked work without retyping them.'),
    atlassian: service('Atlassian', 'Agents read and update Jira issues and Confluence pages.', 'Keep tickets and team docs current.'),
    sentry: service('Sentry', 'Agents look up errors and what caused them.', 'Find out why something broke without digging through dashboards.'),
    asana: service('Asana', 'Agents read and update tasks and projects.', 'Keep your team’s task list in step with the work.'),
    hubspot: service('HubSpot', 'Agents look up contacts, companies and deals, and log notes.', 'Your agents know who a customer is before they answer.'),
    figma: service('Figma', 'Agents read your designs and comments.', 'Build and check work against the real design.'),
    canva: service('Canva', 'Agents find and create designs.', 'Quick graphics and presentations without leaving the conversation.'),
    stripe: service('Stripe', 'Agents look up customers, payments and invoices.', 'Answer billing questions with real numbers.'),
  };
  const STATUS = {
    connected: {label: 'Connected', tone: 'ok'}, off: {label: 'Off', tone: 'idle'}, not_added: {label: 'Not connected', tone: 'idle'},
    needs_sign_in: {label: 'Needs sign-in', tone: 'warn'}, failed: {label: 'Not working', tone: 'warn'},
    checking: {label: 'Checking…', tone: 'idle'}, unavailable: {label: 'Unavailable', tone: 'warn'},
    unknown: {label: 'Unknown', tone: 'idle'}, not_installed: {label: 'Not installed', tone: 'idle'},
    not_set_up: {label: 'Not set up', tone: 'idle'}, not_paired: {label: 'Not linked', tone: 'warn'},
    unsupported: {label: 'Not on Windows', tone: 'idle'}, needs_extension: {label: 'Needs the Chrome extension', tone: 'warn'}, coming_soon: {label: 'Coming soon on Mac', tone: 'idle'}, needs_install: {label: 'Needs a quick install', tone: 'warn'},
    not_available: {label: 'Not available', tone: 'none'}, waiting: {label: 'Waiting for your approval', tone: 'warn'},
  };
  const status = item => STATUS[item.status] || STATUS.unknown;
  const name = item => COPY[item.id]?.name || item.id;
  // Local, built-in and extension connectors are one thing both clients share; online services differ per client.
  const shared = item => item.kind !== 'claude' && item.id !== 'chrome';
  // The shared control (local toggle): 'toggle' or 'none'. Online services use `cell` instead.
  function action(item) {
    if (item.status === 'unsupported' || item.status === 'coming_soon') return {kind: 'none'};
    if (item.kind === 'local') return {kind: 'toggle', label: ['connected', 'needs_extension'].includes(item.status) ? 'Turn off' : 'Turn on'};
    return {kind: 'none'};
  }
  // One column of one row: {status, action|null, hint|null}. `client` is 'claude' or 'codex'.
  function cell(item, client) {
    const state = item.clients?.[client] || {status: client === 'claude' ? item.status : 'not_available'};
    if (item.kind !== 'claude') {
      if (state.status === 'not_available') return {status: STATUS.not_available, action: null, hint: 'Works with Claude only.'};
      const shown = action({...item, status: state.status});
      return {status: STATUS[state.status] || STATUS.unknown, action: shown.kind === 'none' ? null : shown, hint: null};
    }
    if (client === 'claude') {
      const act = state.status === 'connected' ? {kind: 'guide', label: 'Manage', quiet: true}
        : state.status === 'not_added' ? {kind: 'guide', label: 'Connect'}
        : ['needs_sign_in', 'failed'].includes(state.status) ? {kind: 'guide', label: 'Reconnect'} : null;
      const hint = state.status === 'unavailable' ? ((state.reason || item.reason) === 'claude_missing'
        ? 'Install Claude Code and sign in to use Claude’s connectors.' : 'Claude Code could not list its connectors. Run “claude” once in Terminal to sign in.') : null;
      return {status: STATUS[state.status] || STATUS.unknown, action: act, hint};
    }
    if (state.status === 'not_available') return {status: STATUS.not_available, action: null, hint: `${name(item)} doesn’t offer a Codex connection yet. Use a Claude agent for it.`};
    if (state.sign_in?.running) return {status: STATUS.waiting, action: {kind: 'codex-open', label: 'Open sign-in page', link: state.sign_in.link}, hint: `Approve on ${name(item)}’s page. Qorqbot checks by itself.`};
    if (state.status === 'unavailable') return {status: STATUS.unavailable, action: null,
      hint: state.reason === 'codex_missing' ? 'Install Codex to use it with Codex agents.' : 'Codex could not list its connectors. Run “codex” once in Terminal.'};
    if (state.status === 'off') return {status: STATUS.off, action: null, hint: 'Turned off in your Codex settings.'};
    if (state.status === 'connected') return state.managed ? {status: STATUS.connected, action: {kind: 'codex-disconnect', label: 'Remove', quiet: true}, hint: null}
      : {status: STATUS.connected, action: null, hint: 'Set up in your own Codex settings.'};
    const failed = state.sign_in?.failed || null;
    return {status: STATUS[state.status] || STATUS.unknown, action: {kind: 'codex-connect', label: state.status === 'needs_sign_in' ? 'Sign in' : 'Connect'}, hint: failed};
  }
  // Explains a shared or Claude status: {text, tone}; tone 'warn' means the owner has to act.
  function note(item) {
    const text = noteText(item);
    if (!text) return null;
    return {text, tone: ['not_added', 'not_installed', 'unsupported', 'coming_soon', 'checking'].includes(item.status) ? 'info' : 'warn'};
  }
  function noteText(item) {
    if (item.id === 'chrome' && item.status === 'needs_extension') return 'On, but the Claude in Chrome extension isn’t installed in your Chrome yet. Install it from the Chrome Web Store and sign in to it with your Claude account.';
    if (item.id === 'chrome' && item.status === 'off' && item.extension === false) return 'Also needs the Claude in Chrome extension in your Chrome.';
    if (item.id === 'chrome' && item.status === 'unsupported') return 'Claude in Chrome can’t reach Windows Chrome from Qorqbot’s Linux side (WSL). Use Playwright instead.';
    if (item.kind === 'claude' && item.clients?.claude?.status === 'failed') return 'Claude could not reach it. Press Reconnect and connect it again on claude.ai.';
    if (item.id === 'browser' && item.status === 'needs_install') {
      const chrome = item.missing?.includes('chrome'), tool = item.missing?.includes('agent_browser');
      return chrome && tool ? 'Install Google Chrome, then run the command below in Terminal.'
        : chrome ? 'Install Google Chrome from google.com/chrome.' : 'Run the command below in Terminal.';
    }
    if (item.id === 'whatsapp' && item.status === 'coming_soon') return 'The WhatsApp extension is available for Windows (WSL) and Linux. A Mac version is in progress.';
    if (item.id === 'whatsapp' && item.status === 'not_installed') return 'Comes with the WhatsApp extension, which isn’t installed in this Qorqbot yet.';
    if (item.id === 'whatsapp' && item.status === 'not_set_up') return 'The WhatsApp extension is installed. Link your phone to finish.';
    if (item.id === 'whatsapp' && item.status === 'not_paired') return 'Link your phone: in WhatsApp open Settings, Linked devices, Link a device.';
    return null;
  }
  function summary(items) {
    const recommended = items.filter(item => item.tier === 'recommended');
    const done = recommended.filter(item => item.status === 'connected').length;
    return {done, total: recommended.length, complete: done === recommended.length};
  }
  // Which Claude account agents use, so the owner connects Gmail and others on that same account.
  function account(view) {
    const value = view?.claude_account;
    if (!value) return null;
    if (!value.signed_in) return {tone: 'warn', text: 'Claude Code isn’t signed in. Open Terminal, run “claude” and sign in, then press Check again.'};
    if (value.method !== 'claude.ai') return {tone: 'warn', text: 'Claude Code is signed in with an API key. Claude’s connectors need a Claude account sign-in.'};
    return {tone: 'ok', text: `Your Claude agents use the Claude account ${value.email || 'you signed in with'}. Connect services on claude.ai while signed in to that same account.`};
  }
  // The guided Claude connect: steps with the current one marked. `phase` is 'opened' (claude.ai is open) or
  // 'checking' (the owner came back and Qorqbot is checking).
  function guideSteps(serviceName, phase, reconnect = false) {
    const steps = ['Claude’s connectors page is open in a new tab.',
      reconnect ? `Find ${serviceName} and press Connect again (or Disconnect, then Connect).` : `Find ${serviceName} in the list (use Browse connectors if it isn’t there) and press Connect.`,
      `Sign in on ${serviceName}’s own page and allow access.`,
      'Come back here. Qorqbot checks and confirms by itself.'];
    const current = phase === 'checking' ? 3 : 1;
    return steps.map((text, index) => ({text, state: index < current ? 'done' : index === current ? 'current' : 'todo'}));
  }
  // What an agent of one client can use: connected names, plus what the other client has and this one lacks.
  function agentTools(view, client) {
    if (!view?.connectors || !['claude', 'codex'].includes(client)) return null;
    const other = client === 'claude' ? 'codex' : 'claude';
    const usable = item => item.clients?.[client]?.status === 'connected';
    const can = view.connectors.filter(usable).map(name);
    if (client === 'claude') can.push(...(view.account_connectors || []).filter(extra => extra.status === 'connected').map(extra => extra.name));
    const lacks = view.connectors.filter(item => !usable(item) && item.clients?.[other]?.status === 'connected').map(name);
    if (client === 'codex') lacks.push(...(view.account_connectors || []).filter(extra => extra.status === 'connected').map(extra => extra.name));
    return {can, lacks};
  }
  return {COPY, ICONS, SERVICE, STATUS, CLAUDE_CONNECTORS_URL, CHROME_GUIDE_URL, status, name, shared, action, cell, note,
    summary, account, guideSteps, agentTools};
})();
if (typeof module !== 'undefined') module.exports = connectorsModel;

// The last connectors view any panel loaded (or `connectorsLatest.load()` fetched), for agent cards.
const connectorsLatest = (() => {
  let view = null, pending = null, refused = false;
  return {
    get view() { return view; },
    set(next) { view = next; document.dispatchEvent(new CustomEvent('connectors-updated')); },
    // Owner-only route; anyone else gets no chips, and it is asked once per page load.
    load() {
      if (view || pending || refused || typeof api !== 'function') return pending;
      pending = api('/root/connectors').then(data => { this.set(coreData(data)); })
        .catch(() => { refused = true; }).finally(() => { pending = null; });
      return pending;
    },
  };
})();

// Chips for an agent card: which connectors an agent of this runtime ('claude-code' or 'codex') can use.
function agentToolsHTML(runtime) {
  const client = runtime === 'claude-code' ? 'claude' : runtime === 'codex' ? 'codex' : null;
  const tools = client && connectorsModel.agentTools(connectorsLatest.view, client);
  if (!tools) { if (client) connectorsLatest.load(); return ''; }
  const shown = tools.can.slice(0, 4).map(tool => `<span class="agent-tool">${esc(tool)}</span>`).join('');
  const more = tools.can.length > 4 ? `<span class="agent-tool agent-tool-more" title="${esc(tools.can.slice(4).join(', '))}">+${tools.can.length - 4}</span>` : '';
  const lacks = tools.lacks.length ? `<span class="agent-tool agent-tool-missing" title="${esc(`${client === 'codex' ? 'Codex' : 'Claude'} agents can’t use: ${tools.lacks.join(', ')}`)}">No ${esc(tools.lacks.slice(0, 2).join(', '))}${tools.lacks.length > 2 ? ` +${tools.lacks.length - 2}` : ''}</span>` : '';
  return `<span class="agent-tools" aria-label="Connectors this agent can use">${shown || '<span class="agent-tool agent-tool-none">No connectors</span>'}${more}${lacks}</span>`;
}

// Agent cards drawn before the connectors loaded get their chips once they arrive.
if (typeof document !== 'undefined') document.addEventListener('connectors-updated', () => {
  document.querySelectorAll('[data-agent-tools]').forEach(slot => { slot.innerHTML = agentToolsHTML(slot.dataset.agentTools); });
});

const connectorsPanel = (() => {
  const POLL_MS = 3000, GUIDE_POLL_MS = 6000;
  const PRIVACY = `<section class="cx-privacy" aria-labelledby="cx-privacy-title">
      <div class="cx-privacy-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6"/></svg></div>
      <div><h2 id="cx-privacy-title">Private by design. Qorqbot collects nothing.</h2>
      <p>No Qorqbot account, no tracking, no Qorqbot servers. Qorqbot runs on this computer and everything it keeps stays here.</p>
      <ul><li>Your sign-ins stay with Google, Claude and the services you connect. Qorqbot never sees or stores your passwords.</li>
      <li>Every connector is optional, and you can turn it off at any time.</li>
      <li>What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.</li></ul></div>
    </section>`;
  const button = (act, id, client) => act ? `<button type="button" class="${act.quiet || act.label === 'Turn off' ? 'quiet' : ''}" data-cx-action="${esc(act.kind)}" data-cx-id="${esc(id)}" data-cx-client="${client}">${esc(act.label)}</button>` : '';
  function column(item, client) {
    const c = connectorsModel.cell(item, client);
    return `<div class="cx-cell" data-cx-col="${client}" data-tone="${c.status.tone}">
      <span class="cx-col-label" aria-hidden="true">${client === 'claude' ? 'Claude' : 'Codex'}</span>
      <span class="cx-status" data-tone="${c.status.tone}">${esc(c.status.label)}</span>
      ${button(c.action, item.id, client)}${c.hint ? `<span class="cx-hint">${esc(c.hint)}</span>` : ''}</div>`;
  }
  function guide(item, phase) {
    const reconnect = ['needs_sign_in', 'failed'].includes(item.clients?.claude?.status);
    const steps = connectorsModel.guideSteps(connectorsModel.name(item), phase, reconnect);
    return `<div class="cx-guide" role="status" aria-live="polite">
      <div class="cx-guide-head"><b>Connecting ${esc(connectorsModel.name(item))} for your Claude agents</b><button type="button" class="cx-link" data-cx-guide-cancel>Cancel</button></div>
      <ol>${steps.map(step => `<li data-step="${step.state}">${esc(step.text)}</li>`).join('')}</ol>
      <p class="muted small">Not seeing claude.ai? <a href="${connectorsModel.CLAUDE_CONNECTORS_URL}" target="_blank" rel="noopener noreferrer">Open Claude’s connectors again</a>. Codex agents don’t get Claude’s connectors; use the Codex column for those.</p></div>`;
  }
  function row(item, guiding) {
    const copy = connectorsModel.COPY[item.id] || {name: item.id, does: ''};
    const status = connectorsModel.status(item), note = connectorsModel.note(item);
    const steps = copy.steps ? `<ol>${copy.steps.map(step => `<li>${esc(step)}</li>`).join('')}</ol>` : '';
    const chromeGuide = item.id === 'chrome' ? `<a href="${connectorsModel.CHROME_GUIDE_URL}" target="_blank" rel="noopener noreferrer">How to install Claude in Chrome</a>` : '';
    const search = [copy.name, copy.does, copy.why, status.label, item.id.replace('_', ' '), item.clients?.codex?.status === 'not_available' ? 'claude only' : 'codex'].join(' ').toLowerCase();
    const cells = connectorsModel.shared(item)
      ? `<div class="cx-cell cx-cell-both" data-tone="${status.tone}"><span class="cx-col-label" aria-hidden="true">Claude and Codex</span><span class="cx-status" data-tone="${status.tone}">${esc(status.label)}</span>${button(connectorsModel.cell(item, 'claude').action, item.id, 'claude')}<span class="cx-hint">Same for both</span></div>`
      : column(item, 'claude') + column(item, 'codex');
    return `<div class="cx-row" data-cx-card="${esc(item.id)}" data-tone="${status.tone}" data-search="${esc(search)}" ${guiding ? 'data-cx-guiding' : ''}>
      <span class="cx-icon">${connectorsModel.ICONS[item.id] || connectorsModel.SERVICE}</span>
      <div class="cx-row-main">
        <div class="cx-row-title"><h3>${esc(copy.name)}</h3></div>
        <p class="cx-row-does">${esc(copy.does)}</p>
        ${note ? `<p class="cx-note" data-tone="${note.tone}">${esc(note.text)}</p>` : ''}
        ${item.id === 'chrome' && item.extension === false && copy.install ? `<a class="cx-get" href="${esc(copy.install)}" target="_blank" rel="noopener noreferrer">Get Claude in Chrome</a>` : ''}
        ${item.install && item.missing?.includes('agent_browser') ? `<div class="cx-command"><code>${esc(item.install)}</code><button type="button" class="quiet" data-cx-copy="${esc(item.install)}">Copy</button></div>` : ''}
        <details class="cx-more"><summary>What it can see</summary>
          <p><b>Why:</b> ${esc(copy.why || '')}</p><p><b>Privacy:</b> ${esc(copy.privacy || '')}</p>
          ${copy.needs ? `<p><b>Needs:</b> ${esc(copy.needs)}</p>` : ''}${steps}${chromeGuide}</details>
      </div>
      ${cells}
      ${guiding ? guide(item, guiding) : ''}
    </div>`;
  }
  function extras(view) {
    const list = view.account_connectors || [];
    if (!list.length) return '';
    return `<h3 class="cx-group">Also on your Claude account</h3>
      <p class="cx-sub muted small">Every Claude agent can use these. Codex agents can’t. Manage them on claude.ai.</p>
      <div class="cx-extras">${list.map(extra => {
        const status = connectorsModel.STATUS[extra.status] || connectorsModel.STATUS.unknown;
        return `<span class="cx-extra" data-search="${esc((extra.name + ' claude account').toLowerCase())}" data-tone="${status.tone}"><span class="cx-status" data-tone="${status.tone}"></span>${esc(extra.name)}${extra.status === 'connected' ? '' : `<em>${esc(status.label)}</em>`}</span>`;
      }).join('')}</div>`;
  }
  // `options.onChange(view)` is called after every read; `options.compact` drops the privacy banner.
  function mount(container, options = {}) {
    const root = document.createElement('div');
    root.className = 'cx-panel cx-panel-rows';
    root.innerHTML = `${options.compact ? '' : PRIVACY}
      <div class="cx-head"><div><p>Give your agents the tools they need. Your Chrome, Playwright and Gmail are strongly recommended; everything is optional.</p></div>
        <div class="cx-progress" data-cx-progress aria-live="polite"></div></div>
      <div class="cx-clients">
        <div><b>Claude agents</b> use the connectors of your claude.ai account. You connect them once on claude.ai and every Claude agent has them.</div>
        <div><b>Codex agents</b> can’t use Claude’s connectors. Qorqbot adds services to your Codex here, where the service offers it.</div>
      </div>
      <p class="cx-error" data-cx-error role="alert" hidden></p>
      <p class="cx-account" data-cx-account hidden></p>
      <div class="cx-thead" aria-hidden="true"><span></span><span>Connector</span><span>Claude agents</span><span>Codex agents</span></div>
      <h3 class="cx-group">Strongly recommended</h3><div class="cx-rows" data-cx-tier="recommended"><p class="muted small">Loading connectors…</p></div>
      <h3 class="cx-group">Also useful</h3><div class="cx-rows" data-cx-tier="suggested"></div>
      <h3 class="cx-group">More services</h3><div class="cx-rows" data-cx-tier="more"></div>
      <div data-cx-extras></div>
      <p class="cx-foot muted small">Local connectors reach an agent the next time it starts; online services are available right away. <button type="button" class="cx-link" data-cx-refresh>Check again</button></p>`;
    container.appendChild(root);
    let view = null, timer = 0, busy = false, guiding = null;  // guiding: {id, phase}
    const live = () => root.isConnected;
    const error = text => { const el = root.querySelector('[data-cx-error]'); el.textContent = text || ''; el.hidden = !text; };
    const find = id => view?.connectors.find(entry => entry.id === id);
    const waitingCodex = () => view?.connectors.some(item => item.clients?.codex?.sign_in?.running);
    function paint() {
      if (!view) return;
      for (const tier of ['recommended', 'suggested', 'more'])
        root.querySelector(`[data-cx-tier="${tier}"]`).innerHTML = view.connectors.filter(item => item.tier === tier)
          .map(item => row(item, guiding?.id === item.id ? guiding.phase : null)).join('');
      root.querySelector('[data-cx-extras]').innerHTML = extras(view);
      const progress = connectorsModel.summary(view.connectors);
      root.querySelector('[data-cx-progress]').innerHTML = `<span class="cx-progress-text">${progress.done} of ${progress.total} recommended connected</span>
        <span class="cx-progress-bar" aria-hidden="true"><i style="width:${Math.round(progress.done / progress.total * 100)}%"></i></span>`;
      const signIn = connectorsModel.account(view), line = root.querySelector('[data-cx-account]');
      line.hidden = !signIn; if (signIn) { line.textContent = signIn.text; line.dataset.tone = signIn.tone; }
      const refresh = root.querySelector('[data-cx-refresh]');
      refresh.disabled = view.checking; refresh.textContent = view.checking ? 'Checking…' : 'Check again';
      options.onChange?.(view);
      root.dispatchEvent(new CustomEvent('connectors-painted', {bubbles: true}));
    }
    function schedule() {
      clearTimeout(timer);
      if (!live()) return;
      if (guiding) timer = setTimeout(() => load({refresh: true}), GUIDE_POLL_MS);
      else if (view?.checking || waitingCodex()) timer = setTimeout(() => load(), POLL_MS);
    }
    // Ends the guided connect once Claude reports the service connected.
    function settle(before) {
      if (!guiding) return;
      const item = find(guiding.id);
      if (item?.clients?.claude?.status === 'connected' && before !== 'connected') {
        toast(`${connectorsModel.name(item)} is connected for your Claude agents.`);
        guiding = null; paint();
      }
    }
    function codexDone(before) {
      for (const item of view?.connectors || []) {
        const was = before[item.id], now = item.clients?.codex?.status;
        if (was && was !== 'connected' && now === 'connected') toast(`${connectorsModel.name(item)} is connected for your Codex agents.`);
      }
    }
    async function load(change) {
      if (!live()) return null;
      const before = guiding ? find(guiding.id)?.clients?.claude?.status : null;
      const codexBefore = Object.fromEntries((view?.connectors || []).map(item => [item.id, item.clients?.codex?.status]));
      let data = null;
      try {
        data = coreData(change ? await post('/root/connectors', change) : await api('/root/connectors'));
        view = data;
        connectorsLatest.set(view);
        if (!live()) return data;
        error(''); paint(); settle(before); codexDone(codexBefore);
      } catch (failure) { if (live()) error(failure.message || 'Connectors could not be loaded.'); }
      schedule();
      return data;
    }
    function startGuide(item) {
      window.open(connectorsModel.CLAUDE_CONNECTORS_URL, '_blank', 'noopener,noreferrer');
      guiding = {id: item.id, phase: 'opened'};
      paint(); schedule();
      root.querySelector('[data-cx-guiding]')?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    root.addEventListener('click', async event => {
      if (event.target.closest('[data-cx-refresh]')) { await load({refresh: true}); return; }
      if (event.target.closest('[data-cx-guide-cancel]')) { guiding = null; paint(); schedule(); return; }
      const copy = event.target.closest('[data-cx-copy]');
      if (copy) {
        try { await navigator.clipboard.writeText(copy.dataset.cxCopy); copy.textContent = 'Copied'; }
        catch (_) { copy.textContent = 'Select and copy'; }
        setTimeout(() => { if (copy.isConnected) copy.textContent = 'Copy'; }, 2000);
        return;
      }
      const target = event.target.closest('[data-cx-action]');
      if (!target || busy) return;
      const item = find(target.dataset.cxId);
      if (!item) return;
      const kind = target.dataset.cxAction;
      if (kind === 'guide') { startGuide(item); return; }
      if (kind === 'codex-open') {
        const link = item.clients?.codex?.sign_in?.link;
        if (link) window.open(link, '_blank', 'noopener,noreferrer');
        return;
      }
      busy = true; target.disabled = true;
      try {
        if (kind === 'codex-connect' || kind === 'codex-disconnect') {
          if (kind === 'codex-disconnect' && !await new Promise(resolve => modal({
            title: esc(`Remove ${connectorsModel.name(item)} from Codex?`), onClose: () => resolve(false),
            body: '<p>Codex agents, and your own Codex, lose access until you connect it again.</p>',
            actions: [{label: 'Cancel', onClick: close => close()}, {label: 'Remove', primary: true, onClick: close => { resolve(true); close(); }}]}))) return;
          const data = await load({id: item.id, client: 'codex', action: kind === 'codex-connect' ? 'connect' : 'disconnect'});
          if (data?.sign_in?.link) window.open(data.sign_in.link, '_blank', 'noopener,noreferrer');
          return;
        }
        // On covers "needs the extension" too: its button reads Turn off.
        const enabling = !['connected', 'needs_extension'].includes(item.status);
        await load({id: item.id, enabled: enabling});
        if (live() && !root.querySelector('[data-cx-error]').textContent)
          toast(`${connectorsModel.name(item)} ${enabling ? 'is on' : 'is off'}. Your agents get the change the next time they start.`);
      } finally { busy = false; if (target.isConnected) target.disabled = false; }
    });
    // Returning from claude.ai: mark the guide as checking and check at once.
    const focus = () => {
      if (!guiding || !live() || document.visibilityState !== 'visible') return;
      if (guiding.phase !== 'checking') { guiding = {...guiding, phase: 'checking'}; paint(); }
      load({refresh: true});
    };
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', focus);
    const watch = new MutationObserver(() => {
      if (live()) return;
      clearTimeout(timer); watch.disconnect();
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus);
    });
    watch.observe(container.ownerDocument.body, {childList: true, subtree: true});
    load();
    return {root, reload: () => load({refresh: true}), get view() { return view; }};
  }
  return {mount, PRIVACY};
})();
