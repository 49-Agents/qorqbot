// Program routines: right-panel registry, manual queue, history and definition editor.
let rtData = null, rtCosts = null, rtBiz = null, rtForm = null, rtGeneration = 0;
let rtExpanded = new Map(), rtPreviewTimer = null, rtPreviewGeneration = 0;
const rtMoney = n => '$' + Number(n || 0).toFixed(4);
const rtCommand = argv => (argv || []).map(a => /^[a-zA-Z0-9_./=-]+$/.test(a) ? a : "'" + a.replace(/'/g, "'\\''") + "'").join(' ');
function rtAge(value) {
  if (!value) return 'never';
  const seconds = Math.max(0, (Date.now() - Date.parse(value)) / 1000);
  return seconds < 60 ? 'just now' : seconds < 3600 ? Math.floor(seconds / 60) + 'm ago' : seconds < 86400 ? Math.floor(seconds / 3600) + 'h ago' : Math.floor(seconds / 86400) + 'd ago';
}
function rtEndpoint(global) { return global ? '/root/routines' : `/biz/${encodeURIComponent(state.biz)}/routines`; }
function tPaintRoutines() {
  if (rtBiz !== state.biz) {
    rtBiz = state.biz; rtData = null; rtCosts = null; rtForm = null; rtExpanded.clear(); rtGeneration++;
  }
  rtRender(); rtRefresh();
}
async function rtRefresh() {
  if (tView !== 'routines' || !state.biz) return;
  const biz = state.biz, generation = ++rtGeneration;
  try {
    const [data, costs] = await Promise.all([api(`/biz/${encodeURIComponent(biz)}/routines`), api('/routines/costs?days=30')]);
    if (biz !== state.biz || generation !== rtGeneration || tView !== 'routines') return;
    rtData = data; rtCosts = costs;
    if (!rtForm) rtRender();
  } catch (e) { if (biz === state.biz) toast('Could not load routines'); }
}
function rtRow(row, i, local) {
  const key = `${local ? 'local' : 'global'}-${i}`, last = row.last_run;
  const readonly = local && row.scope === 'global';
  return `<article class="rtrow" data-i="${i}" data-local="${local ? '1' : ''}">
    <div class="rtmain"><button class="rttitle" data-action="expand" aria-expanded="${rtExpanded.has(key)}">${esc(row.name)}</button>
    ${row.scope === 'global' ? '<span class="rtchip">global</span>' : ''}${row.status === 'paused' ? '<span class="rtchip">paused</span>' : ''}</div>
    <div class="rtmeta"><code>${esc(row.when)}</code><span>next ${esc(row.next_local || row.next || '—')}</span><span>${esc(row.zone || '')}</span>
    <span class="${last ? (last.ok ? 'rtok' : 'rtfail') : ''}">${last ? (last.ok ? 'ok' : 'failed · rc ' + last.rc) : 'never run'}${last ? ' · ' + rtAge(last.end) : ''}</span>
    ${row.target ? `<span>${esc(row.target)}</span>` : ''}</div>
    <div class="rtbottom"><div class="rtchips">${row.departments.map(d => `<span class="rtchip">${esc(d)}</span>`).join('')}</div>
    <div class="rtactions"><button data-action="run" ${!rtData.enabled ? 'disabled' : ''}>Run now</button>
    ${readonly ? '<span class="rtmuted">Edit below in Global</span>' : `<button data-action="${row.status === 'paused' ? 'resume' : 'pause'}">${row.status === 'paused' ? 'Resume' : 'Pause'}</button><button data-action="edit">Edit</button><button data-action="rm">Delete</button>`}</div></div>
    ${rtExpanded.has(key) ? `<div class="rthistory">${rtHistoryHTML(rtExpanded.get(key))}</div>` : ''}</article>`;
}
function rtHistoryHTML(runs) {
  if (runs === null) return '<p class="rtmuted">Loading runs…</p>';
  if (runs.error) return `<p class="rtfail">${esc(runs.error)}</p>`;
  if (!runs.length) return '<p class="rtmuted">No runs yet.</p>';
  return runs.map(r => `<section class="rtrun"><div class="rtmeta"><span class="${r.ok ? 'rtok' : 'rtfail'}">${r.ok ? 'ok' : 'failed'} · rc ${esc(String(r.rc))}</span>
    <time>${esc(r.start)}</time><span>${esc(r.slug)} · ${esc(String(r.seconds))}s</span><span>${rtMoney(r.usd)}${r.model ? ' · ' + esc(r.model) : ' · no token report'}</span>
    ${r.commit ? `<code>${esc(r.commit)}</code>` : ''}${r.missed ? `<span>${r.missed} missed slots</span>` : ''}</div>
    ${r.usage ? `<div class="rtmuted">Tokens: ${r.usage.hit} cached · ${r.usage.miss} uncached · ${r.usage.out} output</div>` : ''}
    <pre>${esc(r.stdout_tail || '(no stdout)')}\n--- stderr ---\n${esc(r.stderr_tail || '(none)')}${r.git_error ? '\nCommit error: ' + esc(r.git_error) : ''}</pre></section>`).join('');
}
function rtRender() {
  if (tView !== 'routines') return;
  const body = $('#tbody'); if (!body) return;
  if (!state.biz) { body.innerHTML = '<div class="tempty">Pick a business first.</div>'; return; }
  if (!rtData) { body.innerHTML = '<div class="rtwrap rtmuted">Loading routines…</div>'; return; }
  if (rtForm) { body.innerHTML = '<div class="rtwrap">' + rtFormHTML() + '</div>'; rtWireForm(); return; }
  const rows = rtData.routines || [], current = rtData.current;
  const local = rows.map((r, i) => ({r, i})).filter(x => x.r.scope === 'biz' || x.r.target === 'each-business');
  const global = rows.map((r, i) => ({r, i})).filter(x => x.r.scope === 'global');
  const business = (state.bizs || []).find(b => b.slug === state.biz);
  const bid = business && business.id || state.biz;
  const costs = rtCosts && !rtCosts.error ? `Last 30 days · ${esc(state.biz)} ${rtMoney((rtCosts.businesses || {})[bid])} · all businesses ${rtMoney(rtCosts.usd)}${rtCosts.unreported ? ' · ' + rtCosts.unreported + ' runs without token reports' : ''}` : 'Cost history unavailable';
  body.innerHTML = `<div class="rtwrap"><div class="rtrunner"><strong>${rtData.enabled ? 'Runner enabled' : 'Runner disabled'}</strong>
    <span class="rtmuted">checked ${rtAge(rtData.last_sweep)} · ${(rtData.queued || []).length} queued</span>
    ${current ? `<div>${esc(current.routine)} · ${esc(current.slug)} · ${current.seconds}s <button id="rtkill">Stop run</button></div>` : ''}
    ${rtData.error ? `<p class="rtfail">${esc(rtData.error)}</p>` : ''}
    ${(rtData.queued || []).length ? `<div class="rtmuted">${rtData.queued.map(q => `#${q.position} ${esc(q.routine)} · ${esc(q.slug)}`).join('<br>')}</div>` : ''}</div>
    <div class="rtcosts">${costs}</div>
    ${rtForm ? rtFormHTML() : ''}
    <div class="rtheading"><h2>${esc(state.biz)}</h2><button id="rtnewbiz">New routine</button></div>
    ${local.length ? local.map(x => rtRow(x.r, x.i, true)).join('') : '<p class="rtmuted">No routines for this business yet.</p>'}
    <div class="rtheading"><h2>Global</h2>${rtData.root ? '<button id="rtnewglobal">New global routine</button>' : ''}</div>
    ${!rtData.root ? `<p class="rtmuted">${esc(rtData.root_reason || 'Global routines are not configured.')}</p>` : global.length ? global.map(x => rtRow(x.r, x.i, false)).join('') : '<p class="rtmuted">No global routines yet.</p>'}
    ${(rtData.invalid || []).length ? `<h2>Invalid files</h2>${rtData.invalid.map(x => `<p class="rtfail">${esc(x.location)}/${esc(x.file)}: ${esc(x.error)}</p>`).join('')}` : ''}
    ${(rtData.recent || []).some(x => x.skipped || x.error) ? `<h2>Runner notes</h2>${rtData.recent.filter(x => x.skipped || x.error).slice(0, 5).map(x => `<p class="rtmuted">${esc(x.routine || '')} · ${esc(x.slug || '')}: ${esc(x.skipped || x.error)}</p>`).join('')}` : ''}</div>`;
  body.querySelectorAll('.rtrow').forEach(el => el.onclick = e => {
    const action = e.target.closest('[data-action]');
    if (action) rtAction(action.dataset.action, Number(el.dataset.i), el.dataset.local === '1');
  });
  const b = $('#rtnewbiz'), g = $('#rtnewglobal'), kill = $('#rtkill');
  if (b) b.onclick = () => rtOpenForm(null, false);
  if (g) g.onclick = () => rtOpenForm(null, true);
  if (kill) kill.onclick = async () => { const r = await post('/routines/kill', {}); if (r.error) toast(r.error); else rtRefresh(); };
  if (rtForm) rtWireForm();
}
async function rtAction(action, i, local) {
  const row = (rtData.routines || [])[i]; if (!row) return;
  const global = row.scope === 'global';
  if (action === 'edit') return rtOpenForm(row, global);
  if (action === 'expand') {
    const key = `${local ? 'local' : 'global'}-${i}`;
    if (rtExpanded.has(key)) { rtExpanded.delete(key); return rtRender(); }
    rtExpanded.set(key, null); rtRender();
    const biz = state.biz;
    const endpoint = local ? rtEndpoint(false) : rtEndpoint(true);
    const runs = await api(`${endpoint}/${encodeURIComponent(row.id)}/runs?n=20&scope=${row.scope}`);
    if (biz === state.biz && rtExpanded.has(key)) { rtExpanded.set(key, runs); rtRender(); }
    return;
  }
  if (action === 'rm' && !confirm(`Delete “${row.name}”? Its run history stays in git.`)) return;
  const result = action === 'run' ? await post('/routines/run', {location: row.location, id: row.id, ...(local ? {slug: state.biz} : {})})
    : await post(rtEndpoint(global), {op: action, id: row.id});
  if (result.error) return toast(result.error);
  toast(result.warn || (action === 'run' ? `Queued at position ${result.position}` : 'Routine updated'));
  rtExpanded.clear(); rtRefresh();
}
function rtOpenForm(row, global) {
  rtForm = {row, global}; rtRender(); rtPreview();
}
function rtFormHTML() {
  const row = rtForm.row || {}, global = rtForm.global;
  const input = (key, label, value, extra = '') => `<label>${label}<input name="${key}" value="${esc(String(value == null ? '' : value))}" ${extra}></label>`;
  return `<form id="rtform" class="rtform"><h2>${row.id ? 'Edit' : 'New'} ${global ? 'global routine' : 'routine for ' + esc(state.biz)}</h2><div class="rtfields">
    ${input('id', 'ID', row.id, row.id ? 'readonly required' : 'required pattern="[a-z0-9][a-z0-9-]{1,39}"')}
    ${input('name', 'Name', row.name, 'required maxlength="80"')}
    <label class="rtwide">Command<input name="run" value="${esc(rtCommand(row.run))}" required placeholder="git status --short"></label>
    ${input('when', 'Schedule (cron)', row.when || '17 4 * * *', 'required')}${input('tz', 'Timezone (blank uses default)', row.tz || '')}
    <div id="rtpreview" class="rtwide rtmuted" aria-live="polite">Checking next three runs…</div>
    ${global ? `<label>Run in<select name="target"><option value="each-business" ${row.target !== 'root' ? 'selected' : ''}>Each business</option><option value="root" ${row.target === 'root' ? 'selected' : ''}>Businesses root</option></select></label>` : ''}
    ${input('timeout', 'Timeout (seconds)', row.timeout || 900, 'type="number" min="60" max="7200" required')}
    <fieldset class="rtwide"><legend>Departments</legend>${['marketing','accounting','data-science','code','general','onbashlyk'].map(d => `<label class="rtcheck"><input type="checkbox" name="departments" value="${d}" ${(row.departments || ['onbashlyk']).includes(d) ? 'checked' : ''}>${d}</label>`).join('')}</fieldset>
    <label class="rtcheck rtwide"><input type="checkbox" name="skip_if_live" ${row.skip_if_live ? 'checked' : ''}>Wait while agents are active (up to six hours)</label>
    ${input('commit', 'Output paths to commit (comma-separated)', (row.commit || []).join(', '))}
    <label class="rtwide">Why<textarea name="why" required>${esc(row.why || '')}</textarea></label></div>
    <p id="rterror" class="rtfail" role="alert"></p><div class="rtactions"><button type="submit">Save routine</button><button type="button" id="rtcancel">Cancel</button></div></form>`;
}
function rtWireForm() {
  const form = $('#rtform'); if (!form) return;
  $('#rtcancel').onclick = () => { rtForm = null; rtPreviewGeneration++; rtRender(); };
  for (const name of ['when','tz']) form.elements[name].oninput = () => { clearTimeout(rtPreviewTimer); rtPreviewTimer = setTimeout(rtPreview, 300); };
  form.onsubmit = async e => {
    e.preventDefault();
    const data = new FormData(form), global = rtForm.global, biz = state.biz;
    const departments = data.getAll('departments');
    if (!departments.length) { $('#rterror').textContent = 'Choose at least one department.'; return; }
    const payload = {op: rtForm.row ? 'set' : 'add', id: data.get('id'), name: data.get('name'), run: data.get('run'),
      when: data.get('when'), tz: data.get('tz'), timeout: Number(data.get('timeout')), departments,
      skip_if_live: data.has('skip_if_live'), commit: String(data.get('commit') || '').split(',').map(p => p.trim()).filter(Boolean), why: data.get('why')};
    if (global) payload.target = data.get('target');
    const submit = form.querySelector('[type="submit"]'); submit.disabled = true;
    try {
      const result = await post(rtEndpoint(global), payload);
      if (biz !== state.biz) return;
      if (result.error) { $('#rterror').textContent = result.error; return; }
      rtForm = null; toast(result.warn || 'Routine saved'); rtRefresh();
    } finally { submit.disabled = false; }
  };
}
async function rtPreview() {
  const form = $('#rtform'); if (!form || !rtForm) return;
  const generation = ++rtPreviewGeneration, biz = state.biz;
  const q = new URLSearchParams({when: form.elements.when.value, tz: form.elements.tz.value});
  const result = await api(rtEndpoint(rtForm.global) + '/next?' + q);
  if (generation !== rtPreviewGeneration || biz !== state.biz || !rtForm) return;
  const target = $('#rtpreview');
  if (target) target.textContent = result.error || 'Next: ' + (result.next || []).map(x => x.local).join(' · ');
}
function rtFailureToasts(rows) {
  let prior;
  try { prior = JSON.parse(localStorage.getItem('routine-failures-seen') || '[]'); } catch (_) { prior = []; }
  const seen = new Set(Array.isArray(prior) ? prior : []);
  (rows || []).slice().reverse().forEach(row => {
    if (seen.has(row.run)) return;
    seen.add(row.run); notify({text: `Routine failed: ${row.routine}`, sub: `rc ${row.rc} · ${row.end}`, color: 'var(--danger)'});
  });
  localStorage.setItem('routine-failures-seen', JSON.stringify([...seen].slice(-500)));
}
