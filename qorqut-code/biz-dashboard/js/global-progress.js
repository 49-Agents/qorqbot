// Period switches share one owner-scoped daily activity snapshot.
const globalProgressCache = new Map();
function globalProgressMount(host, {signal = pageReads.signal, active = () => true} = {}) {
  const metrics = [['tasks_done','Tasks done','gp-tasks'],['emails_sent','Emails sent','gp-emails'],['inbox_messages','Inbox messages','gp-inbox'],['learnings_accepted','Accepted learnings','gp-learnings'],['cold_outreach_emails_sent','Cold outreach sent','gp-cold-outreach']];
  let days = 30, rows = null;
  host.innerHTML = `<section class="global-progress" aria-label="Business activity"><header><div class="global-progress-periods" role="group" aria-label="Chart period">${[30,120,365].map(n=>`<button type="button" data-days="${n}" aria-pressed="${n===days}">${n} days</button>`).join('')}</div><div class="global-progress-legend">${metrics.map(([,label,cls])=>`<span class="${cls}">${label}</span>`).join('')}</div></header><div class="global-progress-plot" role="status">Loading activity…</div></section>`;
  const plot = host.querySelector('.global-progress-plot');
  const current = () => host.isConnected && !signal.aborted && active();
  const dateLabel = date => sjDayLabel(date);   // server dates are San Juan calendar days
  function draw() {
    if (!current() || !rows) return;
    const values = rows.slice(-days), width=Math.max(280,plot.clientWidth), height=width<550?260:Math.min(420,Math.max(300,width*.3)), left=36, right=18, top=20, bottom=34;
    // Each series gets its own scale so a low-volume metric remains legible
    // beside a high-volume one. Only the Emails sent scale is rendered as an
    // axis; the other scales are intentionally visual-only.
    const scales=Object.fromEntries(metrics.map(([key])=>{
      const max=Math.max(4,...values.map(r=>r[key]));
      const step=Math.max(1,Math.ceil(max/4));
      return [key,{step,ceiling:step*4}];
    }));
    const x=i=>left+i*(width-left-right)/Math.max(1,values.length-1);
    const y=(key,v)=>height-bottom-v*(height-top-bottom)/scales[key].ceiling;
    const points=(key,list)=>list.map((r,i)=>`${x(i)},${y(key,r[key])}`).join(' ');
    const emailScale=scales.emails_sent;
    plot.removeAttribute('role');
    plot.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily tasks, emails, inbox messages, accepted learnings and cold outreach sent across workspaces, last ${days} days, San Juan time. Each series uses its own scale; the visible axis is Emails sent. Today is incomplete." tabindex="0">${[0,1,2,3,4].map(i=>`<line class="gp-grid" x1="${left}" x2="${width-right}" y1="${y('emails_sent',i*emailScale.step)}" y2="${y('emails_sent',i*emailScale.step)}"/><text class="gp-axis-label" x="${left-12}" y="${y('emails_sent',i*emailScale.step)+4}" text-anchor="end">${i*emailScale.step}</text>`).join('')}<text class="gp-axis-title" x="${-(height-top-bottom)/2-top}" y="14" text-anchor="middle" transform="rotate(-90)">Emails sent</text>${metrics.map(([key,,cls])=>`<g class="${cls}" data-scale-ceiling="${scales[key].ceiling}"><polyline class="gp-line" points="${points(key,values.slice(0,-1))}"/><line class="gp-line gp-today" x1="${x(values.length-2)}" y1="${y(key,values.at(-2)[key])}" x2="${x(values.length-1)}" y2="${y(key,values.at(-1)[key])}"/><circle cx="${x(values.length-1)}" cy="${y(key,values.at(-1)[key])}" r="3" fill="currentColor"/></g>`).join('')}${[0,Math.floor((values.length-1)/2),values.length-1].map((i,j)=>`<text x="${x(i)}" y="${height-8}" text-anchor="${['start','middle','end'][j]}">${esc(dateLabel(values[i].date))}${j===2?' · today':''}</text>`).join('')}<line class="gp-cursor" y1="${top}" y2="${height-bottom}" hidden/></svg><output class="gp-readout" hidden></output>`;
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
    const today=sjToday();
    try {
      let entry=globalProgressCache.get('global');
      if (!entry || entry.signal?.aborted || entry.day!==today || Date.now()-entry.at>60000) {
        entry={day:today,at:Date.now(),signal,promise:pageReadPost('/root/operations/global_progress_get',{days:365},signal).then(coreData)};
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
