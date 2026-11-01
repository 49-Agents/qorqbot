// ---------------------------------------------------------------- context sources
// Source documents (uploads, dumps, filed notes and journals) and what the Context queue did with them.
// Everything here is read from the recorded `filing` projection and the `context_queue` process summary. Nothing is
// inferred from file paths, names or generated maps, and a submission is never reported as publication.
// Revisions, cursors and request keys decide what is sent but are never shown.
//
// A scope is {kind: 'business', id: slug} or {kind: 'root'}; every call below is made in exactly that namespace.
let srcMountGeneration = 0;
const SRC_PAGE_CHARS = 6000, SRC_LIST_LIMIT = 30;
const SRC_REFRESH_MS = 20000, SRC_REFRESH_MAX = 30;
const SRC_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const srcScopeKey = scope => scope.kind === 'root' ? 'root' : `biz:${scope.id}`;
const srcOpPath = (scope, op) => scope.kind === 'root' ? `/root/operations/${op}` : `/biz/${encodeURIComponent(scope.id)}/operations/${op}`;
// Only the three raw-document reads select the bounded library, by naming their one owning namespace; no other operation takes `scope`.
const SRC_RAW_READS = new Set(['context_get', 'context_list', 'context_search']);
const srcRead = (scope, op, args, signal = pageReads.signal) =>
  pageReadPost(srcOpPath(scope, op), SRC_RAW_READS.has(op) ? {scope: scope.kind === 'root' ? 'shared' : 'business', ...args} : args, signal).then(coreData);
const srcSourceRoute = (scope, id) => scope.kind === 'root' ? `#/global/context/library/${encodeURIComponent(id)}` : `_source/${id}`;
const SRC_COPY = {loadFailed: "Couldn't load. Retry", saveFailed: "Couldn't save. Try again", changed: 'Changed since you opened it. Refresh', unconfirmed: "Couldn't confirm. Try again"};

// ------------------------------------------------------------- filing presentation
// state + completion_state + operation -> one plain label. `status` is only the derived summary and never decides the label.
// A pending, missing or conflicting completion is never shown as complete.
function srcFilingLabel(filing) {
  if (!filing || typeof filing !== 'object') return {key: 'unavailable', label: 'Unavailable', tone: 'warn'};
  const state = filing.state;
  if (typeof state !== 'string' || !state) return {key: 'unavailable', label: 'Unavailable', tone: 'warn'};
  const table = {unfiled: ['Not added', 'idle'], queued: ['Waiting', 'run'], processing: ['Processing', 'run'], blocked: ["Couldn't process", 'bad'],
    discard_requested: ['Cancelling', 'warn'], discarded: ['Cancelled', 'bad']};
  if (table[state]) return {key: state, label: table[state][0], tone: table[state][1]};
  if (state === 'published') {
    if (filing.completion_state === 'conflict') return {key: 'completion_conflict', label: 'Needs attention', tone: 'bad'};
    if (filing.completion_state === 'applied') return filing.operation === 'remove'
      ? {key: 'withdrawn', label: 'Removed', tone: 'ok'} : {key: 'filed', label: 'Added to Context', tone: 'ok'};
    if (filing.completion_state === 'pending' || filing.completion_state == null) return {key: 'completion_pending', label: 'Finishing', tone: 'run'};
  }
  return {key: 'unavailable', label: 'Unavailable', tone: 'warn'};
}
// States that can still change without anyone acting: worth a bounded view-only refresh.
const srcActive = filing => ['queued', 'processing', 'discard_requested'].includes(filing?.state) ||
  (filing?.state === 'published' && (filing.completion_state === 'pending' || filing.completion_state == null));
