// ---------------------------------------------------------------- context sources
// Source documents (uploads, dumps, filed notes and journals) and what the Context queue did with them.
// Everything here is read from the recorded `filing` projection, the owner diagnostics (`context_upload_map`,
// `context_filings`) and the `context_queue` process summary. Nothing is inferred from file paths, names or
// generated maps, and a submission is never reported as publication.
//
// A scope is {kind: 'business', id: slug} or {kind: 'root'}; every call below is made in exactly that namespace.
let srcMountGeneration = 0;
const SRC_PAGE_CHARS = 6000, SRC_DIAGNOSTIC_LIMIT = 100, SRC_FILINGS_LIMIT = 25, SRC_LIST_LIMIT = 30;
const SRC_REFRESH_MS = 20000, SRC_REFRESH_MAX = 30;
const SRC_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const srcScopeKey = scope => scope.kind === 'root' ? 'root' : `biz:${scope.id}`;
const srcOpPath = (scope, op) => scope.kind === 'root' ? `/root/operations/${op}` : `/biz/${encodeURIComponent(scope.id)}/operations/${op}`;
const srcRead = (scope, op, args, signal = pageReads.signal) => pageReadPost(srcOpPath(scope, op), args, signal).then(coreData);
const srcSourceRoute = (scope, id) => scope.kind === 'root' ? `#/global/context/library/${encodeURIComponent(id)}` : `_source/${id}`;

// ------------------------------------------------------------- filing presentation
// state + completion_state + operation -> one label. `status` is only the derived summary and never decides the label.
function srcFilingLabel(filing) {
  if (!filing || typeof filing !== 'object') return {key: 'unavailable', label: 'Status unavailable', tone: 'warn'};
  const state = filing.state || 'unfiled';
  const table = {unfiled: ['Unfiled', 'idle'], queued: ['Queued', 'run'], processing: ['Processing', 'run'], blocked: ['Blocked', 'bad'],
    discard_requested: ['Discard requested', 'warn'], discarded: ['Discarded', 'bad']};
  if (table[state]) return {key: state, label: table[state][0], tone: table[state][1]};
  if (state === 'published') {
    if (filing.completion_state === 'conflict') return {key: 'completion_conflict', label: 'Published—completion conflict', tone: 'bad'};
    if (filing.completion_state === 'applied') return filing.operation === 'remove'
      ? {key: 'withdrawn', label: 'Withdrawn', tone: 'ok'} : {key: 'filed', label: 'Filed', tone: 'ok'};
    if (filing.completion_state === 'pending') return {key: 'completion_pending', label: 'Published—completion pending', tone: 'run'};
    return {key: 'completion_unknown', label: 'Published—completion status unavailable', tone: 'warn'};
  }
  return {key: 'unavailable', label: 'Status unavailable', tone: 'warn'};
}
// States that can still change without anyone acting: worth a bounded view-only refresh.
const srcActive = filing => ['queued', 'processing', 'discard_requested'].includes(filing?.state) ||
  (filing?.state === 'published' && filing.completion_state === 'pending');
function srcFilingChip(filing, extra = '') {
  const l = srcFilingLabel(filing);
  return `<span class="cnt src-state is-${esc(l.tone)}" data-src-state="${esc(l.key)}" title="${esc(l.label)}">${esc(l.label)}${extra}</span>`;
}
const srcFact = (label, value, selectable = false) => value === null || value === undefined || value === '' ? '' :
  `<div><dt>${esc(label)}</dt><dd${selectable ? ' class="src-sel"' : ''}>${esc(value)}</dd></div>`;
