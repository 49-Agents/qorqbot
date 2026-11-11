// Recorded focus, other agents and the full Todo list above the native chat composer.
// Workflow focus keeps its canonical steps; only the native session controls the working dots.
const WORKFLOW_PANEL_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const WORKFLOW_PANEL_STATE = {completed: 'done', active: 'working', pending: 'open'};
const WORKFLOW_PANEL_TEXT = {completed: 'Done', active: 'Working', pending: 'Upcoming'};

function chatFocusPanelMount(composer, agentId, {visible, onChange, workingTag}) {
  const panel = document.createElement('section');
  panel.className = 'wfpanel'; panel.setAttribute('aria-label', 'Agent focus and Todo');
  panel.innerHTML = '<div class="wfpanel-context-row"><div class="gchat-context" tabindex="0" aria-label="Agent focus"><dl><dt>Focus</dt><dd class="gchat-focus-label">Loading…</dd></dl></div></div>';
  const head = document.createElement('button');
  head.type = 'button'; head.className = 'quiet wfpanel-head';
  const name = document.createElement('span'); name.className = 'wfpanel-name';
  const chevron = document.createElement('span'); chevron.className = 'wfpanel-chevron'; chevron.innerHTML = WORKFLOW_PANEL_ICON;
  head.append(name, chevron);
  const list = document.createElement('ol'); list.className = 'wfpanel-steps'; list.tabIndex = 0; list.setAttribute('aria-label', 'Todo');
  const expanded=document.createElement('div');expanded.className='wfpanel-expanded';expanded.tabIndex=0;
  const peers=document.createElement('section');peers.className='wfpanel-agents';peers.setAttribute('aria-label','Other agents');peers.hidden=true;
  const todoHeading=document.createElement('div');todoHeading.className='wfpanel-list-heading';todoHeading.textContent='Todo';
  expanded.append(peers,todoHeading,list);panel.append(head,expanded);
  composer.before(panel);
  const resize=document.createElement('div');resize.className='wfpanel-resize';resize.tabIndex=0;
  resize.setAttribute('role','separator');resize.setAttribute('aria-orientation','horizontal');resize.setAttribute('aria-label','Resize Todo height');
  resize.title='Drag to resize Todo. Double-click to reset.';panel.prepend(resize);
  const heightKey='chat-todo-height';let requestedHeight=null,drag=null;
  try {const saved=Number(localStorage.getItem(heightKey));if(Number.isFinite(saved) && saved>0)requestedHeight=saved;}catch(_){}
  const limits=()=>{const max=Math.max(80,Math.min(600,panel.parentElement.getBoundingClientRect().height*.65));return {min:Math.min(120,max),max};};
  const applyHeight=()=>{
    const {min,max}=limits();
    if(requestedHeight!==null){panel.style.setProperty('--wfpanel-height',Math.min(max,Math.max(min,requestedHeight))+'px');panel.style.setProperty('--wfpanel-max-height','min(65%,600px)');}
    else {panel.style.removeProperty('--wfpanel-height');panel.style.removeProperty('--wfpanel-max-height');}
    resize.setAttribute('aria-valuemin',String(Math.round(min)));resize.setAttribute('aria-valuemax',String(Math.round(max)));
    resize.setAttribute('aria-valuenow',String(Math.round(panel.getBoundingClientRect().height)));
  };
  const saveHeight=()=>{try{if(requestedHeight===null)localStorage.removeItem(heightKey);else localStorage.setItem(heightKey,String(requestedHeight));}catch(_){}};
  const setHeight=value=>{const {min,max}=limits();requestedHeight=Math.min(max,Math.max(min,value));applyHeight();onChange();};
  resize.onpointerdown=event=>{
    if(event.button!==0 || !event.isPrimary)return;
    event.preventDefault();drag={id:event.pointerId,y:event.clientY,height:panel.getBoundingClientRect().height};
    resize.setPointerCapture(event.pointerId);panel.classList.add('wfpanel-resizing');
  };
  resize.onpointermove=event=>{if(drag?.id===event.pointerId)setHeight(drag.height+drag.y-event.clientY);};
  const finishResize=event=>{
    if(drag?.id!==event.pointerId)return;
    drag=null;panel.classList.remove('wfpanel-resizing');if(resize.hasPointerCapture(event.pointerId))resize.releasePointerCapture(event.pointerId);saveHeight();
  };
  resize.onpointerup=finishResize;resize.onpointercancel=finishResize;resize.onlostpointercapture=finishResize;
  resize.ondblclick=()=>{requestedHeight=null;applyHeight();saveHeight();onChange();};
  resize.onkeydown=event=>{
    if(!['ArrowUp','ArrowDown','Home','End'].includes(event.key))return;
    event.preventDefault();const {min,max}=limits(),height=panel.getBoundingClientRect().height;
    setHeight(event.key==='Home'?min:event.key==='End'?max:height+(event.key==='ArrowUp'?24:-24));saveHeight();
  };
  const sizeObserver=new ResizeObserver(applyHeight);sizeObserver.observe(panel.parentElement);applyHeight();
  const todoKey = 'agent-todo-preview:' + agentId;
  const mobile = matchMedia('(max-width: 700px)');
  const workingHost = workingTag.parentElement, contextRow = panel.querySelector('.wfpanel-context-row');
  // Keep the same native-session indicator; mobile still needs it when the pane is hidden.
  const placeWorkingTag = () => { if (mobile.matches) workingHost.prepend(workingTag); else contextRow.append(workingTag); };
  placeWorkingTag();
  let open = true, disposed = false, pending = null, last = 0, attention = null, loaded = false, focusError = false, todoError = false;
  let todos = uiReads.peek(todoKey)?.items || null;
  const show = () => { panel.hidden = false; };
  let peerVersion=0,peerPending=null,peerAgents=[];
  const peerData=new Map();
  const peersVisible=()=>!disposed && open && !mobile.matches && !document.hidden && visible();
  const paintPeers=()=>{
    const focusedAgents=peerAgents.filter(agent=>{
      const attention=peerData.get(agent.agent_id)?.attention;
      return attention?.state==='focused' && attention.focus && !attention.focus.unavailable;
    });
    peers.hidden=!focusedAgents.length;
    peers.replaceChildren(...focusedAgents.map(agent=>{
      const row=document.createElement('div');row.className='wfpanel-agent';
      const data=peerData.get(agent.agent_id),attention=data?.attention,focus=attention?.focus;
      const text=focus.label || attention.task?.label || 'Focused';
      const label=agent.display_name || agent.name || 'Agent';
      row.innerHTML=identityAvatar('agent',label,agent);
      const summary=document.createElement('span');summary.textContent=String(text).replace(/\s+/g,' ').trim();row.append(summary);
      row.title=label+': '+summary.textContent;row.setAttribute('aria-label',row.title);return row;
    }));
    onChange();
  };
  const refreshPeers=()=>{
    if(!peersVisible() || typeof loadGlobalAgentProfiles!=='function')return Promise.resolve();
    if(peerPending)return peerPending;
    const version=++peerVersion,current=()=>version===peerVersion && peersVisible();
    peerPending=(async()=>{
      try {
        const agents=await loadGlobalAgentProfiles();if(!current())return;
        peerAgents=agents.filter(agent=>agent.agent_id!==agentId && globalRosterAgent(agent) && ['ordubashy','global-worker'].includes(agent.role?.id || agent.role_id));
        for(const agent of peerAgents){const cached=uiReads.peek(agentFocusKey(agent.agent_id));if(cached)peerData.set(agent.agent_id,cached);}
        paintPeers();
        for(let i=0;i<peerAgents.length;i+=3){
          if(!current())return;
          await Promise.all(peerAgents.slice(i,i+3).map(async agent=>{
            try {const data=await agentFocusRead(agent.agent_id);if(current())peerData.set(agent.agent_id,data);}
            catch(error){if(current() && error?.name!=='AbortError')peerData.set(agent.agent_id,null);}
          }));
          if(current())paintPeers();
        }
      } catch(error) {
        if(current()){peers.hidden=false;peers.textContent='Agent focus unavailable';}
      } finally {if(version===peerVersion)peerPending=null;}
    })();return peerPending;
  };

  const paint = () => {
    const focus = attention?.focus, focused = attention?.state === 'focused' && focus && !focus.unavailable;
    panel.querySelector('.gchat-focus-label').textContent = !loaded ? 'Loading…' : focusError || focus?.unavailable ? 'Focus unavailable'
      : focused ? focus.label || attention.task?.label || 'Focused' : 'No focus set';
    const workflow = focused && focus.type === 'workflow' && !attention.workflow?.unavailable ? attention.workflow : null;
    name.textContent = 'Agents and Todo';
    head.setAttribute('aria-expanded', String(open));
    head.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + name.textContent.toLowerCase());
    panel.classList.toggle('wfpanel-collapsed', !open);
    expanded.hidden = !open;resize.hidden=!open;
    const workflowSteps = workflow?.steps || [];
    const todoItems = attentionBoardGroups(todos || []).flatMap(group => group.items);
    const steps = [...workflowSteps, ...todoItems.map(item => ({label: item.task.title, status: item.task.status}))];
    const rows = steps.map(step => {
      const blocked = !!(step.current && workflow?.blocked);
      const row = document.createElement('li'); row.className = 'wfpanel-step';
      if (step.current) row.setAttribute('aria-current', 'step');
      const status = blocked ? 'Blocked' : step.current && workflow?.paused ? 'Paused' : WORKFLOW_PANEL_TEXT[step.status] || step.status;
      row.setAttribute('aria-label', step.label + ': ' + status);
      const ball = document.createElement('span');
      ball.className = 'ball agfocus-task-state ' + (blocked ? 'blocked' : WORKFLOW_PANEL_STATE[step.status] || step.status || 'open');
      ball.setAttribute('aria-hidden', 'true'); ball.innerHTML = '<span class="core"></span>';
      const label = document.createElement('span'); label.className = 'wfpanel-label'; label.textContent = step.label;
      row.append(ball, label);
      if (blocked && workflow.blocker_reason) {
        const reason = document.createElement('p'); reason.className = 'wfpanel-error'; reason.textContent = workflow.blocker_reason; row.append(reason);
      }
      return row;
    });
    if (workflowSteps.length) {
      const heading = text => { const row = document.createElement('li'); row.className = 'wfpanel-list-heading'; row.textContent = text; return row; };
      rows.splice(workflowSteps.length, 0, heading('Todo'));
      rows.unshift(heading('Workflow steps'));
    }
    if (!todos?.length) {
      const empty = document.createElement('li'); empty.className = 'wfpanel-empty';
      empty.textContent = todoError ? 'Todo unavailable' : todos ? 'No tasks on Todo.' : 'Loading Todo…'; rows.push(empty);
    }
    const scrollTop = expanded.scrollTop;
    list.replaceChildren(...rows);
    expanded.scrollTop = scrollTop;
    show(); onChange();
  };
  const receive = data => {
    if (disposed) return;
    attention = data?.attention || null; loaded = true; focusError = !data; paint();
  };
  const refresh = (force = false) => {
    if (disposed || mobile.matches || document.hidden || !visible() || !force && Date.now() - last < 12000) return Promise.resolve();
    if (pending) return pending;
    last = Date.now();
    // Focus can paint immediately from its shared snapshot while Todo pages load.
    pending = Promise.all([
      refreshPeers(),
      agentFocusRead(agentId, {force}).then(receive, () => receive(null)),
      uiReads.read(todoKey, async () => {
        const items = [], seen = new Set();
        let cursor = null;
        do {
          if (disposed || mobile.matches || !visible()) throw Error('Todo hidden');
          const data = coreData(await post('/root/operations/todo_get', {scope: 'global', agent_id: agentId, projection: 'summary', limit: 100, ...(cursor ? {cursor} : {})}));
          if (data?.agent_id !== agentId || !Array.isArray(data.items)) throw Error('Todo unavailable');
          items.push(...data.items); cursor = data.next_cursor;
          if (cursor && seen.has(cursor)) throw Error('Todo pagination did not advance');
          if (cursor) seen.add(cursor);
        } while (cursor);
        return {items};
      }, {force}).then(data => {
        if (disposed) return;
        todos = data.items; todoError = false; paint();
      }, () => {
        if (disposed) return;
        todos = null; todoError = true; paint();
      }),
    ]).finally(() => { pending = null; });
    return pending;
  };
  const unsubscribe = uiReads.subscribe(agentFocusKey(agentId), receive);
  const cached = uiReads.peek(agentFocusKey(agentId));
  if (cached) receive(cached); else paint();
  head.onclick = () => { open = !open;peerVersion++;peerPending=null;paint();if(open)void refreshPeers(); };
  queueMicrotask(() => { void refresh(); });
  const timer = setInterval(() => { void refresh(); }, 12000);
  const onVisible = () => { if (!document.hidden) void refresh(); };
  const onResize = () => { placeWorkingTag(); onChange(); if (!mobile.matches) void refresh(true); };
  document.addEventListener('visibilitychange', onVisible);
  mobile.addEventListener('change', onResize);
  return {
    refresh, show,
    dispose() { disposed = true;peerVersion++; sizeObserver.disconnect(); unsubscribe(); clearInterval(timer); document.removeEventListener('visibilitychange', onVisible); mobile.removeEventListener('change', onResize); workingHost.prepend(workingTag); panel.remove(); },
  };
}