function srcFilingChip(filing, extra = '') {
  const l = srcFilingLabel(filing);
  return `<span class="cnt src-state is-${esc(l.tone)}" data-src-state="${esc(l.key)}">${esc(l.label)}${extra}</span>`;
}
// One compact line for notes, journals and lists. `changed` says the live text differs from what was submitted.
function srcFilingLine(filing, {changed = false} = {}) {
  const l = srcFilingLabel(filing);
  return `<span class="ntfilingstatus" data-src-line="${esc(l.key)}">${esc(l.label)}${changed ? ' · Changes not submitted' : ''}</span>`;
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
async function srcAct({scope, op, target, inspect, report, refresh, live, success, validate}) {
  const id = `${srcScopeKey(scope)}|${op}|${target}`;
  if (srcBusy.has(id)) return;
  srcBusy.add(id);
  try {
    let attempt = srcAttempts.get(id);
    if (!attempt) {
      let basis;
      try { basis = await inspect(); } catch (error) { if (live()) report(SRC_COPY.unconfirmed, 'bad'); return; }
      if (!live()) return;
      if (basis.suppress) { report(basis.suppress, 'warn'); await refresh(); return; }
      attempt = {args: basis.args, options: basis.options, key: newRequestId()};
      srcAttempts.set(id, attempt);
    }
    if (live()) report('Sending…', 'info');
    let result;
    try {
      result = await srcInvoke(scope, op, attempt.args, {...attempt.options, idempotency_key: attempt.key});
    } catch (error) {
      if (srcUncertain(error)) { if (live()) report(SRC_COPY.unconfirmed, 'warn'); return; }
      srcAttempts.delete(id);
      if (live()) report(srcConflict(error) ? SRC_COPY.changed : error.code === 'signoff_required' ? error.message : SRC_COPY.saveFailed, srcConflict(error) ? 'warn' : 'bad');
      if (srcConflict(error)) await refresh();
      return;
    }
    if (validate && !validate(result)) {   // sent, but the answer is not a recognizable result: unconfirmed, the same request may be repeated
      if (live()) report(SRC_COPY.unconfirmed, 'warn');
      return;
    }
    srcAttempts.delete(id);
    if (live()) { report(typeof success === 'function' ? success(result) : success, 'ok'); await refresh(); }
  } finally { srcBusy.delete(id); if (live()) await refresh(true); }
}
// The decision is bound to the revision the owner was shown (`shown`). A preflight that sees another revision sends nothing:
// the display is refreshed and the owner must decide again.
const srcRevisionInspect = (scope, docId, shown) => async () => {
  if (!Number.isSafeInteger(shown)) return {suppress: SRC_COPY.changed};
  const doc = (await srcRead(scope, 'context_get', {document_id: docId, max_chars: 100})).document;
  const current = doc?.filing?.document_revision;
  if (!Number.isSafeInteger(current)) throw new Error('The current document revision was not returned.');
  if (current !== shown) return {suppress: SRC_COPY.changed};
  return {args: {upload_id: docId}, options: {expected_revision: shown}};
};

// ------------------------------------------------------------- source reader
// Mounts one source document: its name, one plain filing status, the allowed source-level actions and a bounded inert body.
// `opts`: {live(), crumbs: html, wireCrumbs(host), openObject(ref), editable: false}
function srcMountSource(host, scope, docId, opts = {}) {
  const generation = ++srcMountGeneration;
  const alive = () => generation === srcMountGeneration && host.isConnected && (opts.live ? opts.live() : true);
  if (!SRC_ID_RE.test(docId || '')) { host.innerHTML = `${opts.crumbs || ''}<div class="empty pad" role="alert">Unavailable</div>`; opts.wireCrumbs?.(host); return; }
  host.innerHTML = `${opts.crumbs || ''}<div class="src-view" data-src-view="${esc(docId)}"><h2 class="src-title">Loading…</h2><div class="chips" data-src-meta></div>
    <section class="src-section" data-src-section="filing"></section>
    <section class="src-section" data-src-section="body"></section></div>`;
  opts.wireCrumbs?.(host);
  const q = name => host.querySelector(`[data-src-section="${name}"]`);
  const title = host.querySelector('.src-title');
  const state = {doc: null, parts: [], loaded: new Set(), expected: 0, restart: false, cursor: null, loading: false, bodyError: '', refreshes: 0, timer: null, message: null, version: 0};
  const report = (text, tone = 'info') => { state.message = {text, tone}; const el = q('filing')?.querySelector('[data-src-message]'); if (el) { el.textContent = text; el.dataset.tone = tone; } };
  const setName = name => { title.textContent = name; const cur = host.querySelector('.crumbs .cur'); if (cur) cur.textContent = name; };

  // Only meaningful names: who it was with, when, and its tags.
  const paintMeta = doc => {
    const chip = (label, value) => value ? `<span class="chip">${label ? `<b>${esc(label)}</b>` : ''}${esc(value)}</span>` : '';
    const names = Array.isArray(doc.participants) ? doc.participants.map(p => p?.name).filter(Boolean).join(', ') : '';
    host.querySelector('[data-src-meta]').innerHTML = [chip('with', names), chip('on', doc.occurred_on), ...(Array.isArray(doc.tags) ? doc.tags.map(t => chip('', t)) : [])].join('');
  };
  const paintFiling = (busy = false) => {
    const doc = state.doc; if (!doc) return;
    const filing = doc.filing;
    busy = busy || [...srcBusy].some(id => id.endsWith(`|${docId}`));
    const processedNow = filing && filing.status === 'processed' && filing.operation !== 'remove' && filing.completion_state === 'applied';
    const retryable = filing?.retryable === true;
    const addLabel = !filing || filing.state === 'unfiled' ? 'Add to Context' : 'Retry';
    q('filing').innerHTML = `<div class="src-filing" data-src-filing>${srcFilingChip(filing)}</div>
      <div class="src-actions" data-src-actions>
        ${retryable ? `<button type="button" class="ghost sm" data-src-retry ${busy ? 'disabled' : ''}>${addLabel}</button>` : ''}
        ${processedNow ? `<button type="button" class="ghost sm" data-src-retract ${busy ? 'disabled' : ''}>Remove from Context</button>` : ''}
        <button type="button" class="ghost sm" data-src-refresh>Refresh</button>
        ${doc.canonical_ref && opts.openObject ? `<button type="button" class="ghost sm" data-src-object="${esc(doc.canonical_ref)}">Open in Context</button>` : ''}
      </div>
      <p class="src-note" data-src-message role="status" data-tone="${esc(state.message?.tone || '')}">${esc(state.message?.text || '')}</p>`;
    const send = (op, success, shown) => srcAct({scope, op, target: docId, live: alive, report, refresh: refreshStatus, success,
      inspect: srcRevisionInspect(scope, docId, shown)});
    const retry = q('filing').querySelector('[data-src-retry]');
    if (retry) retry.onclick = () => send('context_retry', 'Requested.', filing.document_revision);
    const retract = q('filing').querySelector('[data-src-retract]');
    if (retract) retract.onclick = () => {
      const shown = filing.document_revision;   // the decision is bound to what was displayed when it was made
      retract.hidden = true;
      const confirm = document.createElement('span');
      confirm.className = 'src-confirm';
      confirm.innerHTML = 'Remove this from Context? <button type="button" class="ghost sm" data-src-retract-confirm>Remove</button> <button type="button" class="ghost sm" data-src-retract-cancel>Cancel</button>';
      retract.after(confirm);
      confirm.querySelector('[data-src-retract-cancel]').onclick = () => { confirm.remove(); retract.hidden = false; };
      confirm.querySelector('[data-src-retract-confirm]').onclick = () => { confirm.remove(); retract.hidden = false; send('context_retract', 'Removal requested.', shown); };
    };
    q('filing').querySelector('[data-src-refresh]').onclick = () => refreshStatus();
    const obj = q('filing').querySelector('[data-src-object]');
    if (obj) obj.onclick = () => opts.openObject(obj.dataset.srcObject);
  };
  const paintBody = () => {
    const el = q('body'); if (!el) return;
    if (!state.doc) {   // nothing was read: never label it complete
      el.innerHTML = state.bodyError ? `<p class="bad" role="alert" data-src-body-error>${esc(state.bodyError)}</p><button type="button" class="ghost" data-src-reload ${state.loading ? 'disabled' : ''}>Retry</button>` : '';
      const reload = el.querySelector('[data-src-reload]');
      if (reload) reload.onclick = () => loadBody(true).then(() => { if (alive() && state.doc) scheduleRefresh(); });
      return;
    }
    const text = state.parts.join('');
    el.innerHTML = `<pre class="src-body" data-src-body></pre>
      ${state.bodyError ? `<p class="bad" role="alert" data-src-body-error>${esc(state.bodyError)}</p>` : ''}
      ${state.cursor ? `<button type="button" class="ghost" ${state.restart ? 'data-src-restart' : 'data-src-more'} ${state.loading ? 'disabled' : ''}>${state.restart ? 'Refresh' : state.bodyError ? 'Retry' : 'Load more'}</button>` : ''}`;
    el.querySelector('[data-src-body]').textContent = text;
    const more = el.querySelector('[data-src-more]');
    if (more) more.onclick = () => loadBody();
    const restart = el.querySelector('[data-src-restart]');
    // The server refused the cursor (the body changed): never append; read again from the start.
    if (restart) restart.onclick = () => { state.version++; state.parts = []; state.loaded = new Set(); state.expected = 0; state.cursor = null; state.bodyError = ''; state.restart = false; state.loading = false; loadBody(true); };
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
      const cursor = data.next_cursor;
      if (typeof doc.body !== 'string' || !Number.isSafeInteger(doc.character_offset) || doc.character_offset < 0 || typeof data.complete !== 'boolean'
        || !(cursor === null || (typeof cursor === 'string' && cursor !== '')) || data.complete !== (cursor === null) || (cursor !== null && doc.body === '')) throw new Error('The page is malformed and was refused.');
      // The server validates the opaque cursor against the document, scope and body; the file hash is not a second authority
      // (completion rewrites it without changing the body). Only the exact expected offset is accepted.
      if (state.loaded.has(doc.character_offset) || doc.character_offset !== offset) throw new Error('The page does not continue the text already shown and was refused.');
      if (!requested) state.doc = {...doc};
      const part = doc.body;
      state.loaded.add(doc.character_offset ?? 0);
      state.parts.push(part); state.expected = (doc.character_offset ?? 0) + [...part].length;
      state.cursor = cursor;
      if (!requested) { setName(doc.title || 'Untitled source'); paintMeta(doc); }
    } catch (error) {
      if (alive() && version === state.version) {
        state.restart = !!error.code && !srcUncertain(error);
        state.bodyError = state.restart ? SRC_COPY.changed : SRC_COPY.loadFailed;
      }
    }
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
    } catch (error) { if (alive()) report(SRC_COPY.loadFailed, 'bad'); paintFiling(); }
    scheduleRefresh();
  }
  const scheduleRefresh = () => {
    clearTimeout(state.timer);
    if (!alive() || !srcActive(state.doc?.filing) || state.refreshes >= SRC_REFRESH_MAX) return;
    state.timer = setTimeout(() => { state.refreshes++; refreshStatus(); }, SRC_REFRESH_MS);
  };
  // Queue-wide changes (including another business's blocked head) refresh status only; torn down with the mount.
  const unsubscribe = typeof onChange === 'function' ? onChange(['context_queue', 'documents'], scope.kind === 'root' ? null : scope.id, () => { if (alive()) refreshStatus(); else unsubscribe?.(); }) : null;
  (async () => {
    await loadBody(true);
    if (!alive()) return;
    if (!state.doc) { setName('Unavailable'); q('filing').innerHTML = ''; return; }
    scheduleRefresh();
  })();
  return {destroy() { if (generation === srcMountGeneration) srcMountGeneration++; clearTimeout(state.timer); unsubscribe?.(); }};
}