// The filing of the CURRENT source, as recorded. `snapshotNote` says what the filing is about for notes/journals.
function srcFilingFacts(filing, {currentRevision} = {}) {
  if (!filing) return '<p class="src-note" data-src-filing-missing>The filing status was not returned for this source.</p>';
  const l = srcFilingLabel(filing);
  const earlier = filing.source_current === false ? '<p class="src-note" data-src-earlier>This filing belongs to an earlier source revision. The current revision has its own state (or none yet).</p>' : '';
  const published = filing.published_commit && l.key !== 'filed' && l.key !== 'withdrawn'
    ? '<p class="src-note">The Context commit below is published and stays recorded while completion is unresolved.</p>' : '';
  return `<div class="src-filing" data-src-filing>
    <div class="src-filing-head">${srcFilingChip(filing)}${filing.status ? `<span class="src-summary" title="Derived summary only">summary: ${esc(filing.status)}</span>` : ''}</div>
    ${earlier}${published}
    <dl class="src-facts">${[
      srcFact('Operation', filing.operation), srcFact('Source revision', filing.source_revision), srcFact('Document revision', filing.document_revision ?? currentRevision),
      srcFact('Attempts', filing.attempts), srcFact('Queue position', filing.queue_position),
      srcFact('Job', [filing.job_id, filing.job_state].filter(Boolean).join(' · '), true), srcFact('Input', filing.input_id, true), srcFact('Filing', filing.filing_id, true),
      srcFact('Published commit', filing.published_commit, true), srcFact('Completion', filing.completion_state),
      srcFact('Error', typeof filing.error === 'string' ? filing.error : filing.error?.message || (filing.error ? JSON.stringify(filing.error) : ''))].join('')}</dl>
  </div>`;
}
// One compact line for notes, journals and lists.
function srcFilingLine(filing, {changed = false, submitted = ''} = {}) {
  const l = srcFilingLabel(filing);
  const snapshot = filing?.source_revision ? ` · submitted revision ${esc(filing.source_revision)}` : submitted ? ` · ${esc(submitted)}` : '';
  return `<span class="ntfilingstatus" data-src-line="${esc(l.key)}">${esc(l.label)}${snapshot}${filing?.error ? ` · ${esc(typeof filing.error === 'string' ? filing.error : filing.error.message || 'error')}` : ''}${changed ? ' · Changed since this submission (the submitted snapshot is unchanged)' : ''}${filing?.published_commit ? ` · commit <span class="src-sel">${esc(filing.published_commit)}</span>` : ''}</span>`;
}

// ------------------------------------------------------------- exact commands
const srcAttempts = new Map();   // action id -> {args, options, key}: the one request an uncertain outcome may repeat
const srcBusy = new Set();
const srcUncertain = error => !error.code || error.code === 'invalid_response' || error.httpStatus >= 500;
const srcConflict = error => /conflict|stale|revision|precondition|state_changed/i.test(String(error.code || ''));
const srcInvoke = async (scope, op, args, options) => {
  const result = await post(srcOpPath(scope, op), {...args, ...options});
  if (result?.status === 'signoff_required') { const e = new Error('This change is awaiting signoff.'); e.code = 'signoff_required'; throw e; }
  return coreData(result);
};
// inspect() -> {args, options} | {suppress: message}. It runs once per decision; an uncertain outcome repeats the stored request.
async function srcAct({scope, op, target, inspect, report, refresh, live, success}) {
  const id = `${srcScopeKey(scope)}|${op}|${target}`;
  if (srcBusy.has(id)) return;
  srcBusy.add(id);
  try {
    let attempt = srcAttempts.get(id);
    if (!attempt) {
      let basis;
      try { basis = await inspect(); } catch (error) { if (live()) report(`Could not inspect the current state: ${error.message}`, 'bad'); return; }
      if (!live()) return;
      if (basis.suppress) { report(basis.suppress, 'warn'); await refresh(); return; }
      attempt = {args: basis.args, options: basis.options, key: newRequestId()};
      srcAttempts.set(id, attempt);
    }
    if (live()) report('Sending…', 'info');
    try {
      await srcInvoke(scope, op, attempt.args, {...attempt.options, idempotency_key: attempt.key});
    } catch (error) {
      if (srcUncertain(error)) { if (live()) report(`The outcome is not confirmed (${error.message}). Pressing the button again repeats the same request.`, 'warn'); return; }
      srcAttempts.delete(id);
      if (live()) report(srcConflict(error) ? 'The source changed after you looked. It was reread; decide again.' : error.message, srcConflict(error) ? 'warn' : 'bad');
      if (srcConflict(error)) await refresh();
      return;
    }
    srcAttempts.delete(id);
    if (live()) { report(success, 'ok'); await refresh(); }
  } finally { srcBusy.delete(id); if (live()) await refresh(true); }
}
const srcRevisionInspect = (scope, docId, field = 'document_revision') => async () => {
  const doc = (await srcRead(scope, 'context_get', {document_id: docId, max_chars: 100})).document;
  const revision = doc?.filing?.[field] ?? doc?.revision;
  if (!Number.isSafeInteger(revision)) throw new Error('The current document revision was not returned.');
  return {args: {upload_id: docId}, options: {expected_revision: revision}};
};
// Exact-input controls: read the input first, send only what it is currently eligible for.
const srcInputInspect = (scope, docId, inputId, allowed, label) => async () => {
  const map = await srcRead(scope, 'context_upload_map', {upload_id: docId});
  const input = (map.versions || []).map(v => v.input).find(i => i?.id === inputId);
  if (!input) return {suppress: 'That input is no longer recorded for this source.'};
  if (!allowed.includes(input.state)) return {suppress: `The input is ${input.state.replace(/_/g, ' ')}; ${label} is not available.`};
  return {args: {input_id: inputId}, options: {expected_state: input.state}};
};

