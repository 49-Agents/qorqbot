// Reuse the business Progress series; period switches share the same snapshot.
const globalProgressCache = new Map();
function globalProgressMount(host, businesses) {
  let days = 30, rows = null;
  host.innerHTML = `<section class="global-progress" aria-label="Business activity"><header><div class="global-progress-periods" role="group" aria-label="Chart period">${[30,120,365].map(n=>`<button type="button" data-days="${n}" aria-pressed="${n===days}">${n} days</button>`).join('')}</div><div class="global-progress-legend"><span class="gp-tasks">Tasks done</span><span class="gp-emails">Emails sent</span></div></header><div class="global-progress-plot" role="status">Loading activity…</div></section>`;
  const plot = host.querySelector('.global-progress-plot');
  const current = () => host.isConnected;
  const dateLabel = date => new Date(date+'T00:00:00Z').toLocaleDateString(undefined,{month:'short',day:'numeric',timeZone:'UTC'});
  function draw() {
    if (!current() || !rows) return;
    const values = rows.slice(-days), width=Math.max(280,plot.clientWidth), height=width<550?260:Math.min(420,Math.max(300,width*.3)), left=36, right=18, top=20, bottom=34;
    const max=Math.max(4,...values.flatMap(r=>[r.tasks_done,r.emails_sent]));
    const step=Math.max(1,Math.ceil(max/4)), ceiling=step*4;
    const x=i=>left+i*(width-left-right)/Math.max(1,values.length-1), y=v=>height-bottom-v*(height-top-bottom)/ceiling;
    const points=(key,list)=>list.map((r,i)=>`${x(i)},${y(r[key])}`).join(' ');
    plot.removeAttribute('role');
    plot.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily tasks completed and emails sent across businesses, last ${days} days, UTC. Today is incomplete." tabindex="0">${[0,1,2,3,4].map(i=>`<line class="gp-grid" x1="${left}" x2="${width-right}" y1="${y(i*step)}" y2="${y(i*step)}"/><text x="${left-12}" y="${y(i*step)+4}" text-anchor="end">${i*step}</text>`).join('')}${['tasks_done','emails_sent'].map((key,k)=>`<g class="${k?'gp-emails':'gp-tasks'}"><polyline class="gp-line" points="${points(key,values.slice(0,-1))}"/><line class="gp-line gp-today" x1="${x(values.length-2)}" y1="${y(values.at(-2)[key])}" x2="${x(values.length-1)}" y2="${y(values.at(-1)[key])}"/><circle cx="${x(values.length-1)}" cy="${y(values.at(-1)[key])}" r="3" fill="currentColor"/></g>`).join('')}${[0,Math.floor((values.length-1)/2),values.length-1].map((i,j)=>`<text x="${x(i)}" y="${height-8}" text-anchor="${['start','middle','end'][j]}">${esc(dateLabel(values[i].date))}${j===2?' · today':''}</text>`).join('')}<line class="gp-cursor" y1="${top}" y2="${height-bottom}" hidden/></svg><output class="gp-readout" hidden></output>`;
    const svg=plot.querySelector('svg'), cursor=plot.querySelector('.gp-cursor'), readout=plot.querySelector('output');
    let selected=values.length-1;
    function inspect(i) {
      selected=Math.max(0,Math.min(values.length-1,i));const r=values[selected];
      cursor.setAttribute('x1',x(selected));cursor.setAttribute('x2',x(selected));cursor.removeAttribute('hidden');
      readout.hidden=false;readout.textContent=`${dateLabel(r.date)}${selected===values.length-1?' · today':''} · ${r.tasks_done} tasks · ${r.emails_sent} emails`;
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
      const data=await Promise.all(businesses.map(async b=>{
        const key=b.id || b.slug;let entry=globalProgressCache.get(key);
        if(!entry || entry.day!==today || Date.now()-entry.at>60000){
          entry={day:today,at:Date.now(),promise:post(`/biz/${encodeURIComponent(b.slug)}/operations/business_progress_get`,{days:365}).then(coreData)};
          globalProgressCache.set(key,entry);
          entry.promise.catch(()=>{if(globalProgressCache.get(key)===entry)globalProgressCache.delete(key);});
        }
        const result=await entry.promise;
        if(!Array.isArray(result.series) || result.series.length!==365)throw new Error('Activity history is unavailable.');
        return result.series;
      }));
      rows=Array.from({length:365},(_,i)=>{const d=new Date(today+'T00:00:00Z');d.setUTCDate(d.getUTCDate()-364+i);return {date:d.toISOString().slice(0,10),tasks_done:0,emails_sent:0};});
      const dates=new Map(rows.map(r=>[r.date,r]));
      data.forEach(series=>series.forEach(r=>{const row=dates.get(r.date);if(row){row.tasks_done+=r.tasks_done;row.emails_sent+=r.emails_sent;}}));
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