// ------------------------------------------------------------- source lists
// kinds: [{kind, label}]; every kind pages independently with its own cursor.
function srcMountList(host, scope, kinds, opts = {}) {
  const generation = ++srcMountGeneration;
  const alive = () => generation === srcMountGeneration && host.isConnected && (opts.live ? opts.live() : true);
  const sections = kinds.map(k => ({...k, items: [], cursor: null, loading: false, error: '', loaded: false, request: 0}));
  host.innerHTML = sections.map(s => `<div class="sec" data-src-list="${esc(s.kind)}"><div class="sh">${esc(s.label)}</div><div class="cards" data-src-items></div><p class="src-note" data-src-status role="status"></p><button type="button" class="ghost" data-src-more hidden>Load more</button></div>`).join('');
  const paint = s => {
    const el = host.querySelector(`[data-src-list="${s.kind}"]`); if (!el) return;
    el.querySelector('[data-src-items]').innerHTML = s.items.map(item => `<div class="card u d" tabindex="0" role="link" data-src-open="${esc(item.id)}"><div class="ct"><span class="tt">${esc(item.title || 'Untitled source')}</span>${item.filing || /^context_(upload|dump)$/.test(item.kind) ? srcFilingChip(item.filing) : ''}</div><div class="cb">${esc(item.summary || '')}</div></div>`).join('');
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
      s.cursor = data.next_cursor || null; s.loaded = true;
    } catch (error) { if (alive() && request === s.request) s.error = SRC_COPY.loadFailed; }
    finally { if (alive() && request === s.request) { s.loading = false; paint(s); } }
  };
  sections.forEach(s => { paint(s); load(s); });
  return {destroy() { if (generation === srcMountGeneration) srcMountGeneration++; }};
}

