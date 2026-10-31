// Routines use the journal/notes rail, learning scope icons, and shared dialogs/toasts.
let rtData = null, rtRuns = null, rtBiz = null, rtGeneration = 0;
let rtPage = 'active', rtShowPaused = false, rtDialog = null;
let rtPreviewTimer = null, rtPreviewGeneration = 0, rtError = null;
const rtGlobal = () => state.scope === 'global';
const rtContextBiz = () => state.biz;
const rtContextKey = () => rtGlobal() ? 'global' : state.biz;
const rtVisible = () => tView === 'routines';
function rtSuspend() {
  rtGeneration++; rtPreviewGeneration++; clearTimeout(rtPreviewTimer);
  if (rtDialog) { rtDialog.close(); rtDialog = null; }
}

const RT_ICONS = {
  active: lnSvg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  logs: lnSvg('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>'),
  play: lnSvg('<path d="m9 5 11 7-11 7Z"/>'),
  more: lnSvg('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'),
};
const rtMoney = n => '$' + Number(n || 0).toFixed(4);
const rtEndpoint = (global, biz = state.biz) => global ? '/root/routines' : `/biz/${encodeURIComponent(biz)}/routines`;
const rtResult = result => { const data = coreData(result); return data; };
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
  if (rtBiz !== rtContextKey()) {
    if (rtDialog) rtDialog.close();
    rtDialog = null; rtBiz = rtContextKey(); rtData = null; rtRuns = null; rtError = null; rtGeneration++;
  }
  rtRender(); rtRefresh();
}
async function rtRefresh() {
  if (!rtVisible() || !rtGlobal() && !rtContextBiz()) return;
  const biz = rtContextBiz(), key = rtContextKey(), global = rtGlobal(), generation = ++rtGeneration;
  const current = () => key === rtContextKey() && generation === rtGeneration && rtVisible();
  const base = rtEndpoint(global, biz);
  try {
    const [data, logs] = await Promise.all([api(base), api(base + '/runs?n=50')]);
    if (!current()) return;
    if (!data || !Array.isArray(data.routines)) throw new Error(data && data.error || 'Could not load routines');
    if (!Array.isArray(logs)) throw new Error(logs && logs.error || 'Could not load run history');
    rtData = global ? {...data, routines:data.routines.filter(row => row.scope === 'global'),
      current:data.current?.definition_ref_json?.scope === 'global' ? data.current : null,
      queued:(data.queued || []).filter(row => row.definition_ref_json?.scope === 'global'),
      invalid:(data.invalid || []).filter(row => row.namespace === 'global')} : data;
    rtRuns = global ? logs.filter(row => row.scope === 'global') : logs;
    rtError = null;
    rtRender();
  } catch (error) {
    if (current()) { rtError = error.message || 'Could not load routines'; rtData = null; rtRuns = null; rtRender(); }
  }
}
function rtRows() {
  const seen = new Set();
  return (rtData && rtData.routines || []).filter(row => {
    const key = rtKey(row); if (seen.has(key)) return false; seen.add(key); return true;
  }).sort((a,b) => (a.status === 'paused') - (b.status === 'paused') || String(a.next || '').localeCompare(String(b.next || '')) || a.name.localeCompare(b.name));
}
function rtNext(value,now=Date.now()) {
  const hours=(Date.parse(value)-now)/3600000;
  if(!Number.isFinite(hours))return 'Not scheduled';
  if(hours<=0)return 'Due now';
  if(hours<1)return 'In less than 1 hour';
  return hours<48?`In ${Math.ceil(hours)} hours`:`In ${Math.ceil(hours/24)} days`;
}
function rtChart(row){
  if(!rtData?.hourly)return '<span class="rtchart-empty">History unavailable</span>';
  const slots=rtData.hourly.routines[rtKey(row)] || Array.from({length:48},(_,i)=>({hour:new Date(Date.parse(rtData.hourly.start)+i*3600000).toISOString(),succeeded:0,failed:0}));
  const max=Math.max(1,...slots.map(x=>x.succeeded));
  const points=slots.map((x,i)=>({x:22+i*218/47,y:64-x.succeeded/max*60}));
  return `<svg class="global-spark rtchart" viewBox="0 0 244 70" role="img" aria-label="Successful runs per hour over the last 48 hours; red dots show failures"><path class="global-spark-grid" d="M22 64H240"/><polyline class="global-spark-line" points="${points.map(p=>p.x+','+p.y).join(' ')}"/>${slots.map((slot,i)=>`<circle class="${slot.failed?'rt-failure':'global-spark-point'}" cx="${points[i].x}" cy="${points[i].y}" r="${slot.failed?3:2}"><title>${esc(slot.hour)}: ${slot.succeeded} succeeded, ${slot.failed} failed</title></circle>`).join('')}</svg>`;
}
function rtRoutineRow(row) {
  const next = row.status === 'paused' ? 'Paused' : row.scheduling_available === false ? row.scheduling_unavailable_reason || 'Scheduling unavailable' : rtNext(row.next);
  const current = rtData.current && rtData.current.key === row.key;
  return `<article class="rtrow" data-key="${esc(rtKey(row))}">
    ${rtScope(row.scope)}<button class="rttitle" data-action="detail" title="${esc(row.name)}">${esc(row.name)}</button>
    <div class="rtmeta" title="${esc(next)}">${current ? '<span class="rtok">Running</span><span class="sep">·</span>' : ''}${esc(next)}</div>
    <div class="rtchart-wrap">${rtChart(row)}</div><div class="rtrowactions"><button class="jricon" data-action="run" aria-label="Run ${esc(row.name)} now" title="${esc(row.unavailable_reason || 'Run now')}" ${!rtData.enabled || row.available === false ? 'disabled' : ''}>${RT_ICONS.play}</button>
    <button class="jricon" data-action="more" aria-label="Actions for ${esc(row.name)}" title="Routine actions">${RT_ICONS.more}</button></div></article>`;
}
function rtRunRow(run, index) {
  const costTitle = (run.scope === 'global' ? 'Total for this global run' : 'Cost of this business run') + (rtCostNote(run) ? ' · ' + rtCostNote(run) : '');
  return `<article class="rtrow rtrunrow" data-run="${index}">
    ${rtScope(run.scope)}<button class="rttitle" data-action="run-detail" title="${esc(run.name || run.routine)}">${esc(run.name || run.routine)}</button>
    <div class="rtmeta" title="${esc(rtTime(run.start, run.zone))}"><span class="${run.ok ? 'rtok' : 'rtfail'}">${rtRunState(run)}</span><span class="sep">·</span><time>${esc(rtTime(run.start, run.zone))}</time><span class="sep">·</span>${esc(run.seconds)}s</div>
    <span class="rtprice" title="${esc(costTitle)}">${rtMoney(run.usd)}</span></article>`;
}
function rtCostNote(run) {
  if (typeof run.usage_complete === 'boolean') return run.usage_complete ? '' : 'Some usage was not reported; the recorded cost may be incomplete.';
  return (run.runs || []).some(x => !x.model) ? 'Some usage was not reported; the recorded cost may be incomplete.' : '';
}
function rtRunState(run) {
  return run.running ? 'Running' : run.ok ? 'Succeeded' : run.state === 'cancelled' ? 'Cancelled' : 'Failed';
}
function rtRender() {
  if (!rtVisible()) return;
  const body = $('#tbody'); if (!body) return;
  body.classList.add('rt');
  if (!rtGlobal() && !rtContextBiz()) { body.innerHTML = '<div class="jrfaint jrfaint-pad">Pick a business first.</div>'; return; }
  const rows = rtRows(), active = rows.filter(r => r.status === 'active'), paused = rows.length - active.length;
  const logs = Array.isArray(rtRuns) ? rtRuns : [];
  const visible = rtShowPaused ? rows : active;
  const isLog = rtPage === 'logs';
  const current = rtData && rtData.current, queued = rtData && rtData.queued || [];
  const status = rtError ? 'Unavailable' : !rtData ? 'Loading…' : !rtData.enabled ? 'Runner disabled' : current ? `${current.routine} running · ${current.seconds}s` : queued.length ? `${queued.length} queued` : '';
  const problem = rtError || rtData && rtData.error || isLog && rtRuns && rtRuns.error;
  body.innerHTML = `<div class="rtworkspace"><section class="rtcontent"><header class="jrhhead"><div class="rttoggle" role="group" aria-label="Routines view">
    <button class="jricon ${!isLog?'on':''}" data-page="active" aria-label="Routines" aria-pressed="${!isLog}">${RT_ICONS.active}</button>
    <button class="jricon ${isLog?'on':''}" data-page="logs" aria-label="Recent runs" aria-pressed="${isLog}">${RT_ICONS.logs}</button></div>
    ${`<button class="btn" id="rtnew" ${!rtData || rtGlobal() && !rtData.root ? 'disabled' : ''}>${ICO_PLUS}New routine</button>`}</header>
    ${!isLog ? `<div class="rttoolbar"><span class="rtmuted" title="${esc(rtData && rtData.last_sweep || '')}">${esc(status)}</span>${current ? '<button class="btn quiet" id="rtkill">Stop run</button>' : ''}${paused ? `<label class="rtpaused"><input id="rtpaused" type="checkbox" ${rtShowPaused ? 'checked' : ''}>Show paused</label>` : ''}</div>` : ''}
    ${problem ? `<p class="rtfail" role="alert">${esc(problem)} <button class="btn quiet" id="rtretry">Retry</button></p>` : ''}
    <div class="rtlist">${!rtData ? (rtError ? '' : '<p class="jrfaint jrfaint-pad">Loading routines…</p>') : isLog ? (logs.length ? logs.map(rtRunRow).join('') : '<p class="jrfaint jrfaint-pad">No runs yet.</p>') : (visible.length ? visible.map(rtRoutineRow).join('') : '<p class="jrfaint jrfaint-pad">No active routines.</p>')}</div>
    ${!isLog && rtData && (rtData.invalid || []).length ? `<details class="mkchain rtissues"><summary>${rtData.invalid.length} invalid definitions</summary>${rtData.invalid.map(x => `<p class="rtfail">${esc(x.routine_id || x.file)}: ${esc(x.error)}</p>`).join('')}</details>` : ''}
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
  const retry = $('#rtretry'); if (retry) retry.onclick = rtRefresh;
  const add = $('#rtnew'), show = $('#rtpaused'), kill = $('#rtkill');
  if (add) add.onclick = () => rtOpenForm();
  if (show) show.onchange = () => { rtShowPaused = show.checked; rtRender(); };
  if (kill) kill.onclick = () => rtStop(current, kill);
}
async function rtStop(current, button) {
  const biz = rtContextBiz(), definition = current.definition_ref_json || {};
  if (!current.id || !definition.routine_id) return;
  button.disabled = true;
  try {
    rtResult(await post(rtEndpoint(definition.scope === 'global', biz), {op:'stop', id:definition.routine_id,
      job_id:current.id, ...(current.business_id ? {target_business_id:current.business_id} : {})}));
    notify({text:'Stop requested',sub:definition.routine_id});
    if (rtContextBiz() === biz) rtRefresh();
  } catch (error) { notify({text:error.message,color:'var(--danger)'}); }
  finally { button.disabled = false; }
}
function rtMenu(button, row) {
  dropdown(button, [{value:'edit',label:'Edit routine'}, {value:row.status === 'paused' ? 'resume' : 'pause',label:row.status === 'paused' ? 'Resume' : 'Pause'}, {value:'rm',label:'Delete routine'}], action => rtAction(action, row));
}
function rtNotify(text, row, error = false, biz = state.biz) {
  notify({text, sub:row.scope === 'global' ? 'Global' : biz, ...(error ? {color:'var(--danger)'} : {})});
}
async function rtAction(action, row) {
  const biz = rtContextBiz();
  if (action === 'edit') return rtOpenForm(row);
  if (action === 'rm') {
    rtDialog = modal({title:'Delete routine?', body:`<p>${esc(row.name)}</p><p class="rtmuted">Past runs remain in the log.</p>`, actions:[
      {label:'Cancel',onClick:close=>close()},
      {label:'Delete',primary:true,onClick:async close=>{ close(); await rtWrite('rm',row,biz); }},
    ]});
    return;
  }
  if (action !== 'run') return rtWrite(action, row, biz);
  return rtWrite('run', row, biz);
}
async function rtWrite(op, row, biz) {
  try {
    rtResult(await post(rtEndpoint(row.scope === 'global', biz), {op,id:row.id,expected_revision:row.revision}));
    rtNotify(op === 'run' ? `Queued ${row.name}` : op === 'rm' ? 'Routine deleted' : op === 'pause' ? 'Routine paused' : 'Routine resumed', row, false, biz);
  } catch (error) { rtNotify(error.message, row, true, biz); }
  if (biz === rtContextBiz()) rtRefresh();
}
function rtDetail(row) {
  const fields = [
    ['Next scheduled',row.status === 'paused' ? 'Paused' : rtTime(row.next,row.zone)],
    ['Program',row.program], ['Arguments',JSON.stringify(row.arguments || {})], ['Departments',(row.departments || []).map(departmentLabel).join(', ')],
    ['Timeout',row.timeout + ' seconds'], ['Target',row.target || 'business'],
    ...(row.scheduling_available === false ? [['Scheduling',row.scheduling_unavailable_reason || 'Unavailable']] : []),
  ];
  rtDialog = modal({title:esc(row.name),width:620,body:`<div class="tdetail rtdetail"><div class="rtscope">${rtScope(row.scope)}</div><div class="flds">${fields.map(([label,value])=>`<span class="k">${label}</span><span class="v">${esc(value)}</span>`).join('')}</div><p>${esc(row.why)}</p></div>`,actions:[
    {label:'Close',onClick:close=>close()},
    {label:'Edit',onClick:close=>{close();rtOpenForm(row);}},
    ...(rtData.enabled && row.available !== false ? [{label:'Run now',primary:true,onClick:close=>{close();rtAction('run',row);}}] : []),
  ]});
}
function rtRunDetail(run) {
  if (!run) return;
  const result = rtRunState(run);
  const models = [...new Set((run.runs || []).map(x => x.model).filter(Boolean))].join(', ');
  rtDialog = modal({title:esc(run.name || run.routine),width:680,body:`<div class="tdetail rtdetail"><div class="rtscope">${rtScope(run.scope)}<span class="${run.ok ? 'rtok' : 'rtfail'}">${result}</span><span class="rtprice">${rtMoney(run.usd)}</span></div>
    <div class="flds"><span class="k">Started</span><span class="v">${esc(rtTime(run.start,run.zone))}</span><span class="k">Duration</span><span class="v">${esc(run.seconds)} seconds</span>${models ? `<span class="k">Model</span><span class="v">${esc(models)}</span>` : ''}</div>
    ${rtCostNote(run) ? `<p class="rtmuted">${rtCostNote(run)}</p>` : ''}
    ${(run.runs || []).map(x=>`<details class="mkchain rtoutput" ${(run.runs || []).length === 1 ? 'open' : ''}><summary>${esc(x.slug === 'root' ? 'Businesses root' : x.slug)} · ${rtRunState({...x,running:['queued','running','applying','waiting_signoff','cancel_requested'].includes(x.state)})}${x.rc != null ? ' · rc ' + esc(x.rc) : ''}</summary><pre>${esc(x.stdout_tail || 'No additional output.')}${x.stderr_tail ? '\n\nError: ' + esc(x.stderr_tail) : ''}${x.git_error ? '\nCommit error: ' + esc(x.git_error) : ''}</pre></details>`).join('')}</div>`,actions:[{label:'Close',onClick:close=>close()}]});
}
function rtFormHTML(row = {}) {
  const input = (key,label,value,extra='') => `<label>${label}<input class="inp" name="${key}" value="${esc(String(value == null ? '' : value))}" ${extra}></label>`;
  return `<form id="rtform" class="rtform"><div class="rtscopepick"><span class="rtmuted">Scope</span>${(rtGlobal() ? ['global'] : ['biz','global']).map(scope=>`<button type="button" class="lnscope ${scope === (row.scope || 'biz') ? 'on' : ''}" data-scope="${scope}" title="${esc(LN_SCOPE_BY_KEY[scope].label)}" aria-label="${esc(LN_SCOPE_BY_KEY[scope].label)}" ${(row.id && scope !== row.scope) || (scope === 'global' && !rtData.root) ? 'disabled' : ''}>${LN_SCOPE_BY_KEY[scope].ico}</button>`).join('')}</div><div class="rtfields">
    ${input('name','Name',row.name,'required maxlength="80"')}
    <label>Program<select class="inp" name="program" required></select></label>
    <div id="rtarguments" class="rtwide rtarguments"></div><p id="rtprogramerror" class="rtwide rtfail" role="status"></p>
    ${input('when','Schedule',row.when || '17 4 * * *','required')}${input('tz','Timezone',row.tz || 'UTC','required')}
    <div id="rtpreview" class="rtwide rtmuted" aria-live="polite">Checking next scheduled times…</div>
    <label id="rttarget" ${row.scope !== 'global' ? 'hidden' : ''}>Run in<select class="inp" name="target"><option value="each-business" ${row.target !== 'root' ? 'selected' : ''}>Each business</option><option value="root" ${row.target === 'root' ? 'selected' : ''}>Businesses root</option></select></label>
    ${input('timeout','Timeout (seconds)',row.timeout || 900,'type="number" min="60" max="7200" required')}
    <fieldset class="rtwide"><legend>Departments</legend>${Object.keys(DEPTS).map(d=>`<label class="rtcheck"><input type="checkbox" name="departments" value="${d}" ${(row.departments || ['operations']).map(departmentId).includes(d) ? 'checked' : ''}>${departmentTag(d)}</label>`).join('')}</fieldset>
    <label class="rtwide">Why<textarea class="inp" name="why" required>${esc(row.why || '')}</textarea></label></div><p id="rterror" class="rtfail" role="alert"></p></form>`;
}
function rtOpenForm(row = null) {
  const draft = {scope:row && row.scope || (rtGlobal() ? 'global' : 'biz'),biz:rtContextBiz(),key:rtContextKey()};
  const save = async (close, el) => {
    const form = el.querySelector('#rtform'); if (!form.reportValidity()) return;
    const data = new FormData(form), departments = data.getAll('departments');
    if (!departments.length) { form.querySelector('#rterror').textContent = 'Choose at least one department.'; return; }
    if (rtContextKey() !== draft.key) { form.querySelector('#rterror').textContent = 'The review context changed. Reopen this form.'; return; }
    let argumentsValue;
    try { argumentsValue = rtReadArguments(form); } catch (error) { form.querySelector('#rterror').textContent = error.message; return; }
    const program = (rtData.programs || []).find(p => p.id === data.get('program'));
    if (!program || program.unavailable_reason) { form.querySelector('#rterror').textContent = program && program.unavailable_reason || 'Select an available program.'; return; }
    const definition = {name:data.get('name'),program:program.id,arguments:argumentsValue,schedule:data.get('when'),timezone:data.get('tz'),
      timeout_seconds:Number(data.get('timeout')),departments,why:data.get('why'),status:row && row.status || 'active',
      target:draft.scope === 'global' ? data.get('target') : 'business'};
    const payload = {op:row ? 'set' : 'add',definition,
      ...(row ? {id:row.id,expected_revision:row.revision} : {})};
    const button = el.querySelector('.mfoot .primary'); button.disabled = true;
    try {
      const result = await post(rtEndpoint(draft.scope === 'global',draft.biz),payload);
      rtResult(result);
      close(); rtNotify('Routine saved',{scope:draft.scope},false,draft.biz);
      if (draft.key === rtContextKey()) rtRefresh();
    } catch (error) {
      form.querySelector('#rterror').textContent = error.message || 'Could not save routine. Please try again.';
    } finally { button.disabled = false; }
  };
  rtDialog = modal({title:row ? 'Edit routine' : 'New routine',width:640,body:rtFormHTML(row || {scope:draft.scope}),actions:[{label:'Cancel',onClick:close=>close()},{label:'Save routine',primary:true,onClick:save}],onOpen:el=>{
    const form = el.querySelector('#rtform');
    const preview = () => rtPreview(form,draft.scope === 'global',draft.biz);
    const programs = () => rtProgramChoices(form, draft.scope, row);
    programs();
    form.elements.program.onchange = () => rtArgumentFields(form, {});
    form.elements.target.onchange = programs;
    for (const name of ['when','tz']) form.elements[name].oninput = () => { clearTimeout(rtPreviewTimer); rtPreviewTimer = setTimeout(preview,300); };
    form.querySelectorAll('[data-scope]').forEach(button=>button.onclick=()=>{
      draft.scope=button.dataset.scope;form.querySelectorAll('[data-scope]').forEach(b=>b.classList.toggle('on',b===button));form.querySelector('#rttarget').hidden=draft.scope!=='global';programs();preview();
    });
    form.onsubmit = e => { e.preventDefault(); el.querySelector('.mfoot .primary').click(); };
    preview();
  }});
}
async function rtPreview(form, global, biz) {
  const generation = ++rtPreviewGeneration;
  const q = new URLSearchParams({when:form.elements.when.value,tz:form.elements.tz.value});
  try {
    const result = await api(rtEndpoint(global,biz) + '/next?' + q);
    if (generation !== rtPreviewGeneration || !form.isConnected || biz !== rtContextBiz()) return;
    form.querySelector('#rtpreview').textContent = result.error || (result.next || []).map(x=>x.local).join(' · ');
  } catch (error) { if (form.isConnected) form.querySelector('#rtpreview').textContent = error.message; }
}

function rtProgramChoices(form, scope, row) {
  const target = scope === 'global' && form.elements.target.value === 'root' ? 'global' : 'business';
  const selected = form.elements.program.value || row && row.program;
  const programs = (rtData.programs || []).filter(p => (p.scope_kinds || []).includes(target));
  form.elements.program.innerHTML = '<option value="">Select a program</option>' + programs.map(p=>`<option value="${esc(p.id)}" ${p.id === selected ? 'selected' : ''}>${esc(p.id)}${p.unavailable_reason ? ' (unavailable)' : ''}</option>`).join('');
  rtArgumentFields(form, row && form.elements.program.value === row.program ? row.arguments || {} : {});
}
function rtArgumentFields(form, values) {
  const program = (rtData.programs || []).find(p => p.id === form.elements.program.value);
  const schema = program && program.input_schema || {};
  form.querySelector('#rtprogramerror').textContent = program && program.unavailable_reason || '';
  form.querySelector('#rtarguments').innerHTML = Object.entries(schema.properties || {}).map(([key, field]) => {
    const required = (schema.required || []).includes(key), value = values[key] ?? field.default ?? '';
    const type = Array.isArray(field.type) ? field.type.find(t => t !== 'null') : field.type;
    const attrs = `data-argument="${esc(key)}" data-type="${esc(type || 'json')}" ${required && type !== 'boolean' ? 'required' : ''}`;
    const label = esc(field.title || key.replace(/_/g, ' '));
    if (type === 'boolean') return `<label class="rtcheck"><input type="checkbox" ${attrs} ${value === true ? 'checked' : ''}>${label}</label>`;
    if (field.enum) return `<label>${label}<select class="inp" ${attrs}>${required ? '' : '<option value=""></option>'}${field.enum.map(v=>`<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>`;
    if (['integer','number','string'].includes(type)) return `<label>${label}<input class="inp" ${attrs} type="${type === 'string' ? 'text' : 'number'}" value="${esc(value)}" ${type === 'integer' ? 'step="1"' : type === 'number' ? 'step="any"' : ''} ${field.minimum != null ? `min="${Number(field.minimum)}"` : ''} ${field.maximum != null ? `max="${Number(field.maximum)}"` : ''} ${field.maxLength ? `maxlength="${Number(field.maxLength)}"` : ''}></label>`;
    return `<label class="rtwide">${label}<textarea class="inp" ${attrs} spellcheck="false">${esc(value === '' ? '' : JSON.stringify(value, null, 2))}</textarea></label>`;
  }).join('');
}
function rtReadArguments(form) {
  const result = {};
  form.querySelectorAll('[data-argument]').forEach(input => {
    const key = input.dataset.argument, type = input.dataset.type;
    if (type === 'boolean') { result[key] = input.checked; return; }
    if (input.value === '' && !input.required) return;
    if (type === 'integer' || type === 'number') { result[key] = Number(input.value); return; }
    if (type === 'string') { result[key] = input.value; return; }
    try { result[key] = JSON.parse(input.value); } catch (_) { throw new Error(`${key.replace(/_/g, ' ')} must be valid JSON.`); }
  });
  return result;
}
function rtFailureToasts(rows) {
  let prior;
  try { prior=JSON.parse(localStorage.getItem('routine-failures-seen') || '[]'); } catch (_) { prior=[]; }
  const seen=new Set((Array.isArray(prior) ? prior : []).filter(x=>typeof x === 'string' && x));
  (rows || []).slice().reverse().forEach(row=>{
    const id = row.id || row.run;
    if (!id || seen.has(id)) return;
    const detail = row.error && row.error.message || (row.runs || []).map(x=>x.stderr_tail).find(Boolean);
    const scope = row.scope === 'global' ? 'All businesses' : state.biz;
    seen.add(id);notify({text:`Routine failed: ${row.name || row.routine}`,sub:[scope,detail || (row.rc != null ? `rc ${row.rc}` : ''),row.end].filter(Boolean).join(' · '),color:'var(--danger)'});
  });
  localStorage.setItem('routine-failures-seen',JSON.stringify([...seen].slice(-500)));
}
