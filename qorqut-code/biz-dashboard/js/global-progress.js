// Period switches share one owner-scoped daily activity snapshot.
const globalProgressCache = new Map();
function globalProgressMount(host) {
  const metrics = [['tasks_done','Tasks done','gp-tasks'],['emails_sent','Emails sent','gp-emails'],['inbox_messages','Inbox messages','gp-inbox'],['learnings_accepted','Accepted learnings','gp-learnings']];
  let days = 30, rows = null;
  host.innerHTML = `<section class="global-progress" aria-label="Business activity"><header><div class="global-progress-periods" role="group" aria-label="Chart period">${[30,120,365].map(n=>`<button type="button" data-days="${n}" aria-pressed="${n===days}">${n} days</button>`).join('')}</div><div class="global-progress-legend">${metrics.map(([,label,cls])=>`<span class="${cls}">${label}</span>`).join('')}</div></header><div class="global-progress-plot" role="status">Loading activity…</div></section>`;
  const plot = host.querySelector('.global-progress-plot');
  const current = () => host.isConnected;
  const dateLabel = date => new Date(date+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'});
  function draw() {
    if (!current() || !rows) return;
    const values = rows.slice(-days), width=Math.max(280,plot.clientWidth), height=width<550?260:Math.min(420,Math.max(300,width*.3)), left=36, right=18, top=20, bottom=34;
    const max=Math.max(4,...values.flatMap(r=>metrics.map(([key])=>r[key])));
    const step=Math.max(1,Math.ceil(max/4)), ceiling=step*4;
    const x=i=>left+i*(width-left-right)/Math.max(1,values.length-1), y=v=>height-bottom-v*(height-top-bottom)/ceiling;
    const points=(key,list)=>list.map((r,i)=>`${x(i)},${y(r[key])}`).join(' ');
    plot.removeAttribute('role');
    plot.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily tasks, emails, inbox messages and accepted learnings across workspaces, last ${days} days, UTC. Today is incomplete." tabindex="0">${[0,1,2,3,4].map(i=>`<line class="gp-grid" x1="${left}" x2="${width-right}" y1="${y(i*step)}" y2="${y(i*step)}"/><text x="${left-12}" y="${y(i*step)+4}" text-anchor="end">${i*step}</text>`).join('')}${metrics.map(([key,,cls])=>`<g class="${cls}"><polyline class="gp-line" points="${points(key,values.slice(0,-1))}"/><line class="gp-line gp-today" x1="${x(values.length-2)}" y1="${y(values.at(-2)[key])}" x2="${x(values.length-1)}" y2="${y(values.at(-1)[key])}"/><circle cx="${x(values.length-1)}" cy="${y(values.at(-1)[key])}" r="3" fill="currentColor"/></g>`).join('')}${[0,Math.floor((values.length-1)/2),values.length-1].map((i,j)=>`<text x="${x(i)}" y="${height-8}" text-anchor="${['start','middle','end'][j]}">${esc(dateLabel(values[i].date))}${j===2?' · today':''}</text>`).join('')}<line class="gp-cursor" y1="${top}" y2="${height-bottom}" hidden/></svg><output class="gp-readout" hidden></output>`;
    const svg=plot.querySelector('svg'), cursor=plot.querySelector('.gp-cursor'), readout=plot.querySelector('output');
    let selected=values.length-1;
    function inspect(i) {
      selected=Math.max(0,Math.min(values.length-1,i));const r=values[selected];
      cursor.setAttribute('x1',x(selected));cursor.setAttribute('x2',x(selected));cursor.removeAttribute('hidden');
      readout.hidden=false;readout.textContent=`${dateLabel(r.date)}${selected===values.length-1?' · today':''} · ${metrics.map(([key,label])=>`${r[key]} ${label.toLowerCase()}`).join(' · ')}`;
    }
    svg.onpointermove=e=>{const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;inspect(Math.round((p.matrixTransform(svg.getScreenCTM().inverse()).x-left)/(width-left-right)*(values.length-1)));};
    svg.onpointerleave=()=>{cursor.setAttribute('hidden','');readout.hidden=true;};
    svg.onfocus=()=>inspect(selected);
    svg.onblur=svg.onpointerleave;
    svg.onkeydown=e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();inspect(selected+(e.key==='ArrowLeft'?-1:1));}};
  }
  host.querySelectorAll('[data-days]').forEach(button=>button.onclick=()=>{
    days=Number(button.dataset.days);host.querySelectorAll('[data-days]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));draw();
  });
  async function load() {
    plot.textContent='Loading activity…';
    const today=new Date().toISOString().slice(0,10);
    try {
      let entry=globalProgressCache.get('global');
      if (!entry || entry.signal?.aborted || entry.day!==today || Date.now()-entry.at>60000) {
        entry={day:today,at:Date.now(),signal:pageReads.signal,promise:pageReadPost('/root/operations/global_progress_get',{days:365}).then(coreData)};
        globalProgressCache.set('global',entry);
        entry.promise.then(()=>{entry.signal=null;},()=>{});
        entry.promise.catch(()=>{if(globalProgressCache.get('global')===entry)globalProgressCache.delete('global');});
      }
      const data=await entry.promise;
      if(!Array.isArray(data.series) || data.series.length!==365 || data.series.some(r=>metrics.some(([key])=>!Number.isInteger(r[key])))) throw new Error('Activity history unavailable');
      rows=data.series;
      draw();
    } catch(error) {
      if(!current())return;plot.textContent='Activity unavailable. ';
      const retry=document.createElement('button');retry.type='button';retry.textContent='Retry';retry.onclick=load;plot.appendChild(retry);
    }
  }
  let lastWidth = plot.clientWidth;
  const observer = new ResizeObserver(() => {
    if (!current()) { observer.disconnect(); return; }
    if (plot.clientWidth !== lastWidth) { lastWidth=plot.clientWidth; draw(); }
  });
  observer.observe(plot);
  load();
}