// ------------------------------------------------------------- Context queue summary
// context_queue = {queued, processing, blocked, discard_requested, publication_pending_completion, completion_conflict, queue_blocked, blocked_input_id}
// The shape is validated; only "Updating Context" or "Context needs attention" is ever shown, never counters or identifiers.
const SRC_QUEUE_FIELDS = ['queued', 'processing', 'blocked', 'discard_requested', 'publication_pending_completion', 'completion_conflict'];
const srcQueueValid = q => !!q && typeof q === 'object' && SRC_QUEUE_FIELDS.every(k => Number.isSafeInteger(q[k]) && q[k] >= 0);
function srcQueueBits(queue) {
  if (!srcQueueValid(queue) || queue.blocked || queue.completion_conflict || queue.queue_blocked) return [{tone: 'bad', text: 'Context needs attention'}];
  if (queue.queued || queue.processing || queue.discard_requested || queue.publication_pending_completion) return [{tone: 'run', text: 'Updating Context'}];
  return [];
}
const srcQueueHtml = queue => srcQueueBits(queue).map(b => `<span class="${esc(b.tone === 'bad' ? 'bad' : b.tone === 'run' ? 'run' : '')}" data-src-queue>${esc(b.text)}</span>`).join('');
// Bounded view-only read of the Context process view (business: /biz/<slug>/context, Global: /root/context).
const srcProcessesPath = scope => `${scope.kind === 'root' ? '/root' : `/biz/${encodeURIComponent(scope.id)}`}/context?view=processes`;
const srcReadProcesses = (scope, signal = pageReads.signal) => pageRead(srcProcessesPath(scope), {signal});
