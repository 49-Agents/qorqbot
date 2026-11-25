// Environment defaults belong to Global; a project saves only its explicit overrides.
let envView = null;
const ENV_KEY_JS = /^[A-Za-z_][A-Za-z0-9_]*$/;
const envScope = () => state.scope === "global" ? "global" : state.project;
const envCurrent = p => envView === p && p.pane.isConnected && envScope() === p.scope;

// Settings: a section list on the left, one section on the right, and search across every setting. Global settings
// have every section; a project has only its environment overrides.
const SETTINGS_SECTIONS = [
  {id: 'connections', title: 'Connections', icon: 'M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0ZM12 17v5'},
  {id: 'agents', title: 'Model/Provider', icon: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6'},
  {id: 'appearance', title: 'Appearance', icon: 'M12 3a9 9 0 1 0 0 18ZM12 3a9 9 0 0 1 0 18'},
  {id: 'notifications', title: 'Notifications', icon: 'M8 15v-3c0-2 1-2 1-5a3 3 0 1 1 6 0c0 3 1 3 1 5v3M5 15h14l2 4H3ZM5 22h14'},
  {id: 'environment', title: 'Environment', icon: 'm8 6-6 6 6 6m8-12 6 6-6 6'},
  {id: 'walkthrough', title: 'Walkthrough', icon: 'M5 3l14 9-14 9Z'},
  {id: 'about', title: 'About & privacy', icon: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20ZM12 16v-5M12 8h.01'},
];
const SETTINGS_SECTION_KEY = 'qorqbot:settings-section';
let settingsShowSection = () => {};

function settingsRow({label, help = '', control = '', keywords = '', attrs = ''}) {
  return `<div class="st-row" data-search="${esc((label + ' ' + help + ' ' + keywords).toLowerCase())}" ${attrs}>
    <div class="st-row-text"><div class="st-label">${label}</div>${help ? `<div class="st-help">${help}</div>` : ''}</div>
    <div class="st-control">${control}</div></div>`;
}
const settingsChoice = (name, options, current) => `<div class="st-choice" role="radiogroup" aria-label="${esc(name)}">${options.map(([value, label]) =>
  `<button type="button" role="radio" aria-checked="${value === current}" data-choice="${esc(name)}" data-value="${esc(value)}">${esc(label)}</button>`).join('')}</div>`;
const settingsSwitch = (name, on, label) => `<label class="set-switch"><input type="checkbox" role="switch" data-switch="${esc(name)}" aria-label="${esc(label)}"${on ? ' checked' : ''}><span class="set-switch-track" aria-hidden="true"></span></label>`;
// One headed group of a section: the record panels' section head, then its rows.
const settingsGroup = (title, inner, search = '') => `<div class="record-section"${search ? ` data-search="${esc(search)}"` : ''}><div class="record-section-head"><h4>${title}</h4></div>${inner}</div>`;

async function renderSettings(c) {
  envView?.testing?.unsubscribe?.();
  const scope = envScope(), global = state.scope === "global";
  const pane = document.createElement("div");
  pane.className = "pane settingspane" + (global ? " st-global" : "");
  const p = envView = { pane, scope, global, url: global ? "/root/env" : "/project/" + encodeURIComponent(state.project) + "/env",
    rows: [], inherited: [], loaded: false, saving: false, dirty: false, testing: null };
  const sections = global ? SETTINGS_SECTIONS : SETTINGS_SECTIONS.filter(section => section.id === 'environment');
  const environment = `
    <p class="st-help">${global ? "Shared defaults for all projects and Global agents. Projects can override individual values." : "Global values are inherited automatically. Override a value for this project, or use Global again."}</p>
    <div class="envrows" id="envrows"></div>
    <button class="cx-link envadd" id="envadd" disabled>+ Add variable</button>
    <span class="ef-actions"><button class="ef-btn ef-save" id="envsave" disabled>Save</button></span>
    <div class="set-note muted small" id="envnote" role="status"></div>
    <p class="st-help">Changes apply to new agent sessions and later jobs. Agents already running keep their current environment.</p>`;
  const look = appearance.get(), alerts = desktopNotify.prefs();
  const body = {
    connections: `<div class="st-block" data-search="connections browser websites extensions openrouter voice input microphone treg whatsapp" data-connectors-slot></div>`,
    agents: `${global ? settingsGroup('Agent sessions', '<div class="st-rows" data-agent-mode></div>') : ''}
      ${settingsGroup('Clients', `<div class="st-rows" data-agent-clients></div>${global ? '<div class="st-rows" data-agent-extras></div>' : ''}`)}
      <div class="record-section" data-search="agents model provider account switch client effort claude codex"><div class="record-section-head"><h4>Client, model and effort</h4></div>
        <p class="st-help">What each agent runs on. Leave a field empty to use the default.</p><div data-agent-options></div></div>
      ${global ? settingsGroup('Experimental', settingsRow({label: 'Testing mode', keywords: 'testing overseer global agent roster chat workspace experimental',
        help: 'Show Overseer in the Global agent roster and enable its chat and workspace. Also shows the Timeline, Journal, Files and Context views in Ordubashy’s pane. Off by default.<span class="set-testing-note" id="set-testing-note" role="status"></span>',
        control: `<label class="set-switch" for="set-testing-toggle"><input type="checkbox" id="set-testing-toggle" role="switch" aria-label="Testing mode" disabled><span class="set-switch-track" aria-hidden="true"></span><span class="set-switch-label" id="set-testing-state">Loading…</span></label>`,
        attrs: 'data-testing-mode'}), 'experimental testing mode') : ''}`,
    appearance: (global ? settingsGroup('Timezone', settingsRow({label: 'Timezone', keywords: 'time zone clock calendar reminders utc offset',
        help: 'The one clock for the whole installation: calendar, Focus, reminders and the times agents are told. <span id="set-timezone-note" role="status"></span>',
        control: `<input class="inp" id="set-timezone" list="set-timezone-list" value="${esc(SJ_ZONE)}" autocomplete="off" spellcheck="false" aria-label="Timezone"><datalist id="set-timezone-list">${ownerTimezoneZones().map(zone => `<option value="${esc(zone)}">${esc(ownerTimezoneOffset(zone))}</option>`).join('')}</datalist>`}), 'timezone clock') : '') + settingsGroup('Display', `${settingsRow({label: 'Theme', keywords: 'dark light system mode color colour', help: 'System follows your computer’s light or dark setting.',
        control: settingsChoice('theme', [['dark', 'Dark'], ['light', 'Light'], ['system', 'System']], look.theme)})}
      ${settingsRow({label: 'Text size', keywords: 'font zoom bigger smaller scale', help: 'Scales the whole dashboard.',
        control: settingsChoice('textSize', [['small', 'Small'], ['default', 'Default'], ['large', 'Large']], look.textSize)})}
      <p class="st-help">Saved for this browser only.</p>`),
    notifications: settingsGroup('Desktop alerts', `${settingsRow({label: 'Agent replies', keywords: 'notify alert message reply desktop',
        help: 'When an agent answers you while the dashboard is in the background.', control: settingsSwitch('replies', alerts.replies, 'Desktop notifications for agent replies')})}
      ${settingsRow({label: 'Signoffs waiting on you', keywords: 'notify alert approval signoff decision desktop',
        help: 'When an approval or decision needs you.', control: settingsSwitch('signoffs', alerts.signoffs, 'Desktop notifications for Signoffs')})}
      ${settingsRow({label: 'Reminders', keywords: 'notify alert reminder due desktop',
        help: 'When one of your reminders is due.', control: settingsSwitch('reminders', alerts.reminders, 'Desktop notifications for due reminders')})}
      ${settingsRow({label: 'Failed routines', keywords: 'notify alert routine failed failure error desktop',
        help: 'When a routine run fails.', control: settingsSwitch('routines', alerts.routines, 'Desktop notifications for failed routines')})}
      ${settingsRow({label: 'Test', keywords: 'notification permission test', help: '<span data-notify-status></span>',
        control: '<button type="button" class="quiet" data-notify-test>Send a test</button>'})}`),
    environment: settingsGroup('Environment variables', environment, 'environment variables api keys env secrets token'),
    walkthrough: settingsGroup('Walkthrough', `${settingsRow({label: 'Take the walkthrough again', keywords: 'tour onboarding help guide tutorial',
        help: 'A short, interactive tour of your workspace.', control: '<button type="button" class="quiet" data-onboarding-replay>Start</button>'})}
      ${settingsRow({label: 'One chapter', keywords: 'tour chapter onboarding', help: 'Replay just one part.',
        control: `<div class="st-chapters">${onboarding.chapters().map(chapter => `<button type="button" class="quiet" data-onboarding-chapter="${esc(chapter.id)}">${esc(chapter.title)}</button>`).join('')}</div>`})}`),
    about: settingsGroup('About', '<div class="st-rows" data-about><p class="st-help">Loading…</p></div>') +
      settingsGroup('Privacy', `<p class="st-label">Private by design. Your work stays on this computer.</p>
        <p class="st-help">Qorqbot’s servers know only your sign-in email, whether you use it for personal or work, and a daily check that it is running. Never your conversations, files, records or what your agents do.</p>
        <ul class="st-help"><li>You sign in on each service’s own page, or give Ordubashy the password when it asks so it can sign in for you. Qorqbot has no password store and its servers never see your passwords; website sessions stay in your agents’ private browsers on this computer.</li>
          <li>Every connection is optional, and you can turn it off at any time.</li>
          <li>What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.</li></ul>`,
        'privacy data collect tracking passwords private local'),
  };
  const icon = path => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${path}"/></svg>`;
  pane.innerHTML = `
    <header class="agfiles-head"><div><strong>${global ? 'Settings' : 'Project settings'}</strong>
      ${global ? '<span>Private by design: your work stays on this computer. <button type="button" class="cx-link" data-show-section="about">Details</button></span>' : ''}</div>
      ${global ? `${searchField({placeholder: "Search settings", attrs: {"data-settings-search": true}})}` : ''}</header>
    <div class="agfiles-layout">
      ${global ? `<nav class="agfiles agfiles-tree" aria-label="Settings sections">${sections.map(section =>
        `<div class="agfiles-row" style="--depth:0"><button type="button" class="agfiles-name" data-section-link="${section.id}">${icon(section.icon)}<span>${section.title}</span></button></div>`).join('')}</nav>` : ''}
      <div class="agfiles-detail"><div class="st-column">${sections.map(section => `<section class="st-section" data-section="${section.id}" data-title="${esc(section.title)}" aria-label="${esc(section.title)}">${body[section.id]}</section>`).join('')}
        <p class="st-help" data-settings-empty hidden>No settings match your search.</p></div></div>
    </div>`;
  c.appendChild(pane);
  if (global) agentSplitMount(pane.querySelector('.agfiles-layout'), 'qorqut.settings.listWidth');
  if (global) {
    const testing = p.testing = {
      toggle: pane.querySelector("#set-testing-toggle"), state: pane.querySelector("#set-testing-state"),
      note: pane.querySelector("#set-testing-note"), loaded: false, saving: false,
    };
    testing.toggle.onchange = () => saveTestingMode(p);
    testing.unsubscribe = testingModeSubscribe(() => {
      if (p.pane.isConnected && p.testing === testing && !testing.saving) paintTestingMode(p);
    });
    void loadTestingMode(p, true);
  }
  // Sections: one at a time, remembered; search shows every matching setting across sections.
  let current = 'connections';
  try { current = localStorage.getItem(SETTINGS_SECTION_KEY) || current; } catch (_) { /* First section. */ }
  if (!sections.some(section => section.id === current)) current = sections[0].id;
  const search = pane.querySelector('[data-settings-search]');
  function show(id) {
    current = id;
    try { localStorage.setItem(SETTINGS_SECTION_KEY, id); } catch (_) { /* Not remembered. */ }
    if (search && search.value) { search.value = ''; filter(); }
    paintSections();
  }
  function paintSections() {
    const query = (search?.value || '').trim().toLowerCase();
    pane.classList.toggle('st-searching', !!query);
    pane.querySelectorAll('[data-section-link]').forEach(link => {
      const on = !query && link.dataset.sectionLink === current;
      link.setAttribute('aria-current', String(on));
      link.closest('.agfiles-row').classList.toggle('selected', on);
    });
    pane.querySelectorAll('.st-section').forEach(section => { section.hidden = query ? section.dataset.matches === '0' : section.dataset.section !== current; });
  }
  function filter() {
    const query = (search?.value || '').trim().toLowerCase();
    let any = false;
    pane.querySelectorAll('.st-section').forEach(section => {
      let matches = 0;
      section.querySelectorAll('[data-search]').forEach(item => {
        const hit = !query || item.dataset.search.includes(query) || section.dataset.title.toLowerCase().includes(query);
        item.classList.toggle('st-miss', !hit);
        if (hit) matches++;
      });
      section.dataset.matches = String(matches);
      if (matches) any = true;
    });
    pane.querySelector('[data-settings-empty]').hidden = !query || any;
    paintSections();
  }
  settingsShowSection = id => { if (pane.isConnected && sections.some(section => section.id === id)) show(id); };
  pane.querySelectorAll('[data-section-link]').forEach(link => link.addEventListener('click', () => show(link.dataset.sectionLink)));
  pane.querySelector('[data-show-section]')?.addEventListener('click', event => show(event.currentTarget.dataset.showSection));
  search?.addEventListener('input', filter);
  // Rows that arrive later (connectors, CLI status, About) join the current search.
  pane.addEventListener('connectors-painted', filter);
  pane.addEventListener('settings-painted', filter);
  search?.addEventListener('keydown', event => { if (event.key === 'Escape' && search.value) { event.stopPropagation(); search.value = ''; filter(); } });
  paintSections();
  // Walkthrough
  pane.querySelector('[data-onboarding-replay]')?.addEventListener('click', () => {
    if (p.dirty || p.saving) { toast('Save your environment changes before starting the walkthrough.'); return; }
    onboarding.start();
  });
  pane.querySelectorAll('[data-onboarding-chapter]').forEach(button => button.addEventListener('click', () => {
    if (p.dirty || p.saving) { toast('Save your environment changes before starting the walkthrough.'); return; }
    onboarding.start(button.dataset.onboardingChapter);
  }));
  // Appearance
  pane.querySelectorAll('[data-choice]').forEach(button => button.addEventListener('click', () => {
    appearance.set({[button.dataset.choice]: button.dataset.value});
    button.parentElement.querySelectorAll('[role="radio"]').forEach(other => other.setAttribute('aria-checked', String(other === button)));
  }));
  // Timezone: saved when the field changes to an installed zone; applies here at once.
  const zoneInput = pane.querySelector('#set-timezone'), zoneNote = pane.querySelector('#set-timezone-note');
  if (zoneInput) {
    const paintZone = () => { zoneNote.textContent = ownerTimezoneOffset(SJ_ZONE) ? '(' + ownerTimezoneOffset(SJ_ZONE) + ' now)' : ''; zoneNote.classList.remove('error'); };
    paintZone();
    zoneInput.addEventListener('change', async () => {
      const zone = zoneInput.value.trim();
      if (zone === SJ_ZONE) return;
      zoneInput.disabled = true;
      try { await ownerTimezoneSave(zone); paintZone(); toast('Timezone set to ' + SJ_ZONE + '.'); }
      catch (error) { zoneInput.value = SJ_ZONE; zoneNote.textContent = uiFailure(error); zoneNote.classList.add('error'); }
      zoneInput.disabled = false;
    });
  }
  // Notifications
  const notifyStatus = () => {
    const el = pane.querySelector('[data-notify-status]'); if (!el) return;
    const permission = desktopNotify.permission();
    el.textContent = permission === 'granted' ? 'Allowed by your computer.' : permission === 'denied'
      ? 'Blocked by your computer. Allow notifications for Qorqbot in System Settings > Notifications.' : permission === 'unsupported'
      ? 'This browser can’t show desktop notifications.' : 'Your computer asks once when you turn one on.';
  };
  notifyStatus();
  pane.querySelectorAll('[data-switch]').forEach(box => box.addEventListener('change', async () => {
    box.disabled = true;
    const next = await desktopNotify.enable(box.dataset.switch, box.checked);
    box.checked = !!next[box.dataset.switch];
    box.disabled = false; notifyStatus();
  }));
  pane.querySelector('[data-notify-test]')?.addEventListener('click', async () => {
    if (desktopNotify.permission() === 'default' && desktopNotify.supported()) await Notification.requestPermission();
    notifyStatus();
    if (!desktopNotify.send({kind: 'test', title: 'Qorqbot', body: 'Notifications are working.', force: true}))
      toast(desktopNotify.permission() === 'denied' ? 'Notifications are blocked for Qorqbot.' : 'Notifications aren’t available here.');
  });
  if (global) {
    connectorsPanel.mount(pane.querySelector('[data-connectors-slot]'), {compact: true, rows: true});
    paintAgentMode(pane); paintAgentClients(pane); void paintAgentOptions(pane); void paintAbout(pane);
  }
  pane.querySelector("#envsave").onclick = () => saveEnv(p);
  pane.querySelector("#envadd").onclick = () => {
    p.rows.push({ key: "", value: "" }); envDirty(p); paintEnv(p);
    pane.querySelector(".envrow:last-child .ek")?.focus();
  };
  await loadEnv(p);
}

function testingModeNote(p, message, {error = false, retry = false} = {}) {
  const testing = p.testing;
  if (!testing) return;
  testing.note.textContent = message;
  testing.note.classList.toggle("error", error && !!message);
  if (retry) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "btn quiet set-testing-retry"; button.textContent = "Retry";
    button.onclick = () => loadTestingMode(p, true);
    testing.note.append(" ", button);
  }
}
function paintTestingMode(p) {
  const testing = p.testing;
  if (!testing) return;
  testing.toggle.checked = testingModeEnabled();
  testing.state.textContent = testingModeEnabled() ? "On" : "Off";
  testing.toggle.disabled = testing.saving || !testing.loaded;
}
async function loadTestingMode(p, retry = false) {
  const testing = p.testing;
  if (!testing || testing.saving) return;
  testing.loaded = false;
  testing.toggle.disabled = true;
  testing.state.textContent = "Loading…";
  testingModeNote(p, "Loading Testing mode…");
  await testingModeLoad(retry);
  if (!p.pane.isConnected || p.testing !== testing) return;
  if (testingModeFailed()) {
    testing.toggle.checked = false;
    testing.state.textContent = "Off";
    testingModeNote(p, testingModeMessage(), {error: true, retry: true});
    return;
  }
  testing.loaded = true;
  testingModeNote(p, "");
  paintTestingMode(p);
}
async function saveTestingMode(p) {
  const testing = p.testing;
  if (!testing || testing.saving || !testing.loaded) return;
  const wanted = testing.toggle.checked;
  testing.saving = true;
  testing.toggle.disabled = true;
  testing.state.textContent = wanted ? "On" : "Off";
  testingModeNote(p, "Saving Testing mode…");
  try {
    const enabled = await testingModeSet(wanted);
    if (!p.pane.isConnected || p.testing !== testing) return;
    testing.toggle.checked = enabled;
    testing.state.textContent = enabled ? "On" : "Off";
    testingModeNote(p, enabled ? "Testing mode on. Overseer is available." : "Testing mode off. Overseer is available in testing mode only.");
  } catch (error) {
    if (!p.pane.isConnected || p.testing !== testing) return;
    testing.toggle.checked = testingModeEnabled();
    testing.state.textContent = testingModeEnabled() ? "On" : "Off";
    testingModeNote(p, error?.message || "Testing mode could not be saved.", {error: true});
  } finally {
    testing.saving = false;
    if (p.pane.isConnected && p.testing === testing) paintTestingMode(p);
  }
}

// Model/Provider section (id `agents`): how agents run (js/agent-mode.js) comes first because it changes what the rows below mean; the extra rows
// (the extra-rows script) appear only when agents run the legacy way.
function paintAgentMode(pane) {
  const host = pane.querySelector('[data-agent-mode]'); if (!host) return;
  const panel = agentMode.mount(host, {toast});
  panel.root.addEventListener('agent-mode-painted', () => pane.dispatchEvent(new Event('settings-painted')));
  const extras = pane.querySelector('[data-agent-extras]'); if (!extras) return;
  agentTrustRow.mount(extras, {toast, onChange: () => pane.dispatchEvent(new Event('settings-painted'))});
}

// Model/Provider section: Claude Code and Codex, downloaded and signed in from here (js/agent-clients.js).
function paintAgentClients(pane) {
  const host = pane.querySelector('[data-agent-clients]'); if (!host) return;
  const panel = agentClients.mount(host);
  panel.root.addEventListener('agent-clients-painted', () => pane.dispatchEvent(new Event('settings-painted')));
}

// One row per agent: client, model and effort (js/agent-options.js). The agent list is the one the agents menu shows.
async function paintAgentOptions(pane) {
  const host = pane.querySelector('[data-agent-options]'); if (!host) return;
  host.innerHTML = '<div class="st-rows"><p class="ao-empty">Reading your agents…</p></div>';
  let agents;
  try { agents = (await loadGlobalAgentProfiles()).filter(globalRosterAgent).map(a => ({agent_id: a.agent_id, name: a.display_name || a.name || a.agent_id})); }
  catch (_) { host.innerHTML = '<div class="st-rows"><p class="ao-empty">Your agents couldn’t be read. Reload the page to try again.</p></div>'; return; }
  if (!host.isConnected) return;
  const panel = agentOptions.mount(host, agents);
  panel.root.classList.add('st-rows');
  panel.root.addEventListener('agent-options-painted', () => pane.dispatchEvent(new Event('settings-painted')));
}

// The Qorqbot account row in About (qorqut/account.py): who is signed in, personal or work use, and sign out.
function settingsAccountRow(account) {
  if (!account.signed_in) {
    return settingsRow({label: 'Qorqbot account', keywords: 'account sign in email login license',
      help: 'Not signed in.', control: '<button type="button" class="quiet" data-account-signin>Sign in</button>'});
  }
  const info = account.account || {};
  const license = accountGate.licenseMessage(account);
  const note = license && license.text ? ` ${esc(license.text)}` : '';
  const link = `<a href="${accountGate.LICENSE_URL}" target="_blank" rel="noopener noreferrer">License</a>`;
  const use = info.use === 'work'
    ? `For work${info.company ? ` at ${esc(info.company)}` : ''}.${note} ${link}`
    : `Personal use.${note}`;
  return settingsRow({label: 'Qorqbot account', keywords: 'account sign in email login license commercial paid plan',
    help: `${esc(info.email || '')} · ${use}`, control: '<button type="button" class="quiet" data-account-signout>Sign out</button>'});
}
function settingsAccountRefresh() {
  const pane = document.querySelector('.settingspane');
  if (pane) paintAbout(pane);
}

// About section: version and folders (owner_settings.about_view).
async function paintAbout(pane) {
  const host = pane.querySelector('[data-about]'); if (!host) return;
  let view, account = null;
  try { view = coreData(await api('/root/about')); } catch (error) { host.innerHTML = `<p class="st-help">${esc(error.message || 'Could not read version details.')}</p>`; return; }
  try { account = await accountGate.read(); } catch (_) { /* Shown without the account row. */ }
  if (!host.isConnected) return;
  const copy = value => value ? `<button type="button" class="quiet" data-copy="${esc(value)}">Copy path</button>` : '';
  const update = account && account.update
    ? ` · <a href="${esc(account.update.url)}" target="_blank" rel="noopener noreferrer">Qorqbot ${esc(account.update.version)} is available</a>` : '';
  host.innerHTML = settingsRow({label: 'Version', keywords: 'version update release', help: esc(view.version || 'Development server') + update}) +
    (account ? settingsAccountRow(account) : '') +
    (view.install_folder ? settingsRow({label: 'Program folder', keywords: 'install folder path', help: `<code>${esc(view.install_folder)}</code>`, control: copy(view.install_folder)}) : '') +
    (view.data_folder ? settingsRow({label: 'Your data', keywords: 'data folder records backup path', help: `<code>${esc(view.data_folder)}</code> All records, chats and history. Back up this folder to keep them.`, control: copy(view.data_folder)}) : '') +
    (view.logs_folder ? settingsRow({label: 'Logs', keywords: 'logs debug troubleshoot path', help: `<code>${esc(view.logs_folder)}</code>`, control: copy(view.logs_folder)}) : '');
  pane.dispatchEvent(new Event('settings-painted'));
  host.querySelector('[data-account-signout]')?.addEventListener('click', async event => {
    event.target.disabled = true;
    try { await accountGate.send('logout', {}); } catch (_) { /* Repainted below either way. */ }
    paintAbout(pane);
    accountGate.enforce();
  });
  host.querySelector('[data-account-signin]')?.addEventListener('click', () => accountGate.signIn());
  host.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = 'Copied'; } catch (_) { button.textContent = 'Select and copy'; }
    setTimeout(() => { if (button.isConnected) button.textContent = 'Copy path'; }, 2000);
  }));
}