// ------------------------------------------------------------- recorded diagnostics
const srcDiagnosticProblem = error => error.code === 'forbidden' ? {kind: 'denied', text: 'Filing diagnostics are an owner view. They are not available to you; the source above is unaffected.'}
  : error.code === 'not_found' ? {kind: 'empty', text: 'No Context filing is recorded for this source. It is unfiled.'}
  : {kind: 'unavailable', text: `Filing diagnostics are unavailable: ${error.message}`};
const srcWhen = value => value ? `<span class="src-sel">${esc(value)}</span>` : '';
function srcVersionCard(version, ctx) {
  const s = version.source || {}, input = version.input || {}, job = version.job || {}, pub = version.publication, comp = version.completion;
  const current = ctx.currentRevision !== undefined && ctx.currentRevision !== null && s.revision === ctx.currentRevision;
  const actions = [];
  if (input.id && input.state === 'blocked') actions.push(`<button type="button" class="ghost sm" data-src-input-retry="${esc(input.id)}">Retry this input</button>`);
  if (input.id && ['queued', 'blocked'].includes(input.state)) actions.push(`<button type="button" class="ghost sm" data-src-input-discard="${esc(input.id)}">Discard this input</button>`);
  const attempts = (version.attempts || []).map(a => `<tr><td>${esc(a.attempt)}</td><td>${esc(a.cause || '')}</td><td>${srcWhen(a.started_at)}</td><td>${esc(a.outcome || 'recorded, no outcome yet')}</td><td>${srcWhen(a.finished_at)}</td><td class="src-sel">${esc(a.context_event_id || '')}</td><td>${a.error ? esc(typeof a.error === 'string' ? a.error : a.error.message || JSON.stringify(a.error)) : ''}</td></tr>`).join('');
  return `<article class="src-version" data-src-version="${esc(s.revision)}">
    <header><strong>Revision ${esc(s.revision)} · ${esc(s.operation || '')}</strong>${current ? '<span class="cnt">current source filing</span>' : '<span class="cnt">earlier accepted revision</span>'}</header>
    <dl class="src-facts">${[srcFact('Source', [s.kind, s.id].filter(Boolean).join(' '), true), srcFact('Digest', s.digest, true), srcFact('Filing', version.filing_id, true), srcFact('Request', version.request_id, true),
      srcFact('Job', [job.id, job.state].filter(Boolean).join(' · '), true), srcFact('Input', [input.id, input.state && `${input.state.replace(/_/g, ' ')}${input.attempt ? ` (attempt ${input.attempt})` : ''}`].filter(Boolean).join(' · '), true),
      srcFact('Queued', input.queued_at, true), srcFact('Input error', input.error ? (input.error.message || JSON.stringify(input.error)) : ''),
      srcFact('Context event', pub?.context_event_id, true), srcFact('Published commit', pub?.published_commit, true),
      srcFact('Completion', comp ? `${comp.state}${comp.outcome ? ` · ${comp.outcome}` : ''}` : ''), srcFact('Receipt', comp?.receipt_id, true)].join('')}</dl>
    ${pub ? '' : '<p class="src-note">Not published. Attempts below, if any, are unpublished.</p>'}
    ${attempts ? `<table class="src-attempts"><thead><tr><th>#</th><th>Cause</th><th>Started</th><th>Outcome</th><th>Finished</th><th>Event</th><th>Error</th></tr></thead><tbody>${attempts}</tbody></table>` : ''}
    ${actions.length ? `<div class="src-actions">${actions.join('')}</div>` : ''}
  </article>`;
}
async function srcPaintDiagnostics(host, scope, docId, ctx) {
  let map;
  try { map = await srcRead(scope, 'context_upload_map', {upload_id: docId}); }
  catch (error) {
    if (!ctx.live()) return;
    const p = srcDiagnosticProblem(error);
    host.innerHTML = `<div class="sh">Recorded filings</div><p class="src-note" data-src-diagnostic="${p.kind}" role="status">${esc(p.text)}</p>`;
    return;
  }
  if (!ctx.live()) return;
  const versions = Array.isArray(map.versions) ? map.versions : [];
  const bounded = versions.length >= SRC_DIAGNOSTIC_LIMIT;
  host.innerHTML = `<div class="sh">Recorded filings</div>
    <p class="src-note" data-src-bound>${bounded ? `Showing the ${SRC_DIAGNOSTIC_LIMIT} most recent recorded revisions. Earlier history may exist and cannot be paged; this is not the complete history.`
      : `${versions.length} recorded revision${versions.length === 1 ? '' : 's'} returned (limit ${SRC_DIAGNOSTIC_LIMIT}). This is a record of filings, not an outline or a publication feed.`}</p>
    ${versions.length ? `<div class="src-versions">${versions.map(v => srcVersionCard(v, ctx)).join('')}</div>` : '<p class="src-note" data-src-diagnostic="empty">No filing is recorded for this source.</p>'}`;
  const act = (selector, key, op, allowed, label, success) => host.querySelectorAll(selector).forEach(button => button.onclick = () => {
    const inputId = button.dataset[key];
    srcAct({scope, op, target: inputId, live: ctx.live, report: ctx.report, refresh: ctx.refresh, success,
      inspect: srcInputInspect(scope, docId, inputId, allowed, label)});
  });
  act('[data-src-input-retry]', 'srcInputRetry', 'context_queue_retry', ['blocked'], 'retry', 'Input retry accepted. It is queued again; nothing is published yet.');
  act('[data-src-input-discard]', 'srcInputDiscard', 'context_queue_discard', ['queued', 'blocked'], 'discard', 'Discard requested. This does not withdraw anything already published.');
}

// ------------------------------------------------------------- source reader
// Mounts one source document: its current filing, a bounded inert body, and the recorded diagnostics.
// `opts`: {live(), crumbs: html, wireCrumbs(host), openObject(ref), editable: false}
function srcMountSource(host, scope, docId, opts = {}) {
  const generation = ++srcMountGeneration;
  const alive = () => generation === srcMountGeneration && host.isConnected && (opts.live ? opts.live() : true);
  if (!SRC_ID_RE.test(docId || '')) { host.innerHTML = `${opts.crumbs || ''}<div class="empty pad" role="alert">That is not a source document identity.</div>`; opts.wireCrumbs?.(host); return; }
  host.innerHTML = `${opts.crumbs || ''}<div class="src-view" data-src-view="${esc(docId)}"><h2 class="src-title">Loading source…</h2><div class="chips" data-src-meta></div>
    <section class="src-section" data-src-section="filing"></section>
    <section class="src-section" data-src-section="body"></section>
    <section class="src-section" data-src-section="diagnostics"></section></div>`;
  opts.wireCrumbs?.(host);
  const q = name => host.querySelector(`[data-src-section="${name}"]`);
  const title = host.querySelector('.src-title');
  const state = {doc: null, parts: [], loaded: new Set(), expected: 0, hash: null, cursor: null, loading: false, bodyError: '', refreshes: 0, timer: null, message: null, version: 0};
  const report = (text, tone = 'info') => { state.message = {text, tone}; const el = q('filing')?.querySelector('[data-src-message]'); if (el) { el.textContent = text; el.dataset.tone = tone; } };

  // Only what the document itself returns: kind, origin, type, tags and (when present) the recorded encounter.
  const paintMeta = doc => {
    const chip = (label, value) => value ? `<span class="chip"><b>${esc(label)}</b>${esc(value)}</span>` : '';
    const names = Array.isArray(doc.participants) ? doc.participants.map(p => p?.name).filter(Boolean).join(', ') : '';
    host.querySelector('[data-src-meta]').innerHTML = [chip('kind', String(doc.kind || '').replace(/^context_/, '')), chip('origin', doc.origin), chip('type', doc.material_type),
      chip('with', names), chip('on', doc.occurred_on), ...(Array.isArray(doc.tags) ? doc.tags.map(t => chip('tag', t)) : [])].join('');
  };
  const paintFiling = (busy = false) => {
    const doc = state.doc; if (!doc) return;
    const filing = doc.filing;
    busy = busy || [...srcBusy].some(id => id.endsWith(`|${docId}`));
    const processedNow = filing && filing.status === 'processed' && filing.operation !== 'remove' && filing.completion_state === 'applied';
    const retryable = filing?.retryable === true;
    q('filing').innerHTML = `<div class="sh">Current source</div>${srcFilingFacts(filing, {currentRevision: doc.revision})}
      <div class="src-actions" data-src-actions>
        ${retryable ? `<button type="button" class="ghost sm" data-src-retry ${busy ? 'disabled' : ''}>Retry filing</button>` : ''}
        ${processedNow ? `<button type="button" class="ghost sm" data-src-retract ${busy ? 'disabled' : ''}>Withdraw from Context…</button>` : ''}
        <button type="button" class="ghost sm" data-src-refresh>Refresh status</button>
        ${doc.canonical_ref && opts.openObject ? `<button type="button" class="ghost sm" data-src-object="${esc(doc.canonical_ref)}">Open graph object</button>` : ''}
      </div>
      <p class="src-note" data-src-message role="status" data-tone="${esc(state.message?.tone || '')}">${esc(state.message?.text || '')}</p>
      <p class="src-note" data-src-refresh-note></p>`;
    const send = (op, success) => srcAct({scope, op, target: docId, live: alive, report, refresh: refreshStatus, success,
      inspect: srcRevisionInspect(scope, docId)});
    const retry = q('filing').querySelector('[data-src-retry]');
    if (retry) retry.onclick = () => send('context_retry', 'Retry accepted and queued for filing. Nothing is published yet.');
    const retract = q('filing').querySelector('[data-src-retract]');
    if (retract) retract.onclick = () => {
      retract.hidden = true;
      const confirm = document.createElement('span');
      confirm.className = 'src-confirm';
      confirm.innerHTML = 'Withdraw this source from Context? <button type="button" class="ghost sm" data-src-retract-confirm>Confirm withdrawal</button> <button type="button" class="ghost sm" data-src-retract-cancel>Cancel</button>';
      retract.after(confirm);
      confirm.querySelector('[data-src-retract-cancel]').onclick = () => { confirm.remove(); retract.hidden = false; };
      confirm.querySelector('[data-src-retract-confirm]').onclick = () => { confirm.remove(); retract.hidden = false; send('context_retract', 'Withdrawal accepted and queued. It is not complete until the queue publishes it.'); };
    };
    q('filing').querySelector('[data-src-refresh]').onclick = () => refreshStatus();
    const obj = q('filing').querySelector('[data-src-object]');
    if (obj) obj.onclick = () => opts.openObject(obj.dataset.srcObject);
  };
  const paintBody = () => {
    const el = q('body'); if (!el) return;
    const text = state.parts.join('');
    el.innerHTML = `<div class="sh">Body <span class="stamp" data-src-count>${[...text].length} characters shown${state.cursor ? ' · more available' : ' · complete'}</span></div>
      <pre class="src-body" data-src-body></pre>
      ${state.bodyError ? `<p class="bad" role="alert" data-src-body-error>${esc(state.bodyError)}</p>` : ''}
      ${state.cursor ? `<button type="button" class="ghost" data-src-more ${state.loading ? 'disabled' : ''}>${state.bodyError ? 'Retry' : 'Load more'}</button>` : ''}`;
    el.querySelector('[data-src-body]').textContent = text;
    const more = el.querySelector('[data-src-more]');
    if (more) more.onclick = () => loadBody();
  };
  // One page at a time; a page is refused unless it continues exactly where the last one ended, on the same content.
  const loadBody = async first => {
    if (state.loading) return;
    state.loading = true; state.bodyError = ''; if (!first) paintBody();
    const requested = state.cursor, offset = state.expected, version = state.version;
    try {
      const data = await srcRead(scope, 'context_get', {document_id: docId, max_chars: SRC_PAGE_CHARS, ...(requested ? {cursor: requested} : {})});
      if (!alive() || version !== state.version) return;
      const doc = data.document || {};
      if (doc.id !== docId) throw new Error('The page belongs to a different document and was refused.');
      if (requested) {
        if (doc.content_hash !== state.hash) throw new Error('The source changed while reading. Reload it to read the new content.');
        if (state.loaded.has(doc.character_offset) || doc.character_offset !== offset) throw new Error('The page does not continue the text already shown and was refused.');
      } else { state.hash = doc.content_hash; state.doc = {...doc}; }
      const part = String(doc.body ?? '');
      state.loaded.add(doc.character_offset ?? 0);
      state.parts.push(part); state.expected = (doc.character_offset ?? 0) + [...part].length;
      state.cursor = data.next_cursor || null;
      if (!requested) { title.textContent = doc.title || 'Source'; paintMeta(doc); }
    } catch (error) { if (alive() && version === state.version) state.bodyError = error.message; }
    finally { if (alive() && version === state.version) { state.loading = false; paintBody(); if (!requested) paintFiling(); } else state.loading = false; }
  };
  // Status-only refresh: never touches the body pages or their cursor.
  async function refreshStatus(quiet) {
    if (!alive() || !state.doc) return;
    if (quiet === true) { paintFiling(); return; }
    try {
      const doc = (await srcRead(scope, 'context_get', {document_id: docId, max_chars: 100})).document;
      if (!alive()) return;
      if (doc?.id === docId) { state.doc = {...state.doc, filing: doc.filing, revision: doc.revision, canonical_ref: doc.canonical_ref ?? state.doc.canonical_ref}; }
      paintFiling();
    } catch (error) { if (alive()) report(`Could not refresh the status: ${error.message}`, 'bad'); paintFiling(); }
    await srcPaintDiagnostics(q('diagnostics'), scope, docId, diagCtx());
    scheduleRefresh();
  }
  const diagCtx = () => ({live: alive, report, refresh: refreshStatus, currentRevision: state.doc?.filing?.source_current === false ? null : state.doc?.filing?.source_revision});
  const scheduleRefresh = () => {
    clearTimeout(state.timer);
    if (!alive() || !srcActive(state.doc?.filing)) return;
    const note = q('filing')?.querySelector('[data-src-refresh-note]');
    if (state.refreshes >= SRC_REFRESH_MAX) { if (note) note.textContent = 'Automatic refresh stopped. Use Refresh status to check again.'; return; }
    if (note) note.textContent = 'Checking again automatically while this is in progress.';
    state.timer = setTimeout(() => { state.refreshes++; refreshStatus(); }, SRC_REFRESH_MS);
  };
  (async () => {
    await loadBody(true);
    if (!alive()) return;
    if (!state.doc) { title.textContent = 'Source unavailable'; q('filing').innerHTML = ''; return; }
    await srcPaintDiagnostics(q('diagnostics'), scope, docId, diagCtx());
    scheduleRefresh();
  })();
  return {destroy() { if (generation === srcMountGeneration) srcMountGeneration++; clearTimeout(state.timer); }};
}

// ------------------------------------------------------------- source lists
// kinds: [{kind, label}]; every kind pages independently with its own cursor and displayed count.
function srcMountList(host, scope, kinds, opts = {}) {
  const generation = ++srcMountGeneration;
  const alive = () => generation === srcMountGeneration && host.isConnected && (opts.live ? opts.live() : true);
  const sections = kinds.map(k => ({...k, items: [], cursor: null, total: null, loading: false, error: '', loaded: false, request: 0}));
  host.innerHTML = sections.map(s => `<div class="sec" data-src-list="${esc(s.kind)}"><div class="sh">${esc(s.label)} <span class="stamp" data-src-total></span></div><div class="cards" data-src-items></div><p class="src-note" data-src-status role="status"></p><button type="button" class="ghost" data-src-more hidden>Load more</button></div>`).join('');
  const paint = s => {
    const el = host.querySelector(`[data-src-list="${s.kind}"]`); if (!el) return;
    el.querySelector('[data-src-total]').textContent = s.loaded ? `${s.items.length}${s.total != null ? ` of ${s.total}` : ''} shown` : '';
    el.querySelector('[data-src-items]').innerHTML = s.items.map(item => `<div class="card u d" tabindex="0" role="link" data-src-open="${esc(item.id)}"><div class="ct"><span class="tt">${esc(item.title || item.id)}</span>${item.filing || /^context_(upload|dump)$/.test(item.kind) ? srcFilingChip(item.filing) : ''}</div><div class="cb">${esc(item.summary || '')}</div><div class="cs">${esc(item.id)}${item.filing?.source_revision ? ` · revision ${esc(item.filing.source_revision)}` : ''}</div></div>`).join('');
    el.querySelectorAll('[data-src-open]').forEach(card => { const go = () => opts.open?.(card.dataset.srcOpen); card.onclick = go; card.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } }; });
    const status = el.querySelector('[data-src-status]');
    status.textContent = s.error ? s.error : s.loading ? 'Loading…' : s.loaded && !s.items.length ? 'None.' : '';
    status.classList.toggle('bad', !!s.error);
    const more = el.querySelector('[data-src-more]');
    more.hidden = !(s.cursor || s.error); more.disabled = s.loading; more.textContent = s.error ? 'Retry' : 'Load more';
    more.onclick = () => load(s);
  };
  const load = async s => {
    if (s.loading) return;
    const request = ++s.request; s.loading = true; s.error = ''; paint(s);
    try {
      const data = await srcRead(scope, 'context_list', {kind: s.kind, limit: SRC_LIST_LIMIT, ...(s.cursor ? {cursor: s.cursor} : {})});
      if (!alive() || request !== s.request) return;
      const known = new Set(s.items.map(i => i.id));
      s.items.push(...(data.items || []).filter(i => !known.has(i.id)));
      s.cursor = data.next_cursor || null; s.total = Number.isSafeInteger(data.total) ? data.total : null; s.loaded = true;
    } catch (error) { if (alive() && request === s.request) s.error = error.message; }
    finally { if (alive() && request === s.request) { s.loading = false; paint(s); } }
  };
  sections.forEach(s => { paint(s); load(s); });
  return {destroy() { if (generation === srcMountGeneration) srcMountGeneration++; }};
}

// ------------------------------------------------------------- recent filings (owner diagnostic)
async function srcMountRecentFilings(host, scope, opts = {}) {
  const generation = srcMountGeneration;
  const alive = () => generation === srcMountGeneration && host.isConnected && (opts.live ? opts.live() : true);
  host.innerHTML = '<div class="sh">Recent filing records</div><p class="src-note" role="status">Loading…</p>';
  let data;
  try { data = await srcRead(scope, 'context_filings', {limit: SRC_FILINGS_LIMIT}); }
  catch (error) { if (!alive()) return; const p = srcDiagnosticProblem(error); host.innerHTML = `<div class="sh">Recent filing records</div><p class="src-note" data-src-diagnostic="${p.kind === 'empty' ? 'empty' : p.kind}" role="status">${esc(p.kind === 'empty' ? 'No filings are recorded.' : p.text)}</p>`; return; }
  if (!alive()) return;
  const filings = Array.isArray(data.filings) ? data.filings : [];
  const bounded = filings.length >= SRC_FILINGS_LIMIT;
  host.innerHTML = `<div class="sh">Recent filing records</div><p class="src-note" data-src-bound>${bounded ? `The ${SRC_FILINGS_LIMIT} most recent filings are shown. Older filings exist or may exist and cannot be paged here.` : `${filings.length} recorded filing${filings.length === 1 ? '' : 's'} returned (limit ${SRC_FILINGS_LIMIT}).`}</p>
    <div class="cards">${filings.map(f => { const s = f.source || {}, i = f.input || {}, pub = f.publication, comp = f.completion;
      const asFiling = {state: pub ? 'published' : i.state, completion_state: comp?.state, operation: s.operation};
      return `<div class="card u" ${s.kind === 'document' && SRC_ID_RE.test(s.id || '') ? `tabindex="0" role="link" data-src-open="${esc(s.id)}"` : ''}><div class="ct"><span class="tt">${esc(s.kind)} ${esc(s.id)}</span>${srcFilingChip(asFiling)}</div>
      <div class="cb">revision ${esc(s.revision)} · ${esc(s.operation || '')} · input ${esc(i.id || '')} ${esc((i.state || '').replace(/_/g, ' '))}${pub ? ` · commit <span class="src-sel">${esc(pub.published_commit)}</span>` : ' · not published'}</div><div class="cs">filing <span class="src-sel">${esc(f.filing_id)}</span> · job ${esc(f.job?.state || '')}</div></div>`; }).join('')}</div>`;
  host.querySelectorAll('[data-src-open]').forEach(card => { const go = () => opts.open?.(card.dataset.srcOpen); card.onclick = go; card.onkeydown = e => { if (e.key === 'Enter') go(); }; });
}

// ------------------------------------------------------------- Context queue summary
// context_queue = {queued, processing, blocked, discard_requested, publication_pending_completion, completion_conflict, queue_blocked, blocked_input_id}
const SRC_QUEUE_FIELDS = ['queued', 'processing', 'blocked', 'discard_requested', 'publication_pending_completion', 'completion_conflict'];
const srcQueueValid = q => !!q && typeof q === 'object' && SRC_QUEUE_FIELDS.every(k => Number.isSafeInteger(q[k]) && q[k] >= 0);
function srcQueueBits(queue) {
  if (!srcQueueValid(queue)) return [{tone: 'bad', text: 'Context queue status is unavailable'}];
  const bits = [];
  if (queue.processing) bits.push({tone: 'run', text: `${queue.processing} processing`});
  if (queue.queued) bits.push({tone: '', text: `${queue.queued} queued`});
  if (queue.blocked) bits.push({tone: 'bad', text: `${queue.blocked} blocked`});
  if (queue.discard_requested) bits.push({tone: 'warn', text: `${queue.discard_requested} discard requested`});
  if (queue.publication_pending_completion) bits.push({tone: 'run', text: `${queue.publication_pending_completion} published, completion pending`});
  if (queue.completion_conflict) bits.push({tone: 'bad', text: `${queue.completion_conflict} published, completion conflict`});
  if (queue.queue_blocked) bits.push({tone: 'bad', text: `queue is held by input ${queue.blocked_input_id || 'unknown'}; later inputs wait`});
  return bits;
}
const srcQueueHtml = queue => srcQueueBits(queue).map(b => `<span class="${esc(b.tone === 'bad' ? 'bad' : b.tone === 'run' ? 'run' : '')}" data-src-queue>${esc(b.text)}</span>`).join('<span class="dot">·</span>');
// Bounded view-only read of the Context process view (business: /biz/<slug>/context, Global: /root/context).
const srcProcessesPath = scope => `${scope.kind === 'root' ? '/root' : `/biz/${encodeURIComponent(scope.id)}`}/context?view=processes`;
const srcReadProcesses = (scope, signal = pageReads.signal) => pageRead(srcProcessesPath(scope), {signal});
