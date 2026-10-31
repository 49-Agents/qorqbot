// Routines use the journal/notes rail, learning scope icons, and shared dialogs/toasts.
let rtData = null, rtRuns = null, rtBiz = null, rtGeneration = 0;
let rtPage = 'active', rtShowPaused = false, rtDialog = null;
let rtPreviewTimer = null, rtPreviewGeneration = 0;
const RT_ICONS = {
  active: lnSvg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  logs: lnSvg('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>'),
  play: lnSvg('<path d="m9 5 11 7-11 7Z"/>'),
  more: lnSvg('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'),
};
const rtMoney = n => '$' + Number(n || 0).toFixed(4);
const rtCommand = argv => (argv || []).map(a => /^[a-zA-Z0-9_./=-]+$/.test(a) ? a : "'" + a.replace(/'/g, "'\\''") + "'").join(' ');
const rtEndpoint = (global, biz = state.biz) => global ? '/root/routines' : `/biz/${encodeURIComponent(biz)}/routines`;
const rtKey = row => row.key || `${row.scope}/${row.location || ''}/${row.id}`;
function rtScope(scope) {
  const info = LN_SCOPE_BY_KEY[scope] || LN_SCOPE_BY_KEY.biz;
  return `<span class="lnscopeico" title="${esc(info.label)}" aria-label="${esc(info.label)}">${info.ico}</span>`;
}
function rtTime(value, zone) {
  if (!value || !Number.isFinite(Date.parse(value))) return '—';
  const options = {weekday:'short', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hour12:false, timeZoneName:'short'};
  try { return new Intl.DateTimeFormat('en-US', {...options, ...(zone ? {timeZone:zone} : {})}).format(new Date(value)); }
  catch (_) { return new Intl.DateTimeFormat('en-US', options).format(new Date(value)); }
}
function tPaintRoutines() {
  if (rtBiz !== state.biz) {
    if (rtDialog) rtDialog.close();
    rtDialog = null; rtBiz = state.biz; rtData = null; rtRuns = null; rtGeneration++;
  }
  rtRender(); rtRefresh();
}
async function rtRefresh() {
  if (tView !== 'routines' || !state.biz) return;
  const biz = state.biz, generation = ++rtGeneration;
  try {
    const [data, logs] = await Promise.all([api(rtEndpoint(false, biz)), api(rtEndpoint(false, biz) + '/runs?n=50')]);
    if (biz !== state.biz || generation !== rtGeneration || tView !== 'routines') return;
    rtData = data; rtRuns = logs;
    rtRender();
  } catch (_) { if (biz === state.biz) notify({text:'Could not load routines', color:'var(--danger)'}); }
}
function rtRows() {
  const seen = new Set();
  return (rtData && rtData.routines || []).filter(row => {
    const key = rtKey(row); if (seen.has(key)) return false; seen.add(key); return true;
  }).sort((a,b) => (a.status === 'paused') - (b.status === 'paused') || String(a.next || '').localeCompare(String(b.next || '')) || a.name.localeCompare(b.name));
}
function rtRoutineRow(row) {
  const next = row.status === 'paused' ? 'Paused' : 'Next ' + rtTime(row.next, row.zone);
  const current = rtData.current && rtData.current.key === row.key;
  return `<article class="rtrow" data-key="${esc(rtKey(row))}">
    ${rtScope(row.scope)}<button class="rttitle" data-action="detail" title="${esc(row.name)}">${esc(row.name)}</button>
    <div class="rtmeta" title="${esc(next)}">${current ? '<span class="rtok">Running</span><span class="sep">·</span>' : ''}${esc(next)}</div>
    <div class="rtrowactions"><button class="jricon" data-action="run" aria-label="Run ${esc(row.name)} now" title="Run now" ${!rtData.enabled ? 'disabled' : ''}>${RT_ICONS.play}</button>
    <button class="jricon" data-action="more" aria-label="Actions for ${esc(row.name)}" title="Routine actions">${RT_ICONS.more}</button></div></article>`;
}
function rtRunRow(run, index) {
  const costTitle = (run.scope === 'global' ? 'Total for this global run' : 'Cost of this business run') + (rtCostNote(run) ? ' · ' + rtCostNote(run) : '');
  return `<article class="rtrow rtrunrow" data-run="${index}">
    ${rtScope(run.scope)}<button class="rttitle" data-action="run-detail" title="${esc(run.name || run.routine)}">${esc(run.name || run.routine)}</button>
    <div class="rtmeta" title="${esc(rtTime(run.start, run.zone))}"><span class="${run.ok ? 'rtok' : 'rtfail'}">${run.running ? 'Running' : run.ok ? 'Succeeded' : 'Failed'}</span><span class="sep">·</span><time>${esc(rtTime(run.start, run.zone))}</time><span class="sep">·</span>${esc(run.seconds)}s</div>
    <span class="rtprice" title="${esc(costTitle)}">${rtMoney(run.usd)}</span></article>`;
}
function rtCostNote(run) {
  return (run.runs || []).some(x => !x.model) ? 'Some usage was not reported; the recorded cost may be incomplete.' : '';
}
function rtRender() {
  if (tView !== 'routines') return;
  const body = $('#tbody'); if (!body) return;
  body.classList.add('rt');
  if (!state.biz) { body.innerHTML = '<div class="jrfaint jrfaint-pad">Pick a business first.</div>'; return; }
  const rows = rtRows(), active = rows.filter(r => r.status === 'active'), paused = rows.length - active.length;
  const logs = Array.isArray(rtRuns) ? rtRuns : [];
  const visible = rtShowPaused ? rows : active;
  const isLog = rtPage === 'logs';
  const current = rtData && rtData.current, queued = rtData && rtData.queued || [];
  const status = !rtData ? 'Loading…' : !rtData.enabled ? 'Runner disabled' : current ? `${current.routine} running · ${current.seconds}s` : queued.length ? `${queued.length} queued` : 'Ready';
  const problem = rtData && rtData.error || isLog && rtRuns && rtRuns.error;
  body.innerHTML = `<div class="jred rtworkspace"><nav class="jrrail rtnav" aria-label="Routines navigation">
    <button class="jricon ${!isLog ? 'on' : ''}" data-page="active" aria-current="${!isLog ? 'page' : 'false'}" title="Active routines">${RT_ICONS.active}<span>Active</span></button>
    <button class="jricon ${isLog ? 'on' : ''}" data-page="logs" aria-current="${isLog ? 'page' : 'false'}" title="Recent runs">${RT_ICONS.logs}<span>Recent runs</span></button>
    </nav><section class="rtcontent"><header class="jrhhead"><div><h1>${isLog ? 'Recent runs' : 'Routines'}</h1><div class="jrsub">${isLog ? `${logs.length} recent runs` : `${active.length} active${paused ? ' · ' + paused + ' paused' : ''}`}</div></div>
    ${!isLog ? `<button class="btn" id="rtnew">${ICO_PLUS}New routine</button>` : ''}</header>
    ${!isLog ? `<div class="rttoolbar"><span class="rtmuted" title="${esc(rtData && rtData.last_sweep || '')}">${esc(status)}</span>${current ? '<button class="btn quiet" id="rtkill">Stop run</button>' : ''}${paused ? `<label class="rtpaused"><input id="rtpaused" type="checkbox" ${rtShowPaused ? 'checked' : ''}>Show paused</label>` : ''}</div>` : ''}
    ${problem ? `<p class="rtfail">${esc(problem)}</p>` : ''}
    <div class="rtlist">${!rtData ? '<p class="jrfaint jrfaint-pad">Loading routines…</p>' : isLog ? (logs.length ? logs.map(rtRunRow).join('') : '<p class="jrfaint jrfaint-pad">No runs yet.</p>') : (visible.length ? visible.map(rtRoutineRow).join('') : '<p class="jrfaint jrfaint-pad">No active routines.</p>')}</div>
    ${!isLog && rtData && (rtData.invalid || []).length ? `<details class="mkchain rtissues"><summary>${rtData.invalid.length} invalid definitions</summary>${rtData.invalid.map(x => `<p class="rtfail">${esc(x.file)}: ${esc(x.error)}</p>`).join('')}</details>` : ''}
    </section></div>`;
  body.querySelectorAll('[data-page]').forEach(el => el.onclick = () => { rtPage = el.dataset.page; rtRender(); });
  body.querySelectorAll('.rtrow[data-key]').forEach(el => el.onclick = e => {
    const button = e.target.closest('[data-action]'); if (!button) return;
    const row = rows.find(r => rtKey(r) === el.dataset.key); if (!row) return;
    if (button.dataset.action === 'more') return rtMenu(button, row);
    if (button.dataset.action === 'detail') return rtDetail(row);
    rtAction(button.dataset.action, row);
  });
  body.querySelectorAll('.rtrunrow').forEach(el => el.onclick = () => rtRunDetail(logs[Number(el.dataset.run)]));
  const add = $('#rtnew'), show = $('#rtpaused'), kill = $('#rtkill');
  if (add) add.onclick = () => rtOpenForm();
  if (show) show.onchange = () => { rtShowPaused = show.checked; rtRender(); };
  if (kill) kill.onclick = async () => {
    const result = await post('/routines/kill', {});
    notify({text:result.error || 'Stopping routine', ...(result.error ? {color:'var(--danger)'} : {})});
    rtRefresh();
  };
}
function rtMenu(button, row) {
  dropdown(button, [{value:'edit',label:'Edit routine'}, {value:row.status === 'paused' ? 'resume' : 'pause',label:row.status === 'paused' ? 'Resume' : 'Pause'}, {value:'rm',label:'Delete routine'}], action => rtAction(action, row));
}
function rtNotify(text, row, error = false, biz = state.biz) {
  notify({text, sub:row.scope === 'global' ? 'All businesses' : biz, ...(error ? {color:'var(--danger)'} : {})});
}
async function rtAction(action, row) {
  const biz = state.biz;
  if (action === 'edit') return rtOpenForm(row);
  if (action === 'rm') {
    rtDialog = modal({title:'Delete routine?', body:`<p>${esc(row.name)}</p><p class="rtmuted">Past runs remain in the log.</p>`, actions:[
      {label:'Cancel',onClick:close=>close()},
      {label:'Delete',primary:true,onClick:async close=>{ close(); await rtWrite('rm',row,biz); }},
    ]});
    return;
  }
  if (action !== 'run') return rtWrite(action, row, biz);
  const result = await post('/routines/run', {location:row.location,id:row.id});
  rtNotify(result.error || result.warn || `Queued ${row.name}`, row, !!result.error, biz);
  if (biz === state.biz) rtRefresh();
}
async function rtWrite(op, row, biz) {
  const result = await post(rtEndpoint(row.scope === 'global', biz), {op,id:row.id});
  rtNotify(result.error || result.warn || (op === 'rm' ? 'Routine deleted' : op === 'pause' ? 'Routine paused' : 'Routine resumed'), row, !!result.error, biz);
  if (biz === state.biz) rtRefresh();
}
function rtDetail(row) {
  const fields = [
    ['Next scheduled',row.status === 'paused' ? 'Paused' : rtTime(row.next,row.zone)],
    ['Command',rtCommand(row.run)], ['Departments',row.departments.join(', ')],
    ['Timeout',row.timeout + ' seconds'], ['Output paths',(row.commit || []).join(', ') || 'None'],
  ];
  rtDialog = modal({title:esc(row.name),width:620,body:`<div class="tdetail rtdetail"><div class="rtscope">${rtScope(row.scope)}</div><div class="flds">${fields.map(([label,value])=>`<span class="k">${label}</span><span class="v">${esc(value)}</span>`).join('')}</div><p>${esc(row.why)}</p></div>`,actions:[
    {label:'Close',onClick:close=>close()},
    {label:'Edit',onClick:close=>{close();rtOpenForm(row);}},
    ...(rtData.enabled ? [{label:'Run now',primary:true,onClick:close=>{close();rtAction('run',row);}}] : []),
  ]});
}
function rtRunDetail(run) {
  if (!run) return;
  const result = run.running ? 'Running' : run.ok ? 'Succeeded' : 'Failed';
  const models = [...new Set((run.runs || []).map(x => x.model).filter(Boolean))].join(', ');
  rtDialog = modal({title:esc(run.name || run.routine),width:680,body:`<div class="tdetail rtdetail"><div class="rtscope">${rtScope(run.scope)}<span class="${run.ok ? 'rtok' : 'rtfail'}">${result}</span><span class="rtprice">${rtMoney(run.usd)}</span></div>
    <div class="flds"><span class="k">Started</span><span class="v">${esc(rtTime(run.start,run.zone))}</span><span class="k">Duration</span><span class="v">${esc(run.seconds)} seconds</span>${models ? `<span class="k">Model</span><span class="v">${esc(models)}</span>` : ''}</div>
    ${rtCostNote(run) ? `<p class="rtmuted">${rtCostNote(run)}</p>` : ''}
    ${(run.runs || []).map(x=>`<details class="mkchain rtoutput" ${(run.runs || []).length === 1 ? 'open' : ''}><summary>${esc(x.slug === 'root' ? 'Businesses root' : x.slug)} · ${x.ok ? 'succeeded' : 'failed'} · rc ${esc(x.rc)}</summary><pre>${esc(x.stdout_tail || '(no stdout)')}\n--- stderr ---\n${esc(x.stderr_tail || '(none)')}${x.git_error ? '\nCommit error: ' + esc(x.git_error) : ''}</pre></details>`).join('')}</div>`,actions:[{label:'Close',onClick:close=>close()}]});
}
function rtFormHTML(row = {}) {
  const input = (key,label,value,extra='') => `<label>${label}<input class="inp" name="${key}" value="${esc(String(value == null ? '' : value))}" ${extra}></label>`;
  return `<form id="rtform" class="rtform"><div class="rtscopepick"><span class="rtmuted">Scope</span>${['biz','global'].map(scope=>`<button type="button" class="lnscope ${scope === (row.scope || 'biz') ? 'on' : ''}" data-scope="${scope}" title="${esc(LN_SCOPE_BY_KEY[scope].label)}" aria-label="${esc(LN_SCOPE_BY_KEY[scope].label)}" ${(row.id && scope !== row.scope) || (scope === 'global' && !rtData.root) ? 'disabled' : ''}>${LN_SCOPE_BY_KEY[scope].ico}</button>`).join('')}</div><div class="rtfields">
    ${input('id','ID',row.id,row.id ? 'readonly required' : 'required pattern="[a-z0-9][a-z0-9-]{1,39}"')}${input('name','Name',row.name,'required maxlength="80"')}
    <label class="rtwide">Command<input class="inp" name="run" value="${esc(rtCommand(row.run))}" required placeholder="git status --short"></label>
    ${input('when','Schedule',row.when || '17 4 * * *','required')}${input('tz','Timezone (blank uses default)',row.tz || '')}
    <div id="rtpreview" class="rtwide rtmuted" aria-live="polite">Checking next scheduled times…</div>
    <label id="rttarget" ${row.scope !== 'global' ? 'hidden' : ''}>Run in<select class="inp" name="target"><option value="each-business" ${row.target !== 'root' ? 'selected' : ''}>Each business</option><option value="root" ${row.target === 'root' ? 'selected' : ''}>Businesses root</option></select></label>
    ${input('timeout','Timeout (seconds)',row.timeout || 900,'type="number" min="60" max="7200" required')}
    <fieldset class="rtwide"><legend>Departments</legend>${['marketing','accounting','data-science','code','general','onbashlyk'].map(d=>`<label class="rtcheck"><input type="checkbox" name="departments" value="${d}" ${(row.departments || ['onbashlyk']).includes(d) ? 'checked' : ''}>${d}</label>`).join('')}</fieldset>
    <label class="rtcheck rtwide" title="Delays scheduled runs while an agent was active in the last 10 minutes. Retries for up to 6 hours."><input type="checkbox" name="skip_if_live" ${row.skip_if_live ? 'checked' : ''}>Wait until agents are idle</label>
    ${input('commit','Output paths (comma-separated)',(row.commit || []).join(', '))}
    <label class="rtwide">Why<textarea class="inp" name="why" required>${esc(row.why || '')}</textarea></label></div><p id="rterror" class="rtfail" role="alert"></p></form>`;
}
function rtOpenForm(row = null) {
  const draft = {scope:row && row.scope || 'biz',biz:state.biz};
  const save = async (close, el) => {
    const form = el.querySelector('#rtform'); if (!form.reportValidity()) return;
    const data = new FormData(form), departments = data.getAll('departments');
    if (!departments.length) { form.querySelector('#rterror').textContent = 'Choose at least one department.'; return; }
    const payload = {op:row ? 'set' : 'add',id:data.get('id'),name:data.get('name'),run:data.get('run'),when:data.get('when'),tz:data.get('tz'),
      timeout:Number(data.get('timeout')),departments,skip_if_live:data.has('skip_if_live'),commit:String(data.get('commit') || '').split(',').map(x=>x.trim()).filter(Boolean),why:data.get('why')};
    if (draft.scope === 'global') payload.target = data.get('target');
    const button = el.querySelector('.mfoot .primary'); button.disabled = true;
    try {
      const result = await post(rtEndpoint(draft.scope === 'global',draft.biz),payload);
      if (result.error) { form.querySelector('#rterror').textContent = result.error; return; }
      close(); rtNotify(result.warn || 'Routine saved',{scope:draft.scope},false,draft.biz);
      if (draft.biz === state.biz) rtRefresh();
    } catch (_) {
      form.querySelector('#rterror').textContent = 'Could not save routine. Please try again.';
    } finally { button.disabled = false; }
  };
  rtDialog = modal({title:row ? 'Edit routine' : 'New routine',width:640,body:rtFormHTML(row || {}),actions:[{label:'Cancel',onClick:close=>close()},{label:'Save routine',primary:true,onClick:save}],onOpen:el=>{
    const form = el.querySelector('#rtform');
    const preview = () => rtPreview(form,draft.scope === 'global',draft.biz);
    for (const name of ['when','tz']) form.elements[name].oninput = () => { clearTimeout(rtPreviewTimer); rtPreviewTimer = setTimeout(preview,300); };
    form.querySelectorAll('[data-scope]').forEach(button=>button.onclick=()=>{
      draft.scope=button.dataset.scope;form.querySelectorAll('[data-scope]').forEach(b=>b.classList.toggle('on',b===button));form.querySelector('#rttarget').hidden=draft.scope!=='global';preview();
    });
    form.onsubmit = e => { e.preventDefault(); el.querySelector('.mfoot .primary').click(); };
    preview();
  }});
}
async function rtPreview(form, global, biz) {
  const generation = ++rtPreviewGeneration;
  const q = new URLSearchParams({when:form.elements.when.value,tz:form.elements.tz.value});
  const result = await api(rtEndpoint(global,biz) + '/next?' + q);
  if (generation !== rtPreviewGeneration || !form.isConnected || biz !== state.biz) return;
  form.querySelector('#rtpreview').textContent = result.error || (result.next || []).map(x=>x.local).join(' · ');
}
function rtFailureToasts(rows) {
  let prior;
  try { prior=JSON.parse(localStorage.getItem('routine-failures-seen') || '[]'); } catch (_) { prior=[]; }
  const seen=new Set(Array.isArray(prior) ? prior : []);
  (rows || []).slice().reverse().forEach(row=>{
    if (seen.has(row.run)) return;
    seen.add(row.run);notify({text:`Routine failed: ${row.routine}`,sub:`rc ${row.rc} · ${row.end}`,color:'var(--danger)'});
  });
  localStorage.setItem('routine-failures-seen',JSON.stringify([...seen].slice(-500)));
}
