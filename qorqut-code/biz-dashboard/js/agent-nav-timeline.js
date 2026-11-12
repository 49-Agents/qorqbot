// A read-only snapshot of the selected agent's canonical Actions, loaded only while navigation is open.
function agentNavTimelineMount(nav) {
  const panel=document.createElement('aside');
  panel.className='agent-nav-timeline';panel.setAttribute('aria-label','Weekly Report');
  panel.innerHTML='<header><h2>Weekly Report</h2></header><p class="agent-nav-timeline-status" data-status role="status"></p><div class="agent-nav-charts"><section><h3>Actions per day</h3><div class="agent-nav-chart" role="group" aria-label="Recorded actions per day in the last 7 days"></div></section><section><h3>Tasks done per day</h3><p class="agent-nav-tasks-status" data-tasks-status role="status">Loading completed tasks…</p><div class="agent-nav-chart agent-nav-tasks-chart" role="group" aria-label="Completed tasks per day across workspaces" hidden></div></section></div><button type="button" class="quiet" data-reset hidden>Show all dates</button><ol class="agent-nav-events" aria-label="Recent recorded actions"></ol>';
  nav.append(panel);
  const status=panel.querySelector('[data-status]'),chart=panel.querySelector('.agent-nav-chart'),list=panel.querySelector('ol'),reset=panel.querySelector('[data-reset]');
  const taskStatus=panel.querySelector('[data-tasks-status]'),taskChart=panel.querySelector('.agent-nav-tasks-chart');
  let taskRequest=null,taskVersion=0;
  let agent=null,rows=[],days=[],filter=null,request=null,version=0;
  const active=()=>nav.isConnected && nav.matches(':popover-open');
  function paint() {
    const counts=days.map(day=>rows.filter(row=>row.occurred?.date===day).length),max=Math.max(1,...counts);
    chart.replaceChildren(...days.map((day,i)=>{
      const button=document.createElement('button');button.type='button';button.className='agent-nav-chart-day';
      button.setAttribute('aria-label',`${day}: ${counts[i]} recorded actions`);button.setAttribute('aria-pressed',String(filter===day));
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
    reset.textContent=filter+' · Show all dates';
  }
  async function refreshTasks() {
    if(!active() || !agent || taskRequest || matchMedia('(max-width:700px)').matches)return;
    const mine=++taskVersion,id=agent.agent_id,controller=taskRequest=new AbortController();
    const current=()=>active() && taskVersion===mine && agent?.agent_id===id;
    taskStatus.textContent='Loading completed tasks…';taskChart.hidden=true;
    try {
      const data=coreData(await post('/root/operations/global_progress_get',{days:30},{signal:controller.signal}));
      if(!current())return;
      if(!Array.isArray(data.series) || !data.series.length)throw Error('Invalid progress response');
      const recent=[...data.series].sort((a,b)=>a.date.localeCompare(b.date)).slice(-7);
      if(recent.some(day=>!/^\d{4}-\d{2}-\d{2}$/.test(day.date) || !Number.isInteger(day.tasks_done) || day.tasks_done<0))throw Error('Invalid task counts');
      const days=recent.map(day=>day.date),counts=recent.map(day=>day.tasks_done);
      const max=Math.max(1,...counts);
      taskChart.replaceChildren(...days.map((day,i)=>{
        const column=document.createElement('div');column.className='agent-nav-chart-day';column.setAttribute('aria-label',`${day}: ${counts[i]} completed tasks`);
        const count=document.createElement('span');count.className='agent-nav-chart-count';count.textContent=counts[i];
        const track=document.createElement('span');track.className='agent-nav-chart-track';
        const bar=document.createElement('i');bar.style.height=(counts[i]/max*100)+'%';track.append(bar);
        const label=document.createElement('span');label.textContent=day.slice(5);column.append(count,track,label);return column;
      }));
      taskChart.hidden=false;
      taskStatus.textContent=`Last 7 days${data.timezone?' · '+data.timezone:''}`;
    } catch(error) {
      if(!current() || controller.signal.aborted)return;
      taskChart.replaceChildren();taskChart.hidden=true;taskStatus.textContent='Completed tasks unavailable. Reopen navigation to retry.';
    } finally {if(taskVersion===mine)taskRequest=null;}
  }
  async function refresh() {
    if(!active() || matchMedia('(max-width:700px)').matches || request || !agent)return;
    void refreshTasks();
    const mine=++version,id=agent.agent_id,controller=request=new AbortController();
    const current=()=>active() && version===mine && agent?.agent_id===id;
    const until=sjToday(),since=sjAddDays(until,-6),loaded=new Map(),cursors=new Set();
    rows=[];days=[];filter=null;paint();
    status.hidden=false;status.textContent='Loading actions…';
    try {
      // Publish totals only after every page in this week has loaded.
      let cursor;
      do {
        const data=coreData(await post('/root/operations/actions_list',{
          performer:id,since,until,limit:200,...(cursor?{cursor}:{}),
        },{signal:controller.signal}));
        if(!current())return;
        if(!Array.isArray(data.actions))throw Error('Invalid actions response');
        for(const row of data.actions)loaded.set(row.action_id,row);
        cursor=data.next_cursor;
        if(cursor!=null && (typeof cursor!=='string' || !cursor || cursors.has(cursor)))throw Error('Invalid actions cursor');
        if(cursor)cursors.add(cursor);
      }while(cursor);
      rows=[...loaded.values()];
      days=Array.from({length:7},(_,i)=>sjAddDays(since,i));
      status.textContent=rows.length ? '' : 'No actions recorded in the last 7 days.';status.hidden=!!rows.length;
      paint();
    } catch(error) {
      if(!current() || controller.signal.aborted)return;
      rows=[];days=[];paint();status.hidden=false;status.textContent='Actions unavailable. Reopen navigation to retry.';
    } finally {
      if(version===mine)request=null;
    }
  }
  function stop(){version++;request?.abort();request=null;taskVersion++;taskRequest?.abort();taskRequest=null;}
  reset.onclick=()=>{filter=null;paint();};
  return {
    panel,
    setAgent(next){
      if(agent?.agent_id===next?.agent_id)return;
      stop();agent=next;rows=[];days=[];filter=null;paint();taskChart.replaceChildren();taskChart.hidden=true;taskStatus.textContent='Select an agent to view completed tasks.';
      status.hidden=false;status.textContent=agent?'Open navigation to view actions.':'Select an agent to view its actions.';
      void refresh();
    },
    open(){void refresh();},close:stop,
  };
}
