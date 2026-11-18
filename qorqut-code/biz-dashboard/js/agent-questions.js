// Open questions in the sign-off queue. Ordubashy raises them on a Task, a Project or Global; each one is a chip in the Focus
// strip next to the approvals. Opening one shows the item's thread on the left (the paired record workspace) and an answer
// card on the right, above the item's dashboard; a Global question has no thread, so its card opens on its own. The owner's
// answer is filed at once as a decision or fact on the item and closes the question (timeline_question_answer); Ordubashy is
// told. Uses timelineEsc and timelineDay from timeline-panel.js.

const AGENT_QUESTION_KINDS = {task: 'Task', project: 'Project', global: 'Global'};
const AGENT_QUESTION_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.6 2.2c-.8.4-1.2.9-1.2 1.8M12 16.6v.1"/></svg>';

function agentQuestionPlace(row) {
  const kind = AGENT_QUESTION_KINDS[row.source?.type] || '';
  if (row.source?.type === 'global') return 'Global';
  const project = row.source?.type === 'task' && row.business_name && row.business_name !== 'Global' ? ` · ${row.business_name}` : '';
  return `${kind} · ${row.title || 'Untitled'}${project}`;
}

// The card: the question, where it was raised, and the answer form. ``onAnswered`` runs once the answer is filed.
function agentQuestionCard(row, {onAnswered} = {}) {
  const el = document.createElement('section');
  el.className = 'question-card';
  el.setAttribute('aria-label', 'Open question');
  el.innerHTML = `<p class="question-card-kicker">${row.blocking ? '<span class="tlp-flag">Blocks work</span> ' : ''}Open question · ${timelineEsc(agentQuestionPlace(row))}${timelineDay(row.created_at) ? ` · <span class="tlp-day">${timelineEsc(timelineDay(row.created_at))}</span>` : ''}</p>`
    + `<p class="question-card-text">${timelineEsc(row.text)}</p>`
    + `<form class="question-card-form"><textarea rows="3" maxlength="400" aria-label="Your answer" placeholder="Your answer, in your own words"></textarea>`
    + `<div class="question-card-actions"><label class="question-card-kind">File it as <select aria-label="File it as"><option value="decision">Decision</option><option value="fact">Fact</option></select></label>`
    + `<button type="submit" class="sg-approve">Send answer</button></div><p class="question-card-status" role="status" hidden></p></form>`;
  const form = el.querySelector('form'), area = el.querySelector('textarea'), send = el.querySelector('[type=submit]'), status = el.querySelector('[role=status]');
  let key = null, done = false;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const text = area.value.trim();
    if (!text || send.disabled || done) return;
    // The same retry key until the answer is confirmed, so a retried send never files it twice.
    key = key || (typeof newRequestId === 'function' ? newRequestId() : crypto.randomUUID());
    send.disabled = true; status.hidden = true;
    try {
      coreData(await post('/root/operations/timeline_question_answer', {scope: 'global', question_id: row.id, text,
        kind: el.querySelector('select').value, idempotency_key: key}));
      done = true; area.disabled = true;
      status.textContent = 'Answer filed. Ordubashy is told.'; status.hidden = false;
      onAnswered?.();
    } catch (error) {
      status.textContent = uiFailure(error); status.hidden = false;
    } finally { if (!done) send.disabled = false; }
  });
  return {el, dirty: () => !done && !!area.value.trim(), focus: () => area.focus()};
}

// Opens one question from the queue. A Task or Project question opens its record paired, with the card above the dashboard;
// a Global one opens the card in the Focus surface. Returns a handle the queue can follow, or null when nothing opened.
async function agentQuestionOpen(row, hooks = {}) {
  const source = row.source || {};
  let card = null;
  const answered = () => hooks.onResolved?.();
  if (source.type !== 'task' && source.type !== 'project') {
    return window.focusExpansion.open({title: 'Open question', mount(host) {
      card = agentQuestionCard(row, {onAnswered: answered});
      host.append(card.el); card.focus();
      return () => { card.el.remove(); hooks.onClose?.(); };
    }});
  }
  const p = typeof _agpad !== 'undefined' ? _agpad : null;
  if (!p) throw new Error('The Focus pane is unavailable.');
  const project = source.type === 'project' ? (state.bizs || []).find(b => (b.id ?? b.business_id) === source.id) : null;
  const businessId = source.business_id ?? null;
  const slug = businessId == null ? null : (state.bizs || []).find(b => (b.id ?? b.business_id) === businessId)?.slug || project?.slug || null;
  const target = rwRecordFromItem({source: {type: source.type, id: source.id, business_id: businessId, business_slug: slug,
    label: row.title}}, {can_thread: true});
  if (!target) throw new Error('This thread is unavailable.');
  target.question = {row, card: () => card, mount: el => { card = agentQuestionCard(row, {onAnswered: answered}); el.append(card.el); card.focus(); return card; }};
  if (p.surface) { await agpadSurfaceClose(p); if (p.surface) return null; }
  if (!await agfocusWorkspaceMode(p, true)) return null;
  agfocusShowDock(p);
  if (!p.focusWorkspace?.alive?.()) return null;
  await p.focusWorkspace.navigate(target);
  // The item's thread on the left, as Open thread would; the card and the dashboard stay on the right.
  try { await p.focusWorkspace.openThread?.(); } catch (_) { /* the dashboard and card still open */ }
  const disposers = [];
  return {
    isCurrent: () => !!card?.el?.isConnected,
    close: async () => {},
    onDispose: fn => disposers.push(fn),
  };
}
