// Business Tasks: every Task the selected business owns, whoever it is assigned to.
// Rows, status changes and the detail are the drawer's; reads and writes stay in tasks.js.
// The same page renders as the Tasks section of an agent pane's business view.
let btSearch = '', btVisibility = 'active', btSig = null, btPaintPending = false;
const btPage = () => document.querySelector('[data-business-task-page]');

function renderBusinessTasks(c) {
  btSig = null;
  c.innerHTML = `<section class="pane tasks business-tasks" data-business-task-page aria-label="Tasks">
    <header class="business-tasks-head"><h1>Tasks</h1></header>
    <div class="task-toolbar"><label class="tsearch">${SVG_TSEARCH}<input type="search" data-business-task-search aria-label="Search tasks" autocomplete="off" spellcheck="false" value="${esc(btSearch)}"></label><select data-business-task-visibility aria-label="Task visibility">${['active', 'snoozed', 'all'].map(v => `<option value="${v}" ${btVisibility === v ? 'selected' : ''}>${v[0].toUpperCase() + v.slice(1)}</option>`).join('')}</select><button class="btn" type="button" data-business-task-new>${SVG_TPLUS}New task</button></div>
    <div class="business-task-add" hidden><input class="inp" aria-label="New task title" placeholder="Task name…" maxlength="1000" autocomplete="off" spellcheck="false"></div>
    <div class="business-task-list" data-business-task-list></div></section>`;
  const page = btPage(), search = page.querySelector('[data-business-task-search]');
  const add = page.querySelector('.business-task-add'), title = add.querySelector('input');
  let searchTimer;
  search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { btSearch = search.value; businessTasksPaint(); }, 80); };
  page.querySelector('[data-business-task-visibility]').onchange = event => { btVisibility = event.target.value; businessTasksPaint(); };
  page.querySelector('[data-business-task-new]').onclick = () => { add.hidden = false; title.focus(); };
  title.onkeydown = async event => {
    if (event.key === 'Escape') { event.preventDefault(); title.value = ''; add.hidden = true; return; }
    if (event.key !== 'Enter' || event.isComposing) return;
    event.preventDefault();
    const text = title.value.trim(); if (!text || title.disabled) return;
    title.disabled = true;
    // An unconfirmed create keeps the typed title; it is never retried automatically.
    try { if (await tadd({title: text, tags: []})) { title.value = ''; add.hidden = true; } }
    catch (_) { toast("Couldn't save. Try again"); }
    finally { title.disabled = false; }
  };
  businessTasksPaint();
  refreshTasks();
}

function businessTasksMatch(task, query) {
  return !query || String(task.title).toLowerCase().includes(query) || (task.tags || []).some(tag => tag.includes(query))
    || (task.notes || []).some(note => String(note.text || '').toLowerCase().includes(query));
}

function businessTasksPaint() {
  const page = btPage(); if (!page || tEditingNow()) return;
  const list = page.querySelector('[data-business-task-list]'), biz = state.biz;
  const loaded = state.tasksBiz === biz, failed = !loaded && state.tasksError === biz;
  const query = btSearch.trim().toLowerCase();
  const rows = (loaded ? state.tasks : []).filter(task => tSlug(task) === biz && businessTasksMatch(task, query)
    && (btVisibility === 'all' || !tSunk(task) && taskSnoozed(task) === (btVisibility === 'snoozed')))
    .sort((a, b) => Number(tSunk(a)) - Number(tSunk(b)) || String(a.due || '9999').localeCompare(b.due || '9999') || a.title.localeCompare(b.title) || tKey(a).localeCompare(tKey(b)));
  const sig = JSON.stringify([biz, loaded, failed, btVisibility, query, rows.map(task =>
    [tKey(task), task.status, tAssignees(task).join(','), task.title, task.due || '', (task.tags || []).join(','), task.created_by, task.created_by_name, tSunk(task), task.snoozed_until, (task.parent || {}).title])]);
  if (sig === btSig && list.childElementCount) return;
  btSig = sig;
  list.innerHTML = failed ? `<p class="personal-task-error" role="alert">Couldn't load tasks. <button class="ghost" type="button" data-business-task-retry>Retry</button></p>`
    : !loaded ? tEmptyHtml('Loading tasks…')
    : rows.length ? `<div class="personal-task-list">${rows.map(task => tRow(task)).join('')}</div>`
    : tEmptyHtml(query ? 'No matching tasks.' : btVisibility === 'snoozed' ? 'No snoozed tasks.' : 'No open tasks.');
  list.querySelector('[data-business-task-retry]')?.addEventListener('click', () => refreshTasks());
  tWireRows(list);
  list.querySelectorAll('.trow').forEach(row => {
    const task = taskById(row.dataset.key); if (!task) return;
    const ball = row.querySelector('.ball');
    if (ball) ball.onclick = event => { event.stopPropagation(); taskStateSet(tSlug(task), task, tNext(task.status)); };
    row.onclick = event => { if (!event.target.closest('button')) tDetail(task); };
  });
}

// A status change elsewhere (the detail, Focus, another row) repaints this page once.
taskStateSubscribe(scope => {
  if (!btPage() || scope !== state.biz || btPaintPending) return;
  btPaintPending = true;
  Promise.resolve().then(() => { btPaintPending = false; businessTasksPaint(); });
});