function envError(p, message, retry = false) {
  const note = p.pane.querySelector("#envnote");
  note.textContent = message;
  if (retry) {
    const button = document.createElement("button");
    button.className = "quiet"; button.id = "envretry"; button.textContent = "Retry";
    button.onclick = () => loadEnv(p); note.append(" ", button);
  }
}
async function loadEnv(p) {
  envError(p, "Loading environment…");
  try {
    const r = await api(p.url);
    if (!envCurrent(p)) return;
    if (r.error || !Array.isArray(r.pairs)) throw new Error(r.error || "Environment response is unavailable.");
    p.rows = r.pairs.map(row => ({ ...row }));
    p.inherited = (r.inherited || []).map(row => ({ ...row }));
    p.loaded = true; p.dirty = false;
    envError(p, ""); paintEnv(p);
  } catch (error) {
    if (envCurrent(p)) envError(p, error.message || "Could not load environment.", true);
  }
}
function envDirty(p) {
  p.dirty = true;
  p.pane.querySelector("#envsave").disabled = p.saving || !p.loaded;
  envError(p, "");
}
function paintEnv(p) {
  const inherited = new Map(p.inherited.map(row => [row.key, row.value]));
  const local = new Set(p.rows.map(row => row.key.trim()));
  const shown = [
    ...p.inherited.filter(row => !local.has(row.key)).map(row => ({ ...row, inherited: true })),
    ...p.rows.map((row, index) => ({ ...row, index })),
  ];
  p.pane.querySelector("#envrows").innerHTML = shown.map(row => {
    const override = !p.global && inherited.has(row.key.trim()) && !row.inherited;
    const source = row.inherited ? "Global" : override ? "Project override" : p.global ? "Global" : "Project";
    return `<div class="envrow" data-index="${row.index ?? ""}">
      <div class="envkey"><input class="ek mono ef-control" aria-label="Variable name" placeholder="KEY" value="${esc(row.key)}" ${row.inherited ? "disabled" : override ? "readonly" : ""} spellcheck="false"><span class="envsource">${source}</span></div>
      <input class="ev mono ef-control" aria-label="Value for ${esc(row.key || "new variable")}" placeholder="value" value="${esc(row.value)}" ${row.inherited ? "disabled" : ""} spellcheck="false">
      <button class="envdel" type="button" data-key="${esc(row.key)}" data-action="${row.inherited ? "override" : "remove"}" aria-label="${row.inherited ? "Override " : override ? "Use Global for " : "Remove "}${esc(row.key || "variable")}">${row.inherited ? "Override" : override ? "Use Global" : "×"}</button>
    </div>`;
  }).join("") || '<p class="muted small">No environment variables yet.</p>';
  p.pane.querySelectorAll(".envrow").forEach(row => {
    const button = row.querySelector("button");
    if (button.dataset.action === "override") {
      button.onclick = () => {
        p.rows.push({ key: button.dataset.key, value: inherited.get(button.dataset.key) });
        envDirty(p); paintEnv(p);
        p.pane.querySelector(".envrow:last-child .ev")?.focus();
      };
      return;
    }
    const index = Number(row.dataset.index), key = row.querySelector(".ek");
    key.oninput = () => { p.rows[index].key = key.value; key.classList.toggle("bad", !!key.value.trim() && !ENV_KEY_JS.test(key.value.trim())); envDirty(p); };
    row.querySelector(".ev").oninput = e => { p.rows[index].value = e.target.value; envDirty(p); };
    button.onclick = () => { p.rows.splice(index, 1); envDirty(p); paintEnv(p); };
  });
  p.pane.querySelector("#envsave").disabled = !p.loaded || p.saving || !p.dirty;
  p.pane.querySelector("#envadd").disabled = !p.loaded || p.saving;
  if (p.saving) p.pane.querySelectorAll("#envrows input, #envrows button").forEach(el => el.disabled = true);
}

async function saveEnv(p = envView) {
  if (!p || !envCurrent(p) || !p.loaded || p.saving) return;
  const pairs = p.rows.map(row => ({ key: row.key.trim(), value: row.value })).filter(row => row.key);
  if (pairs.some(row => !ENV_KEY_JS.test(row.key))) return envError(p, "Use letters, digits and underscores for keys; do not start with a digit.");
  if (new Set(pairs.map(row => row.key)).size !== pairs.length) return envError(p, "Each variable name must be unique.");
  p.saving = true; paintEnv(p);
  try {
    const r = await post(p.url, { pairs });
    if (!envCurrent(p)) return;
    if (r.error || !Array.isArray(r.pairs)) throw new Error(r.error || "Could not save environment.");
    p.rows = r.pairs.map(row => ({ ...row })); p.inherited = (r.inherited || []).map(row => ({ ...row }));
    p.dirty = false; envError(p, ""); toast("Environment saved");
  } catch (error) {
    if (envCurrent(p)) envError(p, error.message || "Could not save environment.");
  } finally {
    p.saving = false;
    if (envCurrent(p)) paintEnv(p);
  }
}
