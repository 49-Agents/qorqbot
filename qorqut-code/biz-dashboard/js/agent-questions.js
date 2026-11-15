// Every open question on Global, the Projects and the Tasks the owner can read, blocking ones first. Read-only: Ordubashy
// raises and closes them; each row opens the thread of the item it belongs to. Uses timelineEsc and timelineDay.
T_VIEW_LABEL.questions = 'Open questions';
T_VIEW_ICON.questions = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.6 2.2c-.8.4-1.2.9-1.2 1.8M12 16.6v.1"/></svg>';

const AGENT_QUESTION_KINDS = {task: 'Task', project: 'Project', global: 'Global'};
function agentQuestionsHtml(items) {
  if (!items.length) return '';
  return items.map((row, index) => {
    const kind = AGENT_QUESTION_KINDS[row.source?.type] || '';
    const place = row.source?.type === 'global' ? 'Global' : `${kind} · ${row.title || 'Untitled'}${row.source?.type === 'task' && row.business_name && row.business_name !== 'Global' ? ` · ${row.business_name}` : ''}`;
    const link = row.source?.type === 'global' ? `<span class="agent-questions-place">${timelineEsc(place)}</span>`
      : `<button type="button" class="quiet agent-questions-open" data-question-open="${index}">${timelineEsc(place)}</button>`;
    return `<li class="agent-questions-row${row.blocking ? ' agent-questions-blocking' : ''}">${row.blocking ? '<span class="tlp-flag">Blocks work</span> ' : ''}<p class="agent-questions-text">${timelineEsc(row.text)}</p><p class="agent-questions-meta">${link}${timelineDay(row.created_at) ? ` <span class="tlp-day">${timelineEsc(timelineDay(row.created_at))}</span>` : ''}</p></li>`;
  }).join('');
}
// The reference the existing thread opener takes: a Task by its Project, a Project by its slug.
function agentQuestionsReference(row) {
  const source = row.source || {};
  if (source.type === 'task') return typeof taskRecordReference === 'function' ? taskRecordReference({id: source.id, business_id: source.business_id, label: row.title}) : null;
  if (source.type !== 'project') return null;
  const project = (state.bizs || []).find(b => (b.id ?? b.business_id) === source.id);
  return project?.slug ? {type: 'project', id: source.id, business_id: source.id, business_slug: project.slug, label: row.title} : null;
}
function agentQuestionsCurrent(p, questions) {
  return p.questions === questions && p.view === 'questions' && agpadActive(p, true);
}
function agentQuestionsMount(p) {
  const body = $('#agpad-body', p.el);
  if (!body || !agentPaneViewAllowed(p.workspaceRole, 'questions')) return;
  body.innerHTML = `<section class="agent-questions" aria-label="Open questions"><header><h2 class="agent-questions-heading">Open questions</h2></header>
    <p class="agent-questions-status" role="status" aria-live="polite" data-status>Loading…</p>
    <div class="agent-questions-error" hidden><p role="alert"></p><button type="button" class="quiet" data-retry>Retry</button></div>
    <ul class="agent-questions-list" aria-label="Open questions, blocking first"></ul></section>`;
  const root = body.firstElementChild;
  const questions = p.questions = {root, items: null, loading: false, seq: 0, next: 0, error: '', signature: ''};
  root.addEventListener('click', event => {
    const button = event.target.closest('[data-question-open]');
    if (!button || button.disabled) return;
    const reference = agentQuestionsReference(questions.items?.[Number(button.dataset.questionOpen)] || {});
    if (!reference) { toast('This thread is unavailable.'); return; }
    button.disabled = true;
    Promise.resolve(agentOpenRecordReference(reference)).catch(() => toast('This thread is unavailable.')).finally(() => { button.disabled = false; });
  });
  root.querySelector('[data-retry]').onclick = () => void agentQuestionsRefresh(p);
  p.workspaceDispose = () => { questions.seq++; if (p.questions === questions) p.questions = null; };
  void agentQuestionsRefresh(p);
}
function agentQuestionsPaint(p) {
  const t = p.questions;
  if (!t || !agentQuestionsCurrent(p, t)) return;
  const items = t.items;
  const blocking = items ? items.filter(row => row.blocking).length : 0;
  t.root.querySelector('[data-status]').textContent = items === null ? (t.loading ? 'Loading…' : '')
    : !items.length ? 'No open questions.' : `${items.length} open question${items.length === 1 ? '' : 's'}${blocking ? `, ${blocking} blocking` : ''}`;
  t.root.querySelector('.agent-questions-error').hidden = !t.error;
  t.root.querySelector('[role="alert"]').textContent = t.error;
  if (items !== null) {
    const signature = JSON.stringify(items);
    if (signature !== t.signature) { t.root.querySelector('ul').innerHTML = agentQuestionsHtml(items); t.signature = signature; }
  }
}
async function agentQuestionsRefresh(p) {
  const t = p.questions;
  if (!t || t.loading || !agentQuestionsCurrent(p, t) || !agpadActive(p)) return;
  const seq = ++t.seq;
  t.loading = true; t.error = ''; agentQuestionsPaint(p);
  try {
    const data = coreData(await post('/root/operations/timeline_questions_list', {scope: 'global'}));
    if (!agentQuestionsCurrent(p, t) || seq !== t.seq) return;
    if (!Array.isArray(data?.items)) throw Error('Questions unavailable.');
    t.items = data.items;
  } catch (error) {
    if (!agentQuestionsCurrent(p, t) || seq !== t.seq) return;
    t.error = uiFailure(error, {load: true});
  } finally {
    if (agentQuestionsCurrent(p, t) && seq === t.seq) { t.loading = false; t.next = Date.now() + AGPAD_POLL_MS; agentQuestionsPaint(p); }
  }
}
function agentQuestionsTick(p) {
  if (p.questions && !p.questions.loading && Date.now() >= p.questions.next) void agentQuestionsRefresh(p);
}
