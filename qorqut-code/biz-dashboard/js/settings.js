// Environment defaults belong to Global; a business saves only its explicit overrides.
let envView = null;
const ENV_KEY_JS = /^[A-Za-z_][A-Za-z0-9_]*$/;
const envScope = () => state.scope === "global" ? "global" : state.biz;
const envCurrent = p => envView === p && p.pane.isConnected && envScope() === p.scope;

// Settings: a section list on the left, one section on the right, and search across every setting. Global settings
// have every section; a project has only its environment overrides.
const SETTINGS_SECTIONS = [
  {id: 'connections', title: 'Connections', hint: 'Tools your agents can use'},
  {id: 'agents', title: 'Agents', hint: 'Claude Code, Codex and how agents start'},
  {id: 'appearance', title: 'Appearance', hint: 'Theme and text size'},
  {id: 'notifications', title: 'Notifications', hint: 'Desktop alerts'},
  {id: 'environment', title: 'Environment', hint: 'Variables such as API keys'},
  {id: 'walkthrough', title: 'Walkthrough', hint: 'Take the tour again'},
  {id: 'about', title: 'About & privacy', hint: 'Version, folders, your data'},
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
const settingsSwitch = (name, on, label) => `<button type="button" class="st-switch" role="switch" aria-checked="${!!on}" aria-label="${esc(label)}" data-switch="${esc(name)}"><i></i></button>`;

async function renderSettings(c) {
  envView?.testing?.unsubscribe?.();
  const scope = envScope(), global = state.scope === "global";
  const pane = document.createElement("div");
  pane.className = "pane settingspane" + (global ? " st-global" : "");
  const p = envView = { pane, scope, global, url: global ? "/root/env" : "/biz/" + encodeURIComponent(state.biz) + "/env",
    rows: [], inherited: [], loaded: false, saving: false, dirty: false, testing: null };
  const sections = global ? SETTINGS_SECTIONS : SETTINGS_SECTIONS.filter(section => section.id === 'environment');
  const environment = `
    <div class="st-env-head"><p class="st-help">${global ? "Shared defaults for all projects and Global agents. Projects can override individual values." : "Global values are inherited automatically. Override a value for this project, or use Global again."}</p>
      <button class="btn" id="envsave" disabled>Save</button></div>
    <div class="envrows" id="envrows"></div>
    <button class="envadd" id="envadd" disabled>+ Add variable</button>
    <div class="set-note muted small" id="envnote" role="status"></div>
    <p class="st-help">Changes apply to new agent sessions and later jobs. Agents already running keep their current environment.</p>`;
  const look = appearance.get(), alerts = desktopNotify.prefs();
  const body = {
    connections: `<div class="st-block" data-search="connections connectors agent browser chrome gmail calendar drive notion slack whatsapp" data-connectors-slot></div>`,
    agents: `<div class="st-rows" data-agent-clients><p class="st-help">Checking Claude Code and Codex…</p></div>
      <div class="st-rows">${settingsRow({label: 'Trusted agent folders', keywords: 'trust consent hooks prompts folder security',
        help: '<span data-agent-trust-text>Checking…</span>', control: '<button type="button" class="quiet" data-agent-trust-toggle disabled>…</button>', attrs: 'data-agent-trust'})}
      ${global ? settingsRow({label: 'Testing mode', keywords: 'testing overseer global agent roster chat terminal workspace experimental',
        help: 'Show Overseer in the Global agent roster and enable its chat, terminal and workspace. Off by default.<span class="set-testing-note" id="set-testing-note" role="status"></span>',
        control: `<label class="set-switch" for="set-testing-toggle"><input type="checkbox" id="set-testing-toggle" role="switch" aria-label="Testing mode" disabled><span class="set-switch-track" aria-hidden="true"></span><span class="set-switch-label" id="set-testing-state">Loading…</span></label>`,
        attrs: 'data-testing-mode'}) : ''}
      ${settingsRow({label: 'Starting an agent', keywords: 'start launch terminal claude codex restart',
        help: 'Open the agent, click <b>&gt;_</b> above its Chat and type <code>claude</code> or <code>codex</code>. Chat’s <b>Start</b> button does the same. Agents never start by themselves.'})}</div>`,
    appearance: `<div class="st-rows">${settingsRow({label: 'Theme', keywords: 'dark light system mode color colour', help: 'System follows your computer’s light or dark setting.',
        control: settingsChoice('theme', [['dark', 'Dark'], ['light', 'Light'], ['system', 'System']], look.theme)})}
      ${settingsRow({label: 'Text size', keywords: 'font zoom bigger smaller scale', help: 'Scales the whole dashboard.',
        control: settingsChoice('textSize', [['small', 'Small'], ['default', 'Default'], ['large', 'Large']], look.textSize)})}
      <p class="st-help st-note">Saved for this browser only.</p></div>`,
    notifications: `<div class="st-rows">${settingsRow({label: 'Agent replies', keywords: 'notify alert message reply desktop',
        help: 'When an agent answers you while the dashboard is in the background.', control: settingsSwitch('replies', alerts.replies, 'Desktop notifications for agent replies')})}
      ${settingsRow({label: 'Signoffs waiting on you', keywords: 'notify alert approval signoff decision desktop',
        help: 'When an approval or decision needs you.', control: settingsSwitch('signoffs', alerts.signoffs, 'Desktop notifications for Signoffs')})}
      ${settingsRow({label: 'Test', keywords: 'notification permission test', help: '<span data-notify-status></span>',
        control: '<button type="button" class="quiet" data-notify-test>Send a test</button>'})}</div>`,
    environment: `<div class="st-block" data-search="environment variables api keys env secrets token">${environment}</div>`,
    walkthrough: `<div class="st-rows">${settingsRow({label: 'Take the walkthrough again', keywords: 'tour onboarding help guide tutorial',
        help: 'A short, interactive tour of your workspace.', control: '<button type="button" class="quiet" data-onboarding-replay>Start</button>'})}
      ${settingsRow({label: 'One chapter', keywords: 'tour chapter onboarding', help: 'Replay just one part.',
        control: `<div class="st-chapters">${onboarding.chapters().map(chapter => `<button type="button" class="quiet" data-onboarding-chapter="${esc(chapter.id)}">${esc(chapter.title)}</button>`).join('')}</div>`})}</div>`,
    about: `<div class="st-rows" data-about><p class="st-help">Loading…</p></div>
      <div class="st-privacy" data-search="privacy data collect tracking passwords private local">
        <h3>Private by design. Qorqbot collects nothing.</h3>
        <p>No Qorqbot account, no tracking and no Qorqbot servers. Qorqbot runs on this computer and everything it keeps stays here.</p>
        <ul><li>Your sign-ins stay with Google and Claude. Qorqbot never sees or stores your passwords.</li>
          <li>Every connection is optional, and you can turn it off at any time.</li>
          <li>What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.</li></ul></div>`,
  };
  pane.innerHTML = `
    <header class="st-header"><div><h1 class="st-title">${global ? 'Settings' : 'Project settings'}</h1>
      ${global ? '<p class="st-promise"><span aria-hidden="true">●</span> Private by design: Qorqbot collects nothing and keeps everything on this computer. <button type="button" class="cx-link" data-show-section="about">Details</button></p>' : ''}</div>
      ${global ? '<label class="st-search"><span class="sr-only">Search settings</span><input type="search" placeholder="Search settings" data-settings-search autocomplete="off" spellcheck="false"></label>' : ''}</header>
    <div class="st-layout">
      ${global ? `<nav class="st-nav" aria-label="Settings sections">${sections.map(section =>
        `<button type="button" data-section-link="${section.id}"><span>${section.title}</span><small>${section.hint}</small></button>`).join('')}</nav>` : ''}
      <div class="st-sections">${sections.map(section => `<section class="st-section" data-section="${section.id}" aria-labelledby="st-h-${section.id}">
        <h2 id="st-h-${section.id}">${section.title}</h2>${body[section.id]}</section>`).join('')}
        <p class="st-empty" data-settings-empty hidden>No settings match your search.</p></div>
    </div>`;
  c.appendChild(pane);
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
    pane.querySelectorAll('[data-section-link]').forEach(link => link.setAttribute('aria-current', String(!query && link.dataset.sectionLink === current)));
    pane.querySelectorAll('.st-section').forEach(section => { section.hidden = query ? section.dataset.matches === '0' : section.dataset.section !== current; });
  }
  function filter() {
    const query = (search?.value || '').trim().toLowerCase();
    let any = false;
    pane.querySelectorAll('.st-section').forEach(section => {
      let matches = 0;
      section.querySelectorAll('[data-search]').forEach(item => {
        const hit = !query || item.dataset.search.includes(query) || section.querySelector('h2').textContent.toLowerCase().includes(query);
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
  // Notifications
  const notifyStatus = () => {
    const el = pane.querySelector('[data-notify-status]'); if (!el) return;
    const permission = desktopNotify.permission();
    el.textContent = permission === 'granted' ? 'Allowed by your computer.' : permission === 'denied'
      ? 'Blocked by your computer. Allow notifications for Qorqbot in System Settings > Notifications.' : permission === 'unsupported'
      ? 'This browser can’t show desktop notifications.' : 'Your computer asks once when you turn one on.';
  };
  notifyStatus();
  pane.querySelectorAll('[data-switch]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    const next = await desktopNotify.enable(button.dataset.switch, button.getAttribute('aria-checked') !== 'true');
    button.setAttribute('aria-checked', String(next[button.dataset.switch]));
    button.disabled = false; notifyStatus();
  }));
  pane.querySelector('[data-notify-test]')?.addEventListener('click', async () => {
    if (desktopNotify.permission() === 'default' && desktopNotify.supported()) await Notification.requestPermission();
    notifyStatus();
    if (!desktopNotify.send({kind: 'test', title: 'Qorqbot', body: 'Notifications are working.', force: true}))
      toast(desktopNotify.permission() === 'denied' ? 'Notifications are blocked for Qorqbot.' : 'Notifications aren’t available here.');
  });
  if (global) {
    connectorsPanel.mount(pane.querySelector('[data-connectors-slot]'), {compact: true, rows: true});
    void paintAgentTrust(pane); void paintAgentClients(pane); void paintAbout(pane);
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

// Agents section: the CLIs agents run (owner_settings.agent_clients_view).
async function paintAgentClients(pane) {
  const host = pane.querySelector('[data-agent-clients]'); if (!host) return;
  let view;
  try { view = coreData(await api('/root/agent-clients')); } catch (error) { host.innerHTML = `<p class="st-help">${esc(error.message || 'Could not check Claude Code and Codex.')}</p>`; return; }
  if (!host.isConnected) return;
  const describe = (name, item, install, signIn) => {
    if (!item.installed) return settingsRow({label: name, keywords: 'cli install version', help: `Not installed. ${install}`, control: '<span class="st-pill" data-tone="warn">Not installed</span>'});
    const account = item.account?.email ? ` as ${esc(item.account.email)}${item.account.plan ? ` (${esc(item.account.plan)})` : ''}` : '';
    const help = item.signed_in ? `Version ${esc(item.version || 'unknown')}. Signed in${account}.` : `Version ${esc(item.version || 'unknown')}. Not signed in: run <code>${signIn}</code> once in Terminal.`;
    return settingsRow({label: name, keywords: 'cli install version signed account', help, control: `<span class="st-pill" data-tone="${item.signed_in ? 'ok' : 'warn'}">${item.signed_in ? 'Ready' : 'Sign in needed'}</span>`});
  };
  host.innerHTML = describe('Claude Code', view.claude, 'Install it from claude.com/code, then run <code>claude</code> once to sign in.', 'claude') +
    describe('Codex', view.codex, 'Install it from github.com/openai/codex, then run <code>codex</code> once to sign in.', 'codex login');
  pane.dispatchEvent(new Event('settings-painted'));
}

// About section: version and folders (owner_settings.about_view).
async function paintAbout(pane) {
  const host = pane.querySelector('[data-about]'); if (!host) return;
  let view;
  try { view = coreData(await api('/root/about')); } catch (error) { host.innerHTML = `<p class="st-help">${esc(error.message || 'Could not read version details.')}</p>`; return; }
  if (!host.isConnected) return;
  const copy = value => value ? `<button type="button" class="quiet" data-copy="${esc(value)}">Copy path</button>` : '';
  host.innerHTML = settingsRow({label: 'Version', keywords: 'version update release', help: esc(view.version || 'Development server')}) +
    (view.install_folder ? settingsRow({label: 'Program folder', keywords: 'install folder path', help: `<code>${esc(view.install_folder)}</code>`, control: copy(view.install_folder)}) : '') +
    (view.data_folder ? settingsRow({label: 'Your data', keywords: 'data folder records backup path', help: `<code>${esc(view.data_folder)}</code> All records, chats and history. Back up this folder to keep them.`, control: copy(view.data_folder)}) : '') +
    (view.logs_folder ? settingsRow({label: 'Logs', keywords: 'logs debug troubleshoot path', help: `<code>${esc(view.logs_folder)}</code>`, control: copy(view.logs_folder)}) : '');
  pane.dispatchEvent(new Event('settings-painted'));
  host.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = 'Copied'; } catch (_) { button.textContent = 'Select and copy'; }
    setTimeout(() => { if (button.isConnected) button.textContent = 'Copy path'; }, 2000);
  }));
}

// The owner's consent that Qorqbot's own agent folders are trusted (bazarlyk.agent_trust): claude and codex then skip their
// folder-trust and Codex hook-review prompts for those folders. A CLI can still ask about an enclosing repository, which the
// owner answers in the terminal. Turning it off restores Codex's own hook review for new launches.
async function paintAgentTrust(pane) {
  const section = pane.querySelector('[data-agent-trust]');
  if (!section) return;
  const text = section.querySelector('[data-agent-trust-text]'), toggle = section.querySelector('[data-agent-trust-toggle]');
  let view;
  try { view = coreData(await api('/root/agent-trust')); } catch (_) { text.textContent = 'Could not read this setting.'; return; }
  if (!section.isConnected) return;
  text.textContent = view.granted
    ? 'On. Each agent’s own folder is marked as trusted in Claude Code and Codex, and Codex runs Qorqbot’s hooks there without its review prompt, whether you type claude or codex in the agent’s Terminal or press Start in Chat. Either CLI can still ask you to trust a larger folder around an agent, such as the git repository it sits in; you decide that in the terminal.'
    : 'Off. The first time an agent starts in its folder, Claude Code asks whether to trust it and Codex asks you to review its hooks.';
  toggle.textContent = view.granted ? 'Turn off' : 'Turn on';
  toggle.disabled = false;
  toggle.onclick = async () => {
    toggle.disabled = true;
    try {
      if (view.granted) coreData(await post('/root/agent-trust', {granted: false}));
      else await agentStart.consent(null, 'Ordubashy');
    } catch (error) { toast(error.message); }
    void paintAgentTrust(pane);
  };
}

function envError(p, message, retry = false) {
  const note = p.pane.querySelector("#envnote");
  note.textContent = message;
  if (retry) {
    const button = document.createElement("button");
    button.className = "btn quiet"; button.id = "envretry"; button.textContent = "Retry";
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
      <div class="envkey"><input class="ek mono" aria-label="Variable name" placeholder="KEY" value="${esc(row.key)}" ${row.inherited ? "disabled" : override ? "readonly" : ""} spellcheck="false"><span class="envsource">${source}</span></div>
      <input class="ev mono" aria-label="Value for ${esc(row.key || "new variable")}" placeholder="value" value="${esc(row.value)}" ${row.inherited ? "disabled" : ""} spellcheck="false">
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
