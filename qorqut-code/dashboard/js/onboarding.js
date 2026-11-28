// A small owner-only walkthrough. Navigation is real; the only message the tour ever sends is the hello its last step types for the
// owner, when the owner presses done. Progress belongs to the installation, not a browser.
const onboarding = (() => {
  const chapterTitles = {online: 'get started', workspace: 'your workspace', projects: 'projects and decisions', yours: 'make it yours',
    connect: 'connect your tools'};
  const pane = () => document.querySelector('#agpad');
  // One tour for everyone: Qorqbot runs every agent, and the owner only uses Chat. Ordubashy starts with the first Chat message.
  const allSteps = [
    // The agent list, Walkthrough and Settings live in the menu under the mark at the top left. Steps marked `menu`
    // hold that menu open (globalAgentNavHold) while they point into it.
    {id: 'assistant', chapter: 'online', title: 'meet ordubashy', menu: true,
      body: 'Ordubashy is your lead agent. Give him a goal. He carries the work forward, hands parts to helpers and brings decisions back to you. Qorqbot runs him for you, and you talk to him in Chat.',
      hint: 'This menu opens when you point at the mark at the top left. It lists your agents, Walkthrough and Settings.',
      target: () => agentButton('ordubashy'),
      // terminals-begin
      terminals: {
        body: 'Ordubashy is your lead agent. Give him a goal. He carries the work forward, hands parts to helpers and brings decisions back to you. You start him in his own Terminal, and you talk to him in Chat.',
        hint: 'This menu opens when you point at the mark at the top left. It lists your agents, Walkthrough, Terminal and Settings.'},
      // terminals-end
    },
    {id: 'focus', chapter: 'workspace', view: 'focus', title: 'focus',
      body: 'The right side is Ordubashy’s workspace. Focus keeps what needs you in one place: signoffs waiting for your answer, with your own notes below. The feather beside Browser brings you back to them.',
      hint: 'Only you see these notes; agents never read them.',
      target: pane},
    {id: 'projects', chapter: 'projects', title: 'start a project',
      body: 'Projects keep the work, records and context for one client or company apart. You create them by asking.',
      instructions: ['In Chat, ask Ordubashy: “Create a project for …” with a name and what it is for.', 'He prepares it and asks for your signoff before anything is created.'],
      hint: 'Opening a project from a link or record switches the whole workspace to it.',
      target: () => document.querySelector('#global-agent-chat .gchat-composer')},
    {id: 'signoffs', chapter: 'projects', view: 'focus', title: 'signoffs',
      body: 'When an agent needs your approval, such as for creating a project, it waits as a Signoff at the top of Focus. Nothing happens until you approve it. Due reminders show up there too.',
      example: {agent: 'Ordubashy', title: 'Create the project “Acme Bakery”', detail: 'A project for the bakery’s orders, suppliers and weekly menu.'},
      target: pane, action: 'open signoffs',
      run: () => { sgAttentionNavigate(null); return 'Close the list, or press Esc, to continue the walkthrough.'; }},
    {id: 'settings', chapter: 'yours', title: 'settings', menu: true,
      body: 'Settings holds your connections, the client and model each agent uses, the theme and text size, and values such as API keys.',
      hint: 'Walkthrough, in this same menu, takes this tour again, or one chapter of it.',
      target: () => document.querySelector('[data-global-settings]'),
      // terminals-begin
      terminals: {body: 'Settings holds your connections, how agents run, the client and model each agent uses, the theme and text size, and values such as API keys.'},
      // terminals-end
    },
    // No target: the whole screen dims and the promise stands on its own.
    {id: 'privacy', chapter: 'connect', title: 'your data stays yours', wide: true, standalone: true,
      body: '<b class="onboarding-promise">Your work stays on this computer.</b> Qorqbot’s servers know only your sign-in email and that Qorqbot is in use. Never your conversations, files or what your agents do.',
      instructions: ['What you ask your agents goes to Claude or Codex under your own account, the same as when you use them directly.'],
      target: () => null,
      // terminals-begin
      terminals: {instructions: ['What you ask your agents goes to Claude Code under your own account, the same as when you use it directly.']},
      // terminals-end
    },
    {id: 'connect-overview', chapter: 'connect', title: 'two ways to reach your tools', wide: true, standalone: true,
      body: 'Both are optional.',
      instructions: ['<b>Agent browser:</b> built into Qorqbot and the default way agents use websites. Works with both Claude Code and Codex. To connect a site, ask Ordubashy, for example “connect my Slack”. It opens the sign-in page in the agent browser and you sign in yourself, so your password goes only to that website.',
        '<b>Extensions:</b> extra tools that run inside Qorqbot and work with both Claude Code and Codex. Start with treg: one key gives your agents thousands of live data lookups, such as search results and company details. Add your key in Settings under Extensions.'],
      target: () => null,
      // terminals-begin
      terminals: {instructions: ['<b>Agent browser:</b> built into Qorqbot and the default way agents use websites. Works with Claude Code. To connect a site, ask Ordubashy, for example “connect my Slack”. It opens the sign-in page in the agent browser and you sign in yourself, so your password goes only to that website.',
        '<b>Extensions:</b> extra tools that run inside Qorqbot and work with Claude Code. Start with treg: one key gives your agents thousands of live data lookups, such as search results and company details. Add your key in Settings under Extensions.']},
      // terminals-end
    },
    {id: 'browser-setup', chapter: 'connect', tab: 'settings', title: 'agent browser',
      body: 'It sets itself up, and Ordubashy fixes it if something goes wrong. This card shows when it’s ready. Websites Ordubashy connects for you are listed with it.',
      target: () => document.querySelector('[data-cx-browser-area]')},
    // The tour ends where the first conversation starts, so his questions never arrive behind the tour.
    {id: 'chat', chapter: 'connect', title: 'say hello', hello: true,
      body: 'Type “hi, help me get set up” and press send. Ordubashy asks a few quick questions about you, then connects the tools you want him to use.',
      hint: 'His replies appear here in Chat. He starts with your first message.',
      // Neither Claude Code nor Codex is connected: nothing would answer. Settings is where one gets connected.
      setup: {none: 'Ordubashy needs Claude Code or Codex to answer, and neither is connected yet. Connect one in Settings, then come back and say hello.',
        chosen: name => `Ordubashy is set to use ${name}, which isn’t connected. Connect it in Settings, or choose the other one under Model/Provider, then say hello.`},
      // A first walkthrough types the hello for the owner, and done sends it.
      typed: '“hi, help me get set up” is typed in below. Press done to send it. Ordubashy asks a few quick questions about you, then connects the tools you want him to use.',
      target: () => document.querySelector('#global-agent-chat .gchat-composer'),
      // terminals-begin
      // Nothing starts Ordubashy by itself here, so the hello is typed only once he is running (see terminalHello).
      terminals: {
        body: 'Once Ordubashy is running, say “hi, help me get set up” in Chat. He asks a few quick questions about you, then connects the tools you want him to use.',
        hint: 'His replies appear here in Chat. He answers only while he is running in his Terminal.',
        offline: 'Start Ordubashy in his Terminal first, with the terminal icon above Chat, then say hello to him in Chat.'},
      // terminals-end
    },
  ];
  let steps = allSteps;
  // terminals-begin
  // The Terminals wording replaces fields of a step only while agents run in terminals; otherwise the list is used as written.
  const terminalsOn = () => typeof agentMode !== 'undefined' && agentMode.terminals();
  // terminals-end
  let root = null, card = null, target = null, index = 0, generation = 0, busy = false;
  let saved = null, writes = Promise.resolve(), observer = null, resize = null, frame = 0, previousFocus = null;
  let targetDescription = null, welcome = false, booted = false, savedInert = false, stepTargetReady = false;
  // Taking a finished walkthrough again (from Settings or the menu) never changes its saved progress.
  // Set once a step's action opened a list in the workspace (the signoffs step), so the tour closes it when it moves on.
  let openedList = false;
  let replay = false;
  // The agent the first message goes to when a first walkthrough finishes on the hello step; null when the owner types it.
  let greet = null, unanswered = false;
  const HELLO = 'hi, help me get set up';
  const holdMenu = hold => { if (typeof globalAgentNavHold === 'function') globalAgentNavHold(hold); };
  const visible = el => !!el?.isConnected && !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const findAgent = role => globalAgentProfiles.find(a => globalRosterAgent(a) && (a.role?.id || a.role_id) === role);
  const agentButton = role => { const id = findAgent(role)?.agent_id; return id ? document.querySelector(`[data-global-agent="${CSS.escape(id)}"]`) : null; };
  const appDialog = () => [...document.querySelectorAll('.overlay,.cmenu')].some(visible);
  const isCurrent = token => root && token === generation;

  function waitFor(resolve, timeout = 6000) {
    const existing = resolve(); if (existing) return Promise.resolve(existing);
    return new Promise((done, reject) => {
      const watch = new MutationObserver(() => { const value = resolve(); if (value) { cleanup(); done(value); } });
      const timer = setTimeout(() => { cleanup(); reject(new Error('This part of the workspace isn’t available yet.')); }, timeout);
      const cleanup = () => { clearTimeout(timer); watch.disconnect(); };
      watch.observe(document.body, {childList: true, subtree: true});
    });
  }
  function persist(status, step = onboardingPlan.saveId(steps[index])) {
    if (replay) return Promise.resolve();
    saved = {status, step};
    writes = writes.catch(() => {}).then(() => post('/root/onboarding', {status, step})).then(coreData);
    writes.catch(() => { if (root) notice('Progress couldn’t be saved. You can still continue or skip.'); });
    return writes;
  }
  function notice(text) { const el = root?.querySelector('[data-tour-notice]'); if (el) { el.textContent = text; schedule(); } }
  function describeTarget(next) {
    if (target === next) return;
    if (target) {
      if (targetDescription === null) target.removeAttribute('aria-describedby');
      else target.setAttribute('aria-describedby', targetDescription);
    }
    target = next; targetDescription = target?.getAttribute('aria-describedby') ?? null;
    if (target) target.setAttribute('aria-describedby', [targetDescription, 'onboarding-description'].filter(Boolean).join(' '));
    resize?.disconnect(); if (target) resize?.observe(target); if (card) resize?.observe(card);
  }
  function setWelcome(value) {
    welcome = value; root?.classList.toggle('is-welcome', value);
    const app = document.querySelector('.app'); if (app) app.inert = value || savedInert;
    if (card) {
      card.setAttribute('aria-modal', String(value));
      if (value) card.removeAttribute('aria-describedby');
      else card.setAttribute('aria-describedby', 'onboarding-description');
    }
  }
  function create() {
    stopWatching();
    if (root) return;
    previousFocus = document.activeElement; savedInert = !!document.querySelector('.app')?.inert;
    replay = saved?.status === 'completed';
    // terminals-begin
    steps = onboardingPlan.stepsFor(allSteps, terminalsOn());
    // terminals-end
    root = document.createElement('div'); root.className = 'onboarding';
    root.innerHTML = `<div class="onboarding-shade" data-shade="top"></div><div class="onboarding-shade" data-shade="bottom"></div><div class="onboarding-shade" data-shade="left"></div><div class="onboarding-shade" data-shade="right"></div><div class="onboarding-outline" aria-hidden="true"></div><section class="onboarding-card" role="dialog" aria-labelledby="onboarding-title" aria-describedby="onboarding-description"></section>`;
    card = root.querySelector('.onboarding-card'); document.body.appendChild(root);
    observer = new MutationObserver(records => { if (records.some(r => !root?.contains(r.target))) schedule(); });
    observer.observe(document.body, {childList: true, subtree: true});
    resize = new ResizeObserver(schedule);
    window.addEventListener('resize', schedule); window.addEventListener('scroll', schedule, true);
    window.visualViewport?.addEventListener('resize', schedule);
    document.addEventListener('keydown', keydown, true);
    document.addEventListener('focusin', focusin, true);
  }
  function dispose() {
    ++generation; cancelAnimationFrame(frame); frame = 0;
    observer?.disconnect(); describeTarget(null); resize?.disconnect();
    window.removeEventListener('resize', schedule); window.removeEventListener('scroll', schedule, true);
    window.visualViewport?.removeEventListener('resize', schedule);
    document.removeEventListener('keydown', keydown, true); document.removeEventListener('focusin', focusin, true);
    const app = document.querySelector('.app'); if (app) app.inert = savedInert;
    holdMenu(false);
    root?.remove(); root = card = null; busy = false;
    if (visible(previousFocus)) previousFocus.focus({preventScroll: true});
  }
  function exit(status = 'skipped') {
    const again = replay;
    persist(status).catch(() => {}); dispose();
    if (status === 'completed') {
      const assistant = findAgent('ordubashy');
      // The tour ends in Chat, ready for the first message; open the agents view first in case it was left.
      if (assistant) Promise.resolve(state.scope === 'global' && state.globalTab === 'agents' ? null : selectGlobal('agents'))
        .then(() => waitFor(() => document.querySelector('.global-agents')?.openAgent))
        .then(() => globalAgentsNavigate(assistant.agent_id, {chat: true}))
        .then(() => sendHello(greet === assistant.agent_id))
        .catch(() => {});
      toast(unanswered ? 'Walkthrough finished. Connect Claude Code or Codex in Settings, then say hello to Ordubashy in Chat.'
        : again ? 'Walkthrough finished.' : 'You’re all set. Take the walkthrough again anytime from Settings.');
    } else {
      toast(again ? 'Walkthrough closed.' : 'Walkthrough skipped. Take it anytime from Settings.');
    }
  }
  // Sends the typed hello when the owner left it as it is; otherwise only focuses the composer for their own first message.
  async function sendHello(send) {
    // terminals-begin
    if (send && terminalsOn()) send = onboardingPlan.helloLive(await readChat(findAgent('ordubashy')?.agent_id));
    // terminals-end
    const input = await waitFor(() => document.querySelector('#global-agent-chat .gchat-composer textarea:not(:disabled)'), 10000);
    if (!send || ![HELLO, ''].includes(input.value.trim())) { input.focus({preventScroll: true}); return; }
    input.value = HELLO; input.dispatchEvent(new Event('input', {bubbles: true}));
    const button = await waitFor(() => { const b = input.form?.querySelector('.gchat-send'); return b && !b.disabled && b; }, 4000);
    if (input.value === HELLO) input.form.requestSubmit(button);
  }
  // Esc: the welcome, the menu and a replay close as they are; a first walkthrough keeps its place and offers to continue
  // the next time the dashboard opens. Only skip and done record an outcome.
  function pause() {
    if (welcome || replay) { dispose(); return; }
    persist('in_progress').catch(() => {}); dispose();
    toast('Walkthrough paused. It picks up here next time.');
  }
  function focusables() {
    const selector = 'button:not(:disabled),a[href],input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex="0"]';
    return [...new Set([...(target?.matches(selector) ? [target] : []), ...(target?.querySelectorAll(selector) || []), ...card.querySelectorAll(selector)])].filter(visible);
  }
  function focusin(event) {
    if (!root || appDialog() || card.contains(event.target) || target?.contains(event.target)) return;
    card.querySelector('[data-tour-next], [data-tour-start], button')?.focus({preventScroll: true});
  }
  function keydown(event) {
    if (!root || appDialog()) return;
    // A list opened from the tour (the signoffs step's "open signoffs") closes on Esc first; the tour stays.
    if (event.key === 'Escape' && !welcome) {
      const close = document.querySelector('#agpad [data-surface-close]');
      if (visible(close)) {
        event.preventDefault(); event.stopImmediatePropagation(); close.click(); notice('');
        card?.querySelector('[data-tour-next]')?.focus({preventScroll: true});
        return;
      }
    }
    onboardingPlan.handleKey(event, {target, active: document.activeElement, focusables, exit: pause});
  }
  function schedule() { if (root && !frame) frame = requestAnimationFrame(() => { frame = 0; position(); }); }
  function position() {
    if (!root) return;
    root.classList.toggle('is-paused', appDialog());
    if (!welcome) describeTarget(stepTargetReady && visible(steps[index].target()) ? steps[index].target() : null);
    // Text size zooms the whole page (appearance.js). Everything is measured on screen, then placed in the page's own zoomed
    // pixels: `scale` is the ratio between the two, read from the tour's full-window layer.
    const frameBox = root.getBoundingClientRect(), scale = root.offsetWidth ? frameBox.width / root.offsetWidth : 1;
    const px = value => (value / scale) + 'px';
    const w = frameBox.width, h = Math.min(frameBox.height, window.visualViewport?.height || innerHeight);
    let box = target?.getBoundingClientRect();
    const pad = 7;
    let x = box ? Math.max(0, box.left - pad) : 0, y = box ? Math.max(0, box.top - pad) : 0;
    let right = box ? Math.min(w, box.right + pad) : w, bottom = box ? Math.min(h, box.bottom + pad) : 0;
    if (welcome || !box || right <= x || bottom <= y) { box = null; x = right = y = bottom = 0; }
    const rects = {top: [0,0,w,y], bottom: [0,bottom,w,h-bottom], left: [0,y,x,bottom-y], right: [right,y,w-right,bottom-y]};
    for (const [key, values] of Object.entries(rects)) {
      const el = root.querySelector(`[data-shade="${key}"]`);
      Object.assign(el.style, {left: px(values[0]), top: px(values[1]), width: px(values[2]), height: px(values[3])});
    }
    // The outline's shadow dims the screen around a rounded cutout; with no target it shrinks to the centre and dims it all.
    // The shades stay transparent and only keep clicks off the dimmed area, so the bright area remains live.
    const outline = root.querySelector('.onboarding-outline'); outline.classList.toggle('is-empty', !box);
    if (box) Object.assign(outline.style, {left: px(x), top: px(y), width: px(right-x), height: px(bottom-y)});
    else Object.assign(outline.style, {left: px(w/2), top: px(h/2), width: '0px', height: '0px'});
    const edge = 16, gap = 18, minWidth = 280;
    // The card always fits the window, whatever the text size.
    Object.assign(card.style, {maxWidth: px(w - 2*edge), maxHeight: px(h - 2*edge)});
    // A wide highlight leaves no room beside it at the card's usual width: narrow the card into the larger side gap
    // rather than covering the highlighted area.
    card.style.width = '';
    let narrowed = null;
    if (box && !welcome) {
      const natural = card.offsetWidth * scale, rightGap = w - right - gap - edge, leftGap = x - gap - edge;
      if (natural > rightGap && natural > leftGap && Math.max(rightGap, leftGap) >= minWidth) {
        narrowed = rightGap >= leftGap ? 'right' : 'left';
        card.style.width = px(Math.floor(Math.max(rightGap, leftGap)));
      }
    }
    const cw = card.offsetWidth * scale, ch = card.offsetHeight * scale;
    const clampX = v => Math.max(edge, Math.min(v, w-cw-edge));
    const clampY = v => Math.max(edge, Math.min(v, h-ch-edge));
    let cx = (w-cw)/2, cy = (h-ch)/2;
    if (box && !welcome && narrowed) { cx = narrowed === 'right' ? right + gap : x - gap - cw; cy = y; }
    else if (box && !welcome) {
      if (right + gap + cw <= w-edge) { cx=right+gap; cy=y; }
      else if (x-gap-cw >= edge) { cx=x-gap-cw; cy=y; }
      else if (bottom+gap+ch <= h-edge) { cx=x; cy=bottom+gap; }
      else if (y-gap-ch >= edge) { cx=x; cy=y-gap-ch; }
      // Nothing fits beside, below or above: the card covers the end of the highlight, so its start (usually a heading) stays readable.
      else { cx=right-cw; cy=h-ch-edge; }
    }
    Object.assign(card.style, {left: px(clampX(cx)), top: px(clampY(cy))});
  }
  // First run shows a short welcome; Walkthrough (in the agent menu and in Settings) also lists every chapter. Both work in any
  // saved state, so a skipped or finished walkthrough can always be taken again.
  function invitation(resume = 0, menu = false) {
    create(); setWelcome(true); describeTarget(null); card.classList.remove('is-wide');
    const mascot = findAgent('ordubashy');
    const intro = resume ? `<p>You stopped at <b>${chapterTitles[steps[resume].chapter]}</b>.</p>`
      : `<p>Qorqbot runs AI agents for you on this computer. This short tour introduces Ordubashy, shows you around and ends with your first conversation, in ${onboardingPlan.chapters(steps).length} chapters. Skip any of it, or take it again anytime from Settings.</p>`;
    const promise = `<p class="onboarding-private"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6"/></svg><span><b>Private by design.</b> Your work stays on this computer. Qorqbot only knows your email and that it is in use.</span></p>`;
    const list = menu ? `<div class="onboarding-chapters" aria-label="chapters">${onboardingPlan.chapters(steps).map((c, i) =>
      `<button type="button" class="onboarding-chapter-pick" data-tour-chapter-pick="${c.start}"><span>${String(i + 1).padStart(2, '0')}</span>${chapterTitles[c.id]}</button>`).join('')}</div>` : '';
    card.innerHTML = `<button class="onboarding-close" type="button" aria-label="close walkthrough">×</button>
      <div class="onboarding-mark" aria-hidden="true">${mascot ? agentGlyph(mascot, 48) : ""}</div>
      <h2 id="onboarding-title">${menu ? 'walkthrough' : 'welcome to qorqbot'}</h2>
      <p class="onboarding-notice" data-tour-notice role="status"></p>
      ${menu && !resume ? '<p>Take the whole tour, or jump to one chapter.</p>' : intro}${menu ? '' : promise}${list}
      <div class="onboarding-welcome-actions"><button type="button" class="ghost" data-tour-skip>${menu ? 'close' : 'skip'}</button>${resume ? '<button type="button" class="ghost" data-tour-restart>start over</button>' : ''}<button type="button" data-tour-start>${resume ? 'continue' : menu ? 'start' : 'next'}</button></div>`;
    // Closing the menu leaves saved progress as it was; only the first-run welcome records a skip.
    const close = () => menu ? dispose() : exit();
    card.querySelector('.onboarding-close').onclick = close; card.querySelector('[data-tour-skip]').onclick = close;
    card.querySelector('[data-tour-start]').onclick = () => showStep(resume);
    card.querySelector('[data-tour-restart]')?.addEventListener('click', () => showStep(0));
    card.querySelectorAll('[data-tour-chapter-pick]').forEach(button => button.onclick = () => showStep(Number(button.dataset.tourChapterPick)));
    position(); card.querySelector('[data-tour-start]').focus({preventScroll: true});
  }
  // terminals-begin
  const readChat = id => id ? api(`${globalChatBase(id)}?limit=20`, {signal: AbortSignal.timeout(4000)}).then(coreData).catch(() => null) : Promise.resolve(null);
  // The last step when agents run in terminals: it never depends on the client checks, only on Ordubashy being live and able to
  // receive. Otherwise it says where to start him and leaves the owner free to finish without sending.
  async function terminalHello(step, assistant, token) {
    const chat = await readChat(assistant.agent_id);
    if (!isCurrent(token)) return;
    const description = card.querySelector('#onboarding-description');
    if (!onboardingPlan.helloLive(chat)) { description.textContent = step.offline; return; }
    const written = (chat.entries || []).some(entry => entry.speaker === 'user') || (chat.inputs || []).some(item => item.origin === 'owner');
    const input = step.target()?.querySelector('textarea');
    if (!replay && !written && input && !input.disabled && [HELLO, ''].includes(input.value.trim())) {
      greet = assistant.agent_id;
      input.value = HELLO; input.dispatchEvent(new Event('input', {bubbles: true}));
      description.innerHTML = step.typed;
    }
  }
  // terminals-end
  async function prepareStep(step, token) {
    if (step.tab === 'settings') {
      if (state.scope !== 'global' || state.globalTab !== 'settings' || !document.querySelector('.settingspane')) await selectGlobal('settings');
      if (!isCurrent(token)) return;
      // Settings shows one section at a time; the browser step lives in Connections.
      if (typeof settingsShowSection === 'function') settingsShowSection('connections');
      await waitFor(() => visible(step.target()) && step.target(), 10000);
      // Instant, so the spotlight is placed on where the area ends up rather than mid-scroll.
      if (isCurrent(token)) step.target().scrollIntoView({block: 'center', behavior: 'auto'});
      return;
    }
    // A standalone step dims the whole screen. Every other step opens its agent view first, because its target (Chat's
    // composer, for example) may not exist until then.
    if (step.standalone) {
      if (step.id === 'privacy' && (state.scope !== 'global' || state.globalTab !== 'settings')) await selectGlobal('settings');
      return;
    }
    if (state.scope !== 'global' || state.globalTab !== 'agents' || !document.querySelector('.global-agents')) await selectGlobal('agents');
    if (!isCurrent(token)) return;
    await waitFor(() => document.querySelector('.global-agents')?.openAgent);
    if (!isCurrent(token)) return;
    const assistant = findAgent(step.agent || 'ordubashy');
    if (!assistant) throw new Error('This agent isn’t available yet. You can continue or skip.');
    // Select only; this never starts anything.
    const opened = await globalAgentsNavigate(assistant.agent_id, step.view ? {view: step.view} : {});
    if (!isCurrent(token)) return;
    if (!opened) throw new Error('This view could not open. Close the walkthrough, finish or close any open edit, then try again.');
    if (opened && step.view && _agpad?.workspaceFrame) await _agpad.workspaceFrame.workspaceReady;
    if (!isCurrent(token)) return;
    if (step.menu) holdMenu(true);
    await waitFor(() => visible(step.target()) && step.target());
    // The last step asks for the first message. If neither Claude Code nor Codex is connected nothing could answer it, so the step
    // says so and offers Settings instead. On the first walkthrough, while the owner has never written to him, it types the message
    // for them and done sends it; that works whether he is asleep or off, because sending turns him on.
    if (step.hello) {
      // terminals-begin
      if (terminalsOn()) { await terminalHello(step, assistant, token); return; }
      // terminals-end
      const read = path => api(path, {signal: AbortSignal.timeout(4000)}).then(coreData).catch(() => null);
      const [chat, clients, options] = await Promise.all([read(`${globalChatBase(assistant.agent_id)}?limit=20`), read('/root/agent-clients'),
        read(`${globalChatBase(assistant.agent_id)}/options`)]);
      if (!isCurrent(token)) return;
      const ready = onboardingPlan.helloReadiness(clients, options);
      const hint = card.querySelector('.onboarding-hint');
      if (ready.state !== 'ready') {
        unanswered = true;
        // What is missing, said for the clients as they are now (the step's own text is the fallback).
        card.querySelector('#onboarding-description').textContent = onboardingPlan.helloMissing(clients, ready)
          || (ready.state === 'chosen' ? step.setup.chosen(agentClients.NAMES[ready.client]) : step.setup.none);
        if (hint) hint.remove();
        const open = card.querySelector('[data-tour-settings]'); if (open) open.hidden = false;
        return;
      }
      const written = !chat || (chat.entries || []).some(entry => entry.speaker === 'user')
        || (chat.inputs || []).some(item => item.origin === 'owner');
      const input = step.target()?.querySelector('textarea');
      if (!replay && !written && input && !input.disabled && [HELLO, ''].includes(input.value.trim())) {
        greet = assistant.agent_id;
        input.value = HELLO; input.dispatchEvent(new Event('input', {bubbles: true}));
        card.querySelector('#onboarding-description').innerHTML = step.typed;
      }
    }
  }
  // Leaves the tour for Settings, Model/Provider, where Claude Code and Codex get connected. Progress stays on this step, as with Esc. The tour
  // steps aside completely (nothing of it stays on the screen to block a click) and comes back to this same step when the owner
  // leaves Settings or a client becomes ready, whichever is first.
  let comeBack = 0;
  const stopWatching = () => { if (comeBack) { clearInterval(comeBack); comeBack = 0; } };
  async function openSettings() {
    if (!replay) persist('in_progress').catch(() => {});
    const last = steps.length - 1;
    dispose();
    toast('Connect Claude Code or Codex here. The walkthrough comes back to this step when you are done.');
    try { await selectGlobal('settings'); if (typeof settingsShowSection === 'function') settingsShowSection('agents'); } catch (_) { /* Settings opens from the menu too. */ }
    stopWatching();
    comeBack = setInterval(async () => {
      if (root) { stopWatching(); return; }                              // the tour is on screen again some other way
      if (document.hidden || _modal) return;
      const inSettings = state.scope === 'global' && state.globalTab === 'settings';
      if (inSettings) {                                                  // still in Settings: back only once a client is ready
        const clients = await api('/root/agent-clients', {signal: AbortSignal.timeout(4000)}).then(coreData).catch(() => null);
        if (!clients || onboardingPlan.helloReadiness(clients).state !== 'ready') return;
      }
      stopWatching();
      if (root) return;
      index = last; create(); await showStep(last);
    }, 2500);
  }
  async function showStep(next) {
    if (busy) return;
    busy = true;
    const token = ++generation;
    const motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (motion && card.childElementCount) {
      await card.animate([{opacity: 1}, {opacity: 0}], {duration: 120, easing: 'ease-out'}).finished.catch(() => {});
      if (!isCurrent(token)) return;
    }
    if (openedList) { const close = document.querySelector('#agpad [data-surface-close]'); if (visible(close)) close.click(); openedList = false; }
    index = Math.max(0, Math.min(next, steps.length-1)); const step = steps[index]; greet = null; unanswered = false;
    if (!step.menu) holdMenu(false);
    card.style.opacity = '0';
    stepTargetReady = false; setWelcome(false); describeTarget(null); card.classList.toggle('is-wide', !!step.wide);
    persist('in_progress').catch(() => {});
    const chapter = onboardingPlan.chapterAt(steps, index), following = onboardingPlan.nextChapter(steps, index);
    card.innerHTML = `<div class="onboarding-step-head"><span class="onboarding-chapter">${chapterTitles[chapter.id]}</span><span class="onboarding-count">${String(chapter.number).padStart(2,'0')} <span>/ ${String(chapter.total).padStart(2,'0')}</span></span></div>
      <div class="onboarding-progress" aria-label="chapter ${chapter.number} of ${chapter.total}, step ${chapter.step} of ${chapter.count}">${steps.slice(chapter.start, chapter.end + 1).map((_,i)=>`<i class="${chapter.start+i<=index?'done':''}"></i>`).join('')}</div>
      <h2 id="onboarding-title">${step.title}</h2><p id="onboarding-description">${step.body}</p>${step.instructions ? `<ol class="onboarding-instructions">${step.instructions.map(text => `<li>${text}</li>`).join('')}</ol>` : ''}${step.hint ? `<p class="onboarding-hint">${step.hint}</p>` : ''}
      ${step.example?`<div class="onboarding-example" aria-label="Example signoff"><span class="onboarding-example-tag">Example</span><p class="onboarding-example-from">${esc(step.example.agent)} asks</p><p class="onboarding-example-title">${esc(step.example.title)}</p><p class="onboarding-example-detail">${esc(step.example.detail)}</p><div class="onboarding-example-actions"><span>Approve</span><span>Decline</span></div></div>`:''}
      ${step.action?`<button type="button" class="onboarding-action" data-tour-action disabled>${step.action}<span aria-hidden="true">↗</span></button>`:''}
      ${step.hello?'<button type="button" class="onboarding-action" data-tour-settings hidden>open Settings &gt; Model/Provider<span aria-hidden="true">→</span></button>':''}
      <p class="onboarding-notice" data-tour-notice role="status">Opening this part of your workspace…</p>
      <div class="onboarding-footer"><div><button type="button" class="ghost onboarding-skip" data-tour-skip>skip</button>${following >= 0 ? '<button type="button" class="ghost onboarding-skip" data-tour-chapter>skip chapter</button>' : ''}</div><div><button type="button" class="ghost" data-tour-back ${index?'':'disabled'}>back</button><button type="button" data-tour-next disabled>${index===steps.length-1?'done':'next'}</button></div></div>`;
    card.querySelector('[data-tour-skip]').onclick = () => exit();
    card.querySelector('[data-tour-back]').onclick = () => showStep(index-1);
    const skipChapter = card.querySelector('[data-tour-chapter]'); if (skipChapter) skipChapter.onclick = () => showStep(following);
    card.querySelector('[data-tour-next]').onclick = () => index===steps.length-1 ? exit('completed') : showStep(index+1);
    card.querySelector('[data-tour-settings]')?.addEventListener('click', openSettings);
    card.querySelector('[data-tour-action]')?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true;
      try {
        const result = await step.run();
        openedList = true;
        if (!isCurrent(token)) return;
        notice(result || 'Opened. Continue when you’re ready.');
      } catch (_) { if (isCurrent(token)) notice('This view couldn’t open. You can continue or try again.'); }
      finally { if (isCurrent(token)) { button.disabled = false; schedule(); } }
    });
    try { await prepareStep(step, token); if (isCurrent(token)) { stepTargetReady = true; notice(''); } }
    catch (error) { if (isCurrent(token)) notice(error.message || 'This area is unavailable. You can still continue.'); }
    finally {
      if (isCurrent(token)) {
        busy = false; card.querySelector('[data-tour-next]').disabled = false;
        const action = card.querySelector('[data-tour-action]'); if (action) action.disabled = !stepTargetReady;
        position(); card.style.opacity = '1';
        if (motion) card.animate([{opacity: 0, transform: 'translateY(5px)'}, {opacity: 1, transform: 'translateY(0)'}], {duration: 260, easing: 'ease-out'});
        (card.querySelector('[data-tour-settings]:not([hidden])') || card.querySelector('[data-tour-next]')).focus({preventScroll: true});
      }
    }
  }
  return {
    async boot() {
      if (booted || agentWorkspaceEmbedded()) return; booted = true;
      try {
        saved = coreData(await api('/root/onboarding'));
        if (!['new','in_progress'].includes(saved.status) || root) return;
        await waitFor(() => document.querySelector('.global-agents,.settingspane'));
        if (root || _modal) return;
        if (!root && !_modal) invitation(onboardingPlan.resumeIndex(steps, saved));
      } catch (_) { /* A walkthrough never blocks loading the app. Settings can retry. */ }
    },
    async start(chapter = null) {
      if (agentWorkspaceEmbedded()) return;
      if (root) dispose();
      const first = chapter ? Math.max(0, steps.findIndex(step => step.chapter === chapter)) : 0;
      index = first; create(); await showStep(first);
    },
    // Walkthrough in the agent menu and the desktop app's Help menu. Offers to continue an unfinished walkthrough.
    async open() {
      if (agentWorkspaceEmbedded()) return;
      try { saved = coreData(await api('/root/onboarding')); } catch (_) { /* Open the menu even if progress can't be read. */ }
      if (root) dispose();
      invitation(onboardingPlan.resumeIndex(steps, saved), true);
    },
    chapters: () => onboardingPlan.chapters(steps).map(c => ({id: c.id, title: chapterTitles[c.id]})),
  };
})();
