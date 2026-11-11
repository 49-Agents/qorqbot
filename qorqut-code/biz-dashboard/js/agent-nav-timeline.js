// A read-only snapshot of the selected agent's canonical Actions, loaded only while navigation is open.
function agentNavTimelineMount(nav) {
  const panel=document.createElement('aside');
  panel.className='agent-nav-timeline';panel.setAttribute('aria-label','Agent actions timeline');
  panel.innerHTML='<header><div><h2>Actions</h2><p data-agent></p></div><button type="button" class="quiet" data-refresh aria-label="Refresh actions">↻</button></header><p class="agent-nav-timeline-status" data-status role="status"></p><div class="agent-nav-chart" role="group" aria-label="Daily activity in the loaded actions"></div><button type="button" class="quiet" data-reset hidden>Show all dates</button><ol class="agent-nav-events" aria-label="Recent recorded actions"></ol><button type="button" class="quiet" data-more hidden>Load older actions</button>';
  nav.append(panel);
  const status=panel.querySelector('[data-status]'),chart=panel.querySelector('.agent-nav-chart'),list=panel.querySelector('ol'),more=panel.querySelector('[data-more]'),reset=panel.querySelector('[data-reset]');
  let agent=null,rows=[],cursor=null,filter=null,request=null,version=0;
  const active=()=>nav.isConnected && nav.matches(':popover-open');
  function paint() {
    const dates=rows.map(row=>row.occurred?.date).filter(date=>/^\d{4}-\d{2}-\d{2}$/.test(date || '')).sort();
    const days=[];
    if(dates.length){
      const last=new Date(dates.at(-1)+'T12:00:00Z');
      for(let i=6;i>=0;i--){const day=new Date(last);day.setUTCDate(day.getUTCDate()-i);days.push(day.toISOString().slice(0,10));}
    }
    const counts=days.map(day=>rows.filter(row=>row.occurred?.date===day).length),max=Math.max(1,...counts);
    chart.replaceChildren(...days.map((day,i)=>{
      const button=document.createElement('button');button.type='button';button.className='agent-nav-chart-day';
      button.setAttribute('aria-label',`${day}: ${counts[i]} loaded actions`);button.setAttribute('aria-pressed',String(filter===day));
      const count=document.createElement('span');count.className='agent-nav-chart-count';count.textContent=counts[i];
      const track=document.createElement('span');track.className='agent-nav-chart-track';
      const bar=document.createElement('i');bar.style.height=(counts[i]/max*100)+'%';track.append(bar);
      const label=document.createElement('span');label.textContent=day.slice(5);button.append(count,track,label);
      button.onclick=()=>{filter=filter===day?null:day;paint();};return button;
    }));
    chart.hidden=!days.length;reset.hidden=!filter;
    list.replaceChildren(...rows.filter(row=>!filter || row.occurred?.date===filter).map(action=>{
      const item=document.createElement('li'),time=document.createElement('time'),text=document.createElement('p');
      time.textContent=acOccurred(action);text.textContent=action.text || action.summary || 'Recorded action';
      item.append(time,text);
      if(action.attribution==='recorded'){
        const attribution=document.createElement('small');attribution.textContent='Recorded by '+(action.recorded_by?.name || 'unknown');item.append(attribution);
      }
      return item;
    }));
    more.hidden=!cursor;more.disabled=!!request;
    reset.textContent=filter+' · Show all dates';
  }
  async function refresh(older=false) {
    if(!active() || matchMedia('(max-width:700px)').matches || request || !agent || older && !cursor)return;
    const mine=++version,id=agent.agent_id,controller=request=new AbortController();
    const next=older?cursor:null;
    status.textContent=older?'Loading older actions…':'Loading actions…';
    panel.querySelector('[data-refresh]').disabled=true;more.disabled=true;
    try {
      const data=coreData(await post('/root/operations/actions_list',{performer:id,limit:50,...(next?{cursor:next}:{})},{signal:controller.signal}));
      if(!active() || version!==mine || agent?.agent_id!==id)return;
      if(!Array.isArray(data.actions) || data.next_cursor && data.next_cursor===next)throw Error('Invalid actions response');
      rows=[...new Map([...(older?rows:[]),...data.actions].map(row=>[row.action_id,row])).values()];cursor=data.next_cursor || null;
      status.textContent=rows.length ? `${rows.length} recent actions${cursor?' · more available':''} · daily chart shows loaded records` : 'No actions recorded yet.';
      if(!older)filter=null;
      paint();
    } catch(error) {
      if(version!==mine || !active() || controller.signal.aborted)return;
      rows=[];cursor=null;paint();status.textContent='Actions unavailable. Try refreshing.';
    } finally {
      if(version===mine){request=null;panel.querySelector('[data-refresh]').disabled=false;more.disabled=false;}
    }
  }
  function stop(){version++;request?.abort();request=null;}
  panel.querySelector('[data-refresh]').onclick=()=>refresh();more.onclick=()=>refresh(true);reset.onclick=()=>{filter=null;paint();};
  return {
    panel,
    setAgent(next){
      if(agent?.agent_id===next?.agent_id)return;
      stop();agent=next;rows=[];cursor=null;filter=null;paint();
      panel.querySelector('[data-agent]').textContent=agent ? agent.display_name || agent.name : 'Select an agent';
      status.textContent=agent?'Open navigation to view actions.':'Select an agent to view its actions.';
      void refresh();
    },
    open(){void refresh();},close:stop,
  };
}
