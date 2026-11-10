// A lightweight agent overview, independent of the selected Todo board.
// Ordubashy's overview reads focus only; other agent views need one membership summary row.
function attentionAgentsCache(p) {
  return p.attentionAgents ||= {agents:[], entries:new Map(), at:0, retryAt:0, run:null, error:null};
}
function attentionAgentsName(p, agentId) {
  const agent=p.attentionAgents?.agents.find(agent=>agent.agent_id === agentId) || globalAgentProfile(agentId);
  return agent?.display_name || agent?.name || (agentId === p.agentId ? p.agent : null) || 'Agent';
}
function attentionAgentsObserve(p, cache) {
  const focus=uiReads.peek(agentFocusKey(cache.agentId));
  const data=cache.data && focus ? {...cache.data,attention:focus.attention} : cache.data;
  attentionAgentsCache(p).entries.set(cache.agentId, {data, error:cache.error, at:Date.now()});
}
function attentionAgentsLive(p, cache) {
  return p.attentionAgents === cache && !!p.attentionBoard && p.view === 'attentionBoard' && agpadActive(p);
}
function attentionAgentsUnavailable(p, agentId, error) {
  uiReads.invalidate(agentFocusKey(agentId),{drop:true});
  const cache=attentionBoardCacheFor(p, agentId);
  cache.run=null; cache.version++; cache.data=null; cache.partial=false; cache.at=0;
  cache.error=error; cache.retryAt=Date.now()+ATTENTION_POLL_MS;
  attentionAgentsObserve(p, cache);
  if (p.attentionCache !== cache || !p.attentionBoard) return;
  const board=p.attentionBoard;
  board.data=null; board.workspace.dispose();
  board.board.querySelector('[data-attention-record]').hidden=true;
  board.board.querySelector('.agfocus-layout').classList.remove('agfocus-thread-open');
  agfocusOverlayClose(p);
  attentionBoardPaint(p);
}
function attentionAgentsRefresh(p) {
  const cache=attentionAgentsCache(p);
  if (cache.run) return cache.run.promise;
  if (!attentionAgentsLive(p, cache) || Date.now() < cache.retryAt) return Promise.resolve();
  const run={}; cache.run=run;
  run.promise=(async()=>{
    try {
      const agents=await loadGlobalAgentProfiles();
      if (!Array.isArray(agents)) throw Error('Agent list unavailable.');
      if (!attentionAgentsLive(p, cache)) return;
      cache.agents=agents.filter(agent=>globalRosterAgent(agent) && ['ordubashy','global-worker'].includes(agentRole(agent)))
        .sort((a,b)=>(agentRole(a)==='ordubashy'?0:1)-(agentRole(b)==='ordubashy'?0:1)
          || (a.display_name || a.name || '').localeCompare(b.display_name || b.name || '') || a.agent_id.localeCompare(b.agent_id));
      const active=new Set(cache.agents.map(agent=>agent.agent_id));
      for (const agentId of cache.entries.keys()) if (!active.has(agentId)) cache.entries.delete(agentId);
      for (const agentId of p.attentionCaches?.keys() || []) if (!active.has(agentId)) {
        attentionAgentsUnavailable(p, agentId, Error('This agent is no longer active.'));
        cache.entries.delete(agentId);
      }
      cache.error=null;
      const focusOnly=(p.workspaceRole || agentRole(globalAgentProfile(p.agentId))) === 'ordubashy';
      for(const agent of cache.agents) {
        const data=uiReads.peek(agentFocusKey(agent.agent_id));
        if(focusOnly && data) cache.entries.set(agent.agent_id,{data,error:null,at:Date.now()});
      }
      attentionAgentsPaint(p);
      // Bound concurrent reads even when the installation has many permanent workers. Stop at a batch boundary on leave.
      for (let offset=0; offset<cache.agents.length; offset+=3) {
        if (!attentionAgentsLive(p, cache)) return;
        const results=await Promise.allSettled(cache.agents.slice(offset,offset+3).map(async agent=>{
          const agentId=agent.agent_id, boardCache=attentionBoardCacheFor(p, agentId), version=boardCache.version;
          // Focus is independent of the selected board and its pagination.
          if (!attentionAgentsLive(p, cache) || boardCache.version !== version) return;
          if (!focusOnly && boardCache.data && !boardCache.partial && Date.now()-boardCache.at < ATTENTION_POLL_MS) {
            attentionAgentsObserve(p, boardCache); return;
          }
          const previous=cache.entries.get(agentId);
          try {
            const data=focusOnly ? await agentFocusRead(agentId) : coreData(await post('/root/operations/todo_get', {scope:'global',agent_id:agentId,projection:'summary',limit:1}));
            if (data.agent_id !== agentId || !Array.isArray(data.items)) throw Error('Agent Todo unavailable.');
            if (boardCache.version !== version || !attentionAgentsLive(p, cache)) return;
            // A full-board read or removal that completed after this request is the newer observation.
            if (cache.entries.get(agentId) !== previous) return;
            cache.entries.set(agentId, {data,error:null,at:Date.now()});
            attentionAgentsPaint(p);
          } catch (error) {
            if (boardCache.version !== version || !attentionAgentsLive(p, cache)) return;
            if (cache.entries.get(agentId) !== previous) return;
            if (accessLost(error) || error.code === 'not_found') attentionAgentsUnavailable(p, agentId, error);
            else cache.entries.set(agentId, {...cache.entries.get(agentId), error});
          }
        }));
        for (const result of results) if (result.status === 'rejected') throw result.reason;
        if (attentionAgentsLive(p, cache)) attentionAgentsPaint(p);
      }
      cache.at=Date.now(); cache.retryAt=Date.now()+ATTENTION_POLL_MS;
    } catch (error) {
      cache.error=error; cache.retryAt=Date.now()+ATTENTION_POLL_MS;
      if (accessLost(error)) {
        for (const agentId of p.attentionCaches?.keys() || []) attentionAgentsUnavailable(p, agentId, error);
        cache.entries.clear(); cache.agents=[];
      }
    } finally {
      if (cache.run === run) cache.run=null;
      if (attentionAgentsLive(p, cache)) attentionAgentsPaint(p);
    }
  })();
  return run.promise;
}
function attentionAgentsPaint(p) {
  const host=p.attentionBoard?.board.querySelector('[data-attention-agents]');
  if (!host) return;
  const cache=attentionAgentsCache(p), selected=p.attentionAgentId || p.agentId;
  const ordubashyView=(p.workspaceRole || agentRole(globalAgentProfile(p.agentId))) === 'ordubashy';
  const entries=cache.agents.map(agent=>{
    const entry=cache.entries.get(agent.agent_id);
    return {agent,...entry,data:ordubashyView ? uiReads.peek(agentFocusKey(agent.agent_id)) || entry?.data : entry?.data};
  });
  const visible=entries.filter(({agent,data})=>ordubashyView
    ? agentRole(agent) === 'ordubashy' || data?.attention?.state === 'focused' && data.attention.focus && !data.attention.focus.unavailable
    : data?.items.length);
  const failed=entries.filter(entry=>entry.error).map(entry=>entry.agent.display_name || entry.agent.name || 'Agent');
  const signature=JSON.stringify([ordubashyView,selected,visible.map(({agent,data,error})=>[agent.agent_id,agent.display_name,agent.name,agent.presentation,data?.attention,!!error]),failed,cache.error?.message,!!cache.at,!!cache.run]);
  if (host._attentionSignature === signature) return;
  host._attentionSignature=signature;
  const focused=host.contains(document.activeElement) ? document.activeElement.dataset.attentionAgent : null;
  host.replaceChildren();
  const heading=document.createElement('h2'); heading.textContent=ordubashyView ? 'Agent focus' : 'Agents with todos'; host.setAttribute('aria-label',heading.textContent); host.append(heading);
  for (const {agent,data,error} of visible) {
    const row=document.createElement('button');row.type='button';row.className='attention-agent';row.dataset.attentionAgent=agent.agent_id;
    row.setAttribute('aria-pressed',String(selected === agent.agent_id));
    const name=agent.display_name || agent.name || 'Agent';
    row.innerHTML=identityAvatar('agent',name,agent)+'<span class="attention-agent-copy"><span class="attention-agent-heading"><strong></strong><span class="attention-agent-focus"></span></span><span class="attention-agent-intent"></span></span>';
    row.querySelector('strong').textContent=name;
    const attention=data?.attention, focus=attention?.focus;
    const focused=attention?.state === 'focused' && focus && !focus.unavailable;
    row.querySelector('.attention-agent-focus').textContent=!data ? (error ? 'Focus unavailable' : 'Loading focus…')
      : focus?.unavailable ? 'Focus unavailable' : focused ? (focus.label || attention.task?.label || 'Focused') : 'No focus set';
    const intent=row.querySelector('.attention-agent-intent');
    intent.textContent=focused ? attention.intent || 'No intent recorded' : '';
    intent.hidden=!focused;
    if (error) { const stale=document.createElement('span');stale.className='attention-agent-stale';stale.textContent='Couldn’t refresh';row.querySelector('.attention-agent-copy').append(stale); }
    row.onclick=()=>{void attentionBoardSelectAgent(p,agent.agent_id);};
    host.append(row);
  }
  if (cache.error || failed.length || !visible.length) {
    const message=document.createElement('p');message.className='attention-empty';
    message.textContent=cache.error ? 'Could not refresh agents. ' + cache.error.message
      : failed.length ? 'Could not refresh Todo for ' + failed.join(', ') + '.'
      : cache.run || !cache.at ? 'Loading agents…' : ordubashyView ? 'No focused agents.' : 'No agents have todos.';
    if (cache.error || failed.length) message.setAttribute('role','status');
    host.append(message);
  }
  if (focused) [...host.querySelectorAll('[data-attention-agent]')].find(row=>row.dataset.attentionAgent === focused)?.focus({preventScroll:true});
}
