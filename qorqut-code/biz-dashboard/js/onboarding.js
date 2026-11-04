// A small owner-only walkthrough. Navigation is real; messages and CLI commands
// are never sent by the tour. Progress belongs to the installation, not a browser.
const onboarding = (() => {
  const steps = [
    {id: 'assistant', title: 'this is qorqbot, main guy',
      body: 'Give him a goal. He carries the work forward and brings decisions back to you.',
      target: () => agentButton('ordubashy')},
    {id: 'terminal', title: 'open terminal',
      body: 'Click the >_ icon at the top of your conversation, next to the chat bubble. This is your agent’s Terminal.',
      hint: 'Then press next. Or use the button below.',
      target: () => document.querySelector('#global-agent-terminal [data-terminal]'), action: 'open terminal',
      run: () => showStep(index + 1)},
    {id: 'terminal-start', progressStep: 'terminal', title: 'bring your agent online',
      body: 'Click inside Terminal, then:',
      instructions: ['Type claude or codex and press Enter.', 'Follow any sign-in or folder-trust prompts. When the agent’s input appears, type hey and press Enter.', 'If a hooks approval dialog appears, accept the hooks so Qorqbot can connect to your agent.'],
      hint: 'Wait for a reply, then press next to return to Chat. If the command is not found, install Claude Code or Codex first.',
      target: () => document.querySelector('#global-agent-terminal')},
    {id: 'chat', title: 'say it in chat',
      body: 'You’re back in Chat. Type stop here in the message box and press send.',
      hint: 'This reaches the same agent you just started in Terminal. Wait for its reply, then continue.',
      target: () => document.querySelector('#global-agent-terminal .gchat-composer')},
    {id: 'focus', title: 'keep important work close',
      body: 'Pin tasks and records here. Signoffs bring decisions back for your approval.',
      target: () => document.querySelector('#agpad'), view: 'focus'},
    {id: 'learning', title: 'make it yours',
      body: 'The Learning Applier updates shared context and how your agents work as you go.',
      target: () => agentButton('learning-applier'), action: 'meet the learning applier',
      run: async () => { const agent = findAgent('learning-applier'); if (agent) await globalAgentsNavigate(agent.agent_id); }},
  ];
  let root = null, card = null, target = null, index = 0, generation = 0, busy = false;
  let saved = null, writes = Promise.resolve(), observer = null, resize = null, frame = 0, previousFocus = null;
  let targetDescription = null, welcome = false, booted = false, savedInert = false;
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
  function persist(status, step = steps[index].progressStep || steps[index].id) {
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
    if (root) return;
    previousFocus = document.activeElement; savedInert = !!document.querySelector('.app')?.inert;
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
    root?.remove(); root = card = null; busy = false;
    if (visible(previousFocus)) previousFocus.focus({preventScroll: true});
  }
  function exit(status = 'skipped') {
    persist(status).catch(() => {}); dispose();
    if (status === 'completed') {
      const assistant = findAgent('ordubashy');
      if (assistant) globalAgentsNavigate(assistant.agent_id, {chat: true}).catch(() => {});
      toast('You’re ready. Replay the walkthrough anytime in Settings.');
    }
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
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); exit(); }
    if (event.key !== 'Tab') return;
    const list = focusables(); if (!list.length) return;
    const at = list.indexOf(document.activeElement), next = (at + (event.shiftKey ? -1 : 1) + list.length) % list.length;
    event.preventDefault(); list[next].focus();
  }
  function schedule() { if (root && !frame) frame = requestAnimationFrame(() => { frame = 0; position(); }); }
  function position() {
    if (!root) return;
    root.classList.toggle('is-paused', appDialog());
    if (!welcome) describeTarget(visible(steps[index].target()) ? steps[index].target() : null);
    const w = document.documentElement.clientWidth, h = window.visualViewport?.height || innerHeight;
    let box = target?.getBoundingClientRect();
    const pad = 7;
    let x = box ? Math.max(0, box.left - pad) : 0, y = box ? Math.max(0, box.top - pad) : 0;
    let right = box ? Math.min(w, box.right + pad) : w, bottom = box ? Math.min(h, box.bottom + pad) : 0;
    if (welcome || !box || right <= x || bottom <= y) { box = null; x = right = y = bottom = 0; }
    const rects = {top: [0,0,w,y], bottom: [0,bottom,w,h-bottom], left: [0,y,x,bottom-y], right: [right,y,w-right,bottom-y]};
    for (const [key, values] of Object.entries(rects)) {
      const el = root.querySelector(`[data-shade="${key}"]`);
      Object.assign(el.style, {left: values[0]+'px', top: values[1]+'px', width: values[2]+'px', height: values[3]+'px'});
    }
    const outline = root.querySelector('.onboarding-outline'); outline.hidden = !box;
    Object.assign(outline.style, {left:x+'px', top:y+'px', width:(right-x)+'px', height:(bottom-y)+'px'});
    const cw = card.offsetWidth, ch = card.offsetHeight, edge = 16, gap = 18;
    const clampX = v => Math.max(edge, Math.min(v, w-cw-edge));
    const clampY = v => Math.max(edge, Math.min(v, h-ch-edge));
    let cx = (w-cw)/2, cy = (h-ch)/2;
    if (box && !welcome) {
      if (right + gap + cw <= w-edge) { cx=right+gap; cy=y; }
      else if (x-gap-cw >= edge) { cx=x-gap-cw; cy=y; }
      else if (bottom+gap+ch <= h-edge) { cx=x; cy=bottom+gap; }
      else if (y-gap-ch >= edge) { cx=x; cy=y-gap-ch; }
      else { cx=(w-cw)/2; cy=(y+bottom)/2 > h/2 ? edge : h-ch-edge; }
    }
    Object.assign(card.style, {left:clampX(cx)+'px', top:clampY(cy)+'px'});
  }
  function invitation() {
    create(); setWelcome(true); describeTarget(null);
    const mascot = findAgent('ordubashy');
    card.innerHTML = `<button class="onboarding-close" type="button" aria-label="skip walkthrough">×</button>
      <div class="onboarding-mark" aria-hidden="true">${mascot ? agentGlyph(mascot, 48) : ""}</div>
      <h2 id="onboarding-title">welcome to qorqbot</h2>
      <p class="onboarding-notice" data-tour-notice role="status"></p>
      <div class="onboarding-welcome-actions"><button type="button" class="ghost" data-tour-skip>skip</button><button type="button" data-tour-start>next</button></div>`;
    card.querySelector('.onboarding-close').onclick = () => exit(); card.querySelector('[data-tour-skip]').onclick = () => exit();
    card.querySelector('[data-tour-start]').onclick = () => showStep(0);
    position(); card.querySelector('[data-tour-start]').focus({preventScroll: true});
  }
  async function prepareStep(step, token) {
    if (state.scope !== 'global' || state.globalTab !== 'agents' || !document.querySelector('.global-agents')) await selectGlobal('agents');
    if (!isCurrent(token)) return;
    await waitFor(() => document.querySelector('.global-agents')?.openAgent);
    if (!isCurrent(token)) return;
    const assistant = findAgent('ordubashy');
    if (!assistant) throw new Error('Your main assistant isn’t available yet. You can continue or skip.');
    // Select only; this never starts a terminal or client.
    const opened = await globalAgentsNavigate(assistant.agent_id, step.view ? {view: step.view} : {});
    if (!isCurrent(token)) return;
    if (!opened) throw new Error('Finish your open edit first, then continue the walkthrough.');
    if (step.id === 'terminal' || step.id === 'chat') globalAgentViewActive?.showChat();
    if (step.id === 'terminal-start') {
      if (!document.querySelector('#global-agent-terminal .gchat-terminal-host')) globalAgentViewActive?.showTerminal();
      try {
        const data = coreData(await api(`${globalChatBase(assistant.agent_id)}?limit=1`, {signal: AbortSignal.timeout(3000)}));
        if (!isCurrent(token)) return;
        if (data.session?.state === 'live') {
          card.querySelector('#onboarding-description').textContent = 'Your agent is already running. Use this session:';
          card.querySelector('.onboarding-instructions').innerHTML = '<li>Click inside Terminal, type hey and press Enter.</li><li>If a hooks approval dialog appears, accept the hooks so Qorqbot can connect to your agent.</li>';
          card.querySelector('.onboarding-hint').textContent = 'Wait for a reply, then press next to return to Chat. No need to start another agent.';
        }
      } catch (_) { /* The regular startup instructions remain usable offline. */ }
    }
    if (step.id === 'focus' && _agpad?.workspaceFrame) await _agpad.workspaceFrame.workspaceReady;
    if (!isCurrent(token)) return;
    await waitFor(() => visible(step.target()) && step.target());
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
    index = Math.max(0, Math.min(next, steps.length-1)); const step = steps[index];
    card.style.opacity = '0';
    setWelcome(false); describeTarget(null);
    persist('in_progress').catch(() => {});
    card.innerHTML = `<div class="onboarding-step-head"><span class="onboarding-count">${String(index+1).padStart(2,'0')} <span>/ ${String(steps.length).padStart(2,'0')}</span></span></div>
      <div class="onboarding-progress" aria-label="step ${index+1} of ${steps.length}">${steps.map((_,i)=>`<i class="${i<=index?'done':''}"></i>`).join('')}</div>
      <h2 id="onboarding-title">${step.title}</h2><p id="onboarding-description">${step.body}</p>${step.instructions ? `<ol class="onboarding-instructions">${step.instructions.map(text => `<li>${text}</li>`).join('')}</ol>` : ''}${step.hint ? `<p class="onboarding-hint">${step.hint}</p>` : ''}
      ${step.action?`<button type="button" class="onboarding-action" data-tour-action disabled>${step.action}<span aria-hidden="true">↗</span></button>`:''}
      <p class="onboarding-notice" data-tour-notice role="status">Opening this part of your workspace…</p>
      <div class="onboarding-footer"><button type="button" class="ghost onboarding-skip" data-tour-skip>skip</button><div><button type="button" class="ghost" data-tour-back ${index?'':'disabled'}>back</button><button type="button" data-tour-next disabled>${index===steps.length-1?'done':'next'}</button></div></div>`;
    card.querySelector('[data-tour-skip]').onclick = () => exit();
    card.querySelector('[data-tour-back]').onclick = () => showStep(index-1);
    card.querySelector('[data-tour-next]').onclick = () => index===steps.length-1 ? exit('completed') : showStep(index+1);
    card.querySelector('[data-tour-action]')?.addEventListener('click', async event => {
      const button = event.currentTarget; button.disabled = true;
      try {
        const result = await step.run();
        if (!isCurrent(token)) return;
        notice(result || 'Opened. Continue when you’re ready.');
      } catch (_) { if (isCurrent(token)) notice('This view couldn’t open. You can continue or try again.'); }
      finally { if (isCurrent(token)) { button.disabled = false; schedule(); } }
    });
    try { await prepareStep(step, token); if (isCurrent(token)) notice(''); }
    catch (error) { if (isCurrent(token)) notice(error.message || 'This area is unavailable. You can still continue.'); }
    finally {
      if (isCurrent(token)) {
        busy = false; card.querySelector('[data-tour-next]').disabled = false;
        const action = card.querySelector('[data-tour-action]'); if (action) action.disabled = false;
        position(); card.style.opacity = '1';
        if (motion) card.animate([{opacity: 0, transform: 'translateY(5px)'}, {opacity: 1, transform: 'translateY(0)'}], {duration: 260, easing: 'ease-out'});
        card.querySelector('[data-tour-next]').focus({preventScroll: true});
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
        if (!root && !_modal) invitation();
      } catch (_) { /* A walkthrough never blocks loading the app. Settings can retry. */ }
    },
    async start() {
      if (agentWorkspaceEmbedded()) return;
      if (root) dispose(); index = 0; create(); await showStep(0);
    },
  };
})();
