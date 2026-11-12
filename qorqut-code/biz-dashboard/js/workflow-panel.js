// Ordubashy's recorded focus above the composer; expand to see the full active agent roster.
// Only focus projections are read. The working dots always belong to the chat's native session.
const WORKFLOW_PANEL_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

function chatFocusPanelMount(composer, agentId, {agent, visible, onChange, workingTag}) {
  const panel = document.createElement('section');
  panel.className = 'wfpanel'; panel.setAttribute('aria-label', 'Agent focus board');
  panel.innerHTML = '<div class="wfpanel-context-row"><div class="gchat-context" tabindex="0" aria-label="Agent focus"><dl><dt class="wfpanel-focus-icon" aria-label="Focus"></dt><dd class="gchat-focus-label">Loading…</dd></dl></div></div>';
  const contextRow = panel.querySelector('.wfpanel-context-row');
  const head = document.createElement('button');
  head.type = 'button'; head.className = 'quiet wfpanel-head';
  const chevron = document.createElement('span'); chevron.className = 'wfpanel-chevron'; chevron.innerHTML = WORKFLOW_PANEL_ICON;
  head.append(chevron); contextRow.append(head);
  const expanded=document.createElement('div');expanded.className='wfpanel-expanded';expanded.tabIndex=0;
  const intent=document.createElement('p');intent.className='gchat-focus-intent';intent.hidden=true;
  const peers=document.createElement('section');peers.className='wfpanel-agents';peers.setAttribute('aria-label','Other agents');peers.hidden=true;
  expanded.append(intent,peers);panel.append(expanded);
  composer.before(panel);
  const resize=document.createElement('div');resize.className='wfpanel-resize';resize.tabIndex=0;
  resize.setAttribute('role','separator');resize.setAttribute('aria-orientation','horizontal');resize.setAttribute('aria-label','Resize focus board height');
  resize.title='Drag to resize the focus board. Double-click to reset.';panel.prepend(resize);
  const heightKey='chat-focus-height';let requestedHeight=null,drag=null;
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
  const mobile = matchMedia('(max-width: 700px)');
  const workingHost = workingTag.parentElement;
  const role = profile => profile?.role?.id || profile?.role_id;
  const supportsFocus = profile => ['ordubashy','global-worker'].includes(role(profile));
  let primary = null, unsubscribe = () => {};
  let open = true, disposed = false, last = 0, attention = null, loaded = false, focusError = false;
  const active = () => !disposed && !mobile.matches && !document.hidden && visible();
  // A worker chat must never put its own working dots beside Ordubashy's focus.
  const placeWorkingTag = () => {
    if (mobile.matches || primary?.agent_id !== agentId) workingHost.prepend(workingTag);
    else contextRow.insertBefore(workingTag, head);
  };
  const show = () => { panel.hidden = false; };
  let peerVersion=0,peerPending=null,peerAgents=[];
  const peerData=new Map();
  const paintPeers=()=>{
    const scrollTop=expanded.scrollTop;
    peers.hidden=!peerAgents.length;
    peers.replaceChildren(...peerAgents.map(agent=>{
      const row=document.createElement('div');row.className='wfpanel-agent';row.dataset.agentId=agent.agent_id;
      const data=peerData.get(agent.agent_id),attention=data?.attention,focus=attention?.focus;
      const focused=attention?.state==='focused' && focus && !focus.unavailable;
      const text=!supportsFocus(agent) ? 'No recorded focus' : !peerData.has(agent.agent_id) ? 'Loading…'
        : !attention || focus?.unavailable ? 'Focus unavailable' : focused ? focus.label || attention.task?.label || 'Focused' : 'No focus set';
      row.classList.toggle('wfpanel-agent-unfocused',!focused);
      const label=agent.display_name || agent.name || 'Agent';
      row.innerHTML=identityAvatar('agent',label,agent);
      const summary=document.createElement('span');summary.className='wfpanel-agent-summary';
      const name=document.createElement('span');name.className='wfpanel-agent-name';name.textContent=label;
      const focusText=document.createElement('span');focusText.className='wfpanel-agent-focus';focusText.textContent=String(text).replace(/\s+/g,' ').trim();
      summary.append(name,document.createTextNode(' · '),focusText);row.append(summary);
      row.setAttribute('aria-label',label+': '+focusText.textContent);return row;
    }));
    expanded.scrollTop=scrollTop;
    onChange();
  };
  const refreshPeers=(force=false)=>{
    if(!active() || !open && primary || typeof loadGlobalAgentProfiles!=='function')return Promise.resolve();
    if(peerPending)return peerPending;
    const version=++peerVersion,current=()=>version===peerVersion && active();
    peerPending=(async()=>{
      try {
        const profiles=await loadGlobalAgentProfiles();if(!current())return;
        const agents=profiles.filter(globalRosterAgent);
        const changed=setPrimary(agents.find(agent=>role(agent)==='ordubashy') || null);
        if(!primary){loaded=true;focusError=true;paint();}
        // Selecting Ordubashy never changes which agent receives chat input.
        const primaryRead=changed ? refreshPrimary(force) : Promise.resolve();
        peerAgents=agents.filter(agent=>agent.agent_id!==primary?.agent_id);
        const keep=new Set(peerAgents.map(agent=>agent.agent_id));
        for(const id of peerData.keys())if(!keep.has(id))peerData.delete(id);
        for(const agent of peerAgents){const cached=uiReads.peek(agentFocusKey(agent.agent_id));if(cached)peerData.set(agent.agent_id,cached);}
        if(!open){await primaryRead;return;}
        paintPeers();
        const readable=peerAgents.filter(supportsFocus);
        for(let i=0;i<readable.length;i+=3){
          if(!current() || !open)break;
          await Promise.all(readable.slice(i,i+3).map(async agent=>{
            try {const data=await agentFocusRead(agent.agent_id,{force});if(current() && open)peerData.set(agent.agent_id,data);}
            catch(error){if(current() && open && error?.name!=='AbortError')peerData.set(agent.agent_id,null);}
          }));
          if(current() && open)paintPeers();
        }
        await primaryRead;
      } catch(error) {
        if(current()){
          if(!primary){loaded=true;focusError=true;paint();}
          if(open){peers.hidden=false;peers.textContent='Agent focus unavailable';onChange();}
        }
      } finally {if(version===peerVersion)peerPending=null;}
    })();return peerPending;
  };

  const paint = () => {
    const focus = attention?.focus, focused = attention?.state === 'focused' && focus && !focus.unavailable;
    panel.querySelector('.gchat-focus-label').textContent = !loaded ? 'Loading…' : !primary ? 'Ordubashy unavailable' : focusError || focus?.unavailable ? 'Focus unavailable'
      : focused ? focus.label || attention.task?.label || 'Focused' : 'No focus set';
    intent.textContent=focused && !focusError && typeof attention.intent==='string' ? attention.intent.trim() : '';
    intent.hidden=!intent.textContent;
    head.setAttribute('aria-expanded', String(open));
    head.setAttribute('aria-label', (open ? 'Collapse' : 'Expand') + ' focus details');
    panel.classList.toggle('wfpanel-collapsed', !open);
    expanded.hidden = !open;resize.hidden=!open;
    show(); onChange();
  };
  const receive = data => {
    if (disposed) return;
    attention = data?.attention || null; loaded = true; focusError = !data; paint();
  };
  const setPrimary = profile => {
    const previous=primary?.agent_id;
    primary=profile;
    const identity=primary || {display_name:'Ordubashy',role:{id:'ordubashy'}};
    panel.querySelector('.wfpanel-focus-icon').innerHTML=identityAvatar('agent',identity.display_name || identity.name || 'Ordubashy',identity);
    placeWorkingTag();
    if(previous===primary?.agent_id)return false;
    unsubscribe();unsubscribe=()=>{};
    attention=null;loaded=!primary;focusError=!primary;
    if(primary){
      const id=primary.agent_id;
      unsubscribe=uiReads.subscribe(agentFocusKey(id),data=>{if(primary?.agent_id===id)receive(data);});
      const cached=uiReads.peek(agentFocusKey(id));
      if(cached)receive(cached);else paint();
    }else paint();
    return true;
  };
  const refreshPrimary = (force=false) => {
    const id=primary?.agent_id;
    if(!id)return Promise.resolve();
    return agentFocusRead(id,{force}).then(
      data=>{if(primary?.agent_id===id)receive(data);},
      ()=>{if(primary?.agent_id===id)receive(null);});
  };
  const refresh = (force = false) => {
    if (!active() || !force && Date.now() - last < 12000) return Promise.resolve();
    last = Date.now();
    // Ordubashy can paint immediately while the other agent focus reads finish.
    return Promise.all([
      refreshPrimary(force), refreshPeers(force),
    ]);
  };
  setPrimary(role(agent)==='ordubashy' ? agent : null);
  paint();
  panel.onclick = event => {
    if(event.target.closest('.wfpanel-resize'))return;
    const selection=window.getSelection();
    if(!event.target.closest('.wfpanel-head') && selection && !selection.isCollapsed && panel.contains(selection.anchorNode))return;
    open=!open;peerVersion++;peerPending=null;paint();if(open)void refreshPeers();
  };
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
