// The Work tab: the active work (Tasks and Stories, plus Routines) in one read-only view, for all projects or for one,
// grouped by status, as a List or a Board. Rows open the existing record reader (js/record-workspace.js), as in the Explorer.
// The shell is the Explorer's (css/agent-files.css, css/agent-explorer.css); css/agent-work.css adds the few rules of its own.
T_VIEW_LABEL.work = 'Work';
T_VIEW_ICON.work = agentFilesIcon('tasks');

const AGWORK_PAGE = 100, AGWORK_MAX_PAGES = 10, AGWORK_PARALLEL = 6;
const AGWORK_MOVING = ['needs_input', 'working', 'blocked', 'open'];
const AGWORK_WORKERS_SHOWN = 3;
const agentWorkFinished = status => status === 'done' || status === 'cancelled';
const agentWorkActiveChildren = task => (task.children || []).filter(child => !agentWorkFinished(child.status));
// Waiting or snoozed work is parked; everything else is moving.
const agentWorkParked = task => task.status === 'waiting' || taskSnoozed(task);
// The group a Task sits in: 'parked', or one of the four moving statuses. A status this view does not know counts as Open,
// so no active Task is ever left out of every group.
const agentWorkGroup = task => agentWorkParked(task) ? 'parked' : AGWORK_MOVING.includes(task.status) ? task.status : 'open';
// A due date first (earliest first), then the queue rank (missing last), then the title.
function agentWorkCompare(a, b) {
  const x = a.task, y = b.task;
  if (!!x.due !== !!y.due) return x.due ? -1 : 1;
  if (x.due && y.due && x.due !== y.due) return x.due < y.due ? -1 : 1;
  const rx = typeof x.queue_rank === 'number' ? x.queue_rank : Infinity, ry = typeof y.queue_rank === 'number' ? y.queue_rank : Infinity;
  if (rx !== ry) return rx < ry ? -1 : 1;
  return String(x.title || '').localeCompare(String(y.title || ''));
}
function agentWorkRoutineCompare(a, b) {
  const x = a.routine, y = b.routine;
  if ((x.status === 'paused') !== (y.status === 'paused')) return x.status === 'paused' ? 1 : -1;
  const nx = x.next || '￿', ny = y.next || '￿';
  if (nx !== ny) return nx < ny ? -1 : 1;
  return String(x.name || '').localeCompare(String(y.name || ''));
}
// The scope key of a row is '' for Global or the project's slug; a row key is `${scope}:${id}`.
const agentWorkKey = (scope, id) => `${scope}:${id}`;
// What was last read, by scope key, kept for the page's life: the tab paints from it at once and then reads again quietly.
const agentWorkSnapshot = {tasks: new Map(), routines: new Map()};

function agentWorkMount(p) {
  if (!agentPaneViewAllowed(p.workspaceRole, 'work')) return;
  const host = $('#agpad-body', p.el);
  if (!host) return;
  host.classList.add('agfiles-host');
  const saved = p.workState || {};
  // scope: '*' all projects, '' Global, or a project slug. tasks / routines: what was read, by scope key.
  const s = {scope: typeof saved.scope === 'string' ? saved.scope : '*', layout: saved.layout === 'board' ? 'board' : 'list',
    parkedOpen: !!saved.parkedOpen, expanded: new Set(saved.expanded || []), filter: '',
    tasks: new Map(), routines: new Map(), tasksLoaded: false, routinesLoaded: false, failed: 0, closed: false, generation: 0, open: false, scroll: 0};
  const shell = document.createElement('section');
  shell.className = 'agfiles agwork';
  shell.setAttribute('aria-label', 'Work');
  shell.innerHTML = `<div class="agexplorer-top agwork-top"><div class="agfiles-search">${agentFilesIcon('search')}<input type="search" placeholder="Find a task" aria-label="Find a task"></div>
    <button type="button" class="agwork-scope" data-scope aria-haspopup="listbox"><span class="agwork-scope-name">All projects</span>${agentFilesIcon('chevron')}</button>
    <span class="agwork-layout" role="group" aria-label="Layout">
      <button type="button" class="agfiles-icon-btn" data-layout="list" aria-pressed="true" title="List" aria-label="List">${agentFilesIcon('tasks')}</button>
      <button type="button" class="agfiles-icon-btn" data-layout="board" aria-pressed="false" title="Board" aria-label="Board">${agentFilesIcon('board')}</button>
    </span></div>
    <div class="agwork-body"></div>
    <section class="agwork-record" aria-label="Selected record" hidden></section>`;
  host.replaceChildren(shell);
  p.workMount = shell;
  const top = shell.querySelector('.agwork-top'), body = shell.querySelector('.agwork-body'), recordHost = shell.querySelector('.agwork-record');
  const scopeButton = shell.querySelector('[data-scope]'), scopeName = shell.querySelector('.agwork-scope-name');
  const live = () => !s.closed && shell.isConnected;
  const projects = () => typeof workspaceProjects === 'function' ? workspaceProjects() : (state.projects || []);
  const projectOf = slug => (state.projects || []).find(project => project.slug === slug) || null;
  const projectName = slug => slug ? projectOf(slug)?.name || slug : 'Global';
  if (s.scope !== '*' && s.scope !== '' && !projectOf(s.scope)) s.scope = '*';
  const scopes = () => s.scope === '*' ? ['', ...projects().map(project => project.slug)] : [s.scope];

  function paintControls() {
    scopeName.textContent = s.scope === '*' ? 'All projects' : projectName(s.scope);
    scopeButton.title = scopeName.textContent;
    shell.querySelectorAll('[data-layout]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.layout === s.layout)));
  }

  // ---- reading. Tasks first, Routines after: the Task groups paint as soon as the Task reads end, and only then are the
  // Routines read (the last group), so the slower Routine reads never compete with the Task reads.
  async function pool(list, work, generation) {
    const queue = [...list];
    await Promise.all(Array.from({length: Math.min(AGWORK_PARALLEL, queue.length)}, async () => {
      while (queue.length && live() && generation === s.generation) await work(queue.shift());
    }));
  }
  async function readTasks(generation, background) {
    // A first read shows each scope's rows as they arrive; a quiet re-read swaps everything in at the end.
    const found = background ? new Map() : s.tasks;
    let failed = 0;
    await pool(scopes(), async scope => {
      const path = scope ? `/project/${encodeURIComponent(scope)}` : '/root';
      const rows = [];
      try {
        let cursor = null;
        for (let page = 0; page < AGWORK_MAX_PAGES; page++) {
          const data = coreData(await post(`${path}/operations/tasks_list`, {parent_id: null, snooze_filter: 'all', include_closed: false,
            limit: AGWORK_PAGE, ...(cursor ? {cursor} : {})}));
          if (!live() || generation !== s.generation) return;
          rows.push(...(data.tasks || []));
          const next = data.next_cursor || null;
          if (!next || next === cursor) break;
          cursor = next;
        }
        found.set(scope, rows);
        agentWorkSnapshot.tasks.set(scope, rows);
        if (!background) paintBody();
      } catch (_) {
        if (!live() || generation !== s.generation) return;
        // A quiet re-read that fails keeps the rows already shown for that scope.
        if (background && s.tasks.has(scope)) found.set(scope, s.tasks.get(scope)); else failed++;
      }
    }, generation);
    if (!live() || generation !== s.generation) return;
    s.tasks = found; s.failed = failed; s.tasksLoaded = true;
    paintBody();
  }
  async function readRoutines(generation, background) {
    if (!live() || generation !== s.generation) return;
    const found = new Map();
    if (!background) s.routines = found;
    await pool(scopes(), async scope => {
      try {
        const data = await api(scope ? `/project/${encodeURIComponent(scope)}/routines` : '/root/routines');
        if (!live() || generation !== s.generation) return;
        found.set(scope, (data?.routines || []).filter(routine => scope ? routine.scope !== 'global' : routine.scope === 'global'));
        agentWorkSnapshot.routines.set(scope, found.get(scope));
        if (!background) paintBody();
      } catch (_) {
        // A routines read that fails is silent: the group is simply missing, or stays as it was on a quiet re-read.
        if (background && live() && generation === s.generation && s.routines.has(scope)) found.set(scope, s.routines.get(scope));
      }
    }, generation);
    if (!live() || generation !== s.generation) return;
    s.routines = found; s.routinesLoaded = true;
    paintBody();
  }
  // A background read keeps what is shown until the answer arrives, so nothing flashes "Loading…".
  function reload(background = false) {
    const generation = ++s.generation;
    if (!background) {
      // Scopes read earlier on this page paint at once from the snapshot; the read that follows is then a quiet one.
      const keep = (from, list) => new Map(list.filter(scope => from.has(scope)).map(scope => [scope, from.get(scope)]));
      const list = scopes();
      s.tasks = keep(agentWorkSnapshot.tasks, list); s.routines = keep(agentWorkSnapshot.routines, list);
      s.tasksLoaded = s.tasks.size === list.length; s.routinesLoaded = s.tasksLoaded && s.routines.size === list.length; s.failed = 0;
      background = s.tasksLoaded;
    }
    paintBody();
    void readTasks(generation, background).then(() => readRoutines(generation, background));
  }

  // ---- rows
  const needle = () => s.filter;
  function taskRows() {
    const rows = [];
    for (const [scope, list] of s.tasks) for (const task of list) {
      if (task.task_type === 'customer' || task.task_type === 'email' || agentWorkFinished(task.status)) continue;
      const project = projectName(scope);
      // The search reads what the row shows: the project's name only where rows carry it.
      if (needle() && !`${task.title || ''} ${s.scope === '*' ? project : ''}`.toLocaleLowerCase().includes(needle())) continue;
      rows.push({task, scope, project, group: agentWorkGroup(task)});
    }
    return rows.sort(agentWorkCompare);
  }
  function routineRows() {
    const rows = [];
    for (const [scope, list] of s.routines) for (const routine of list) {
      if (needle() && !String(routine.name || '').toLocaleLowerCase().includes(needle())) continue;
      rows.push({routine, scope, project: projectName(scope)});
    }
    return rows.sort(agentWorkRoutineCompare);
  }
  function workerAvatars(task) {
    const ids = task.worker_ids || [];
    if (!ids.length) return '';
    const actors = [...(task.workers || []), ...(task.assignees || [])];
    const shown = ids.slice(0, AGWORK_WORKERS_SHOWN).map(id => {
      const actor = actors.find(candidate => candidate.id === id) || {};
      const name = actor.display_name || actor.name || 'Unknown';
      return identityAvatar(actor.kind === 'agent' ? 'agent' : actor.kind === 'human' ? 'founder' : 'unknown', name, globalAgentProfile(id) || actor, 'Worker: ' + name, true);
    }).join('');
    const more = ids.length > AGWORK_WORKERS_SHOWN ? `<span class="task-assignee-more">+${ids.length - AGWORK_WORKERS_SHOWN}</span>` : '';
    return `<span class="task-workers" aria-label="Workers">${shown}${more}</span>`;
  }
  // Under the title only a snoozed Task says until when. The status line is the row's tooltip, not text on screen.
  function secondLine(entry) {
    const text = taskSnoozed(entry.task) ? taskSnoozeLabel(entry.task) : '';
    return text ? `<small class="task-snooze-label agwork-line">${esc(text)}</small>` : '';
  }
  const projectLabel = entry => s.scope === '*' ? `<small class="task-snooze-label agwork-project" title="${esc(entry.project)}">${esc(entry.project)}</small>` : '';
  const hint = task => task.status_line ? ` title="${esc(task.status_line)}"` : '';
  function chipText(task) {
    const count = agentWorkActiveChildren(task).length;
    if (!count) return null;
    const plus = task.children_has_more ? '+' : '';
    return {count: count + plus, word: count === 1 && !plus ? 'subtask' : 'subtasks', label: `${count}${plus} ${count === 1 && !plus ? 'subtask' : 'subtasks'}`};
  }
  const chipInner = chip => `${esc(chip.count)}<span class="agwork-chip-word"> ${chip.word}</span>`;
  function marker(task) {
    if (!taskShowsState(task)) return `<span class="task-type-marker">${taskTypeIcon(task.task_type)}</span>`;
    const label = T_LABEL[task.status] || task.status;
    return `<span class="ball ${esc(task.status)}" role="img" aria-label="Status: ${esc(label)}"><span class="core"></span></span>`;
  }
  function childRow(child, scope) {
    const status = child.status || 'open';
    const mark = taskShowsState(child) ? `<span class="ball ${esc(status)}" role="img" aria-label="Status: ${esc(T_LABEL[status] || status)}"><span class="core"></span></span>` : taskTypeIcon(child.task_type);
    return `<article class="record-child-row" style="--record-depth:1" tabindex="0" role="button" data-task="${esc(agentWorkKey(scope, child.id))}" aria-label="${esc(child.title || 'Untitled task')}">
      <span class="record-child-marker">${mark}</span><span class="record-child-head"><strong class="record-child-title">${esc(child.title || 'Untitled task')}</strong></span></article>`;
  }
  function listRow(entry) {
    const {task, scope} = entry, key = agentWorkKey(scope, task.id), chip = chipText(task), expanded = s.expanded.has(key);
    const tail = (chip ? `<button type="button" class="task-chip" data-children="${esc(key)}" aria-expanded="${expanded}" aria-label="${esc(chip.label)}">${chipInner(chip)}</button>` : '')
      + (task.due ? tDue(task) : '') + projectLabel(entry) + workerAvatars(task);
    const title = task.title || 'Untitled task';
    return `<div class="trow agwork-row ${esc(task.status)}" data-task="${esc(key)}" tabindex="0" role="button" aria-label="${esc(title)}"${hint(task)}>${marker(task)}
      <div class="agwork-main"><p class="ttl">${esc(title)}</p>${secondLine(entry)}</div>
      ${tail ? `<span class="agwork-tail">${tail}</span>` : ''}</div>`
      + (chip && expanded ? `<div class="record-child-list agwork-children">${agentWorkActiveChildren(task).map(child => childRow(child, scope)).join('')}</div>` : '');
  }
  function card(entry) {
    const {task, scope} = entry, chip = chipText(task), title = task.title || 'Untitled task';
    const foot = projectLabel(entry) + (chip ? `<span class="task-chip">${chipInner(chip)}</span>` : '') + (task.due ? tDue(task) : '') + workerAvatars(task);
    return `<article class="agwork-card" data-task="${esc(agentWorkKey(scope, task.id))}" tabindex="0" role="button" aria-label="${esc(title)}"${hint(task)}>
      <p class="ttl">${esc(title)}</p>${secondLine(entry)}${foot ? `<div class="agwork-card-foot">${foot}</div>` : ''}</article>`;
  }
  const heading = (label, count, extra = '', lead = '') => `<div class="tsh">${lead}<h2>${esc(label)}</h2><span class="c">${count}</span>${extra}</div>`;
  function parkedGroup(rows) {
    if (!rows.length) return '';
    const open = s.parkedOpen || !!s.filter;
    const fold = s.filter ? '' : `<button type="button" class="agwork-fold" data-fold aria-expanded="${open}" aria-label="${open ? 'Hide' : 'Show'} waiting or snoozed">${open ? 'Hide' : 'Show'}</button>`;
    return `<section class="tsec" data-group="parked">${heading('Waiting or snoozed', rows.length, fold)}${open ? `<div class="tsi">${rows.map(listRow).join('')}</div>` : ''}</section>`;
  }
  function routinesGroup(rows) {
    if (!rows.length) return '';
    const withProject = s.scope === '*';
    return `<section class="tsec" data-group="routines">${heading('Routines', rows.length)}<div class="rtlist">${rows.map(({routine, project}) => {
      const next = routine.status === 'paused' ? 'Paused' : routine.scheduling_available === false ? routine.scheduling_unavailable_reason || 'Scheduling unavailable' : rtNext(routine.next);
      return `<article class="rtrow">${rtScope(routine.scope)}<span class="rttitle" title="${esc(routine.name)}">${esc(routine.name)}</span>
        <div class="rtmeta" title="${esc(next)}">${esc(next)}${withProject ? `<span class="sep">·</span>${esc(project)}` : ''}</div></article>`;
    }).join('')}</div></section>`;
  }

  // ---- painting
  function paintBody() {
    if (!live()) return;
    const scroll = body.scrollTop;
    const active = body.contains(document.activeElement) ? document.activeElement : null;
    const focus = active ? ['data-task', 'data-children', 'data-fold', 'data-retry'].map(name => [name, active.getAttribute(name)]).find(pair => pair[1] !== null) : null;
    let html = '';
    if (!s.tasksLoaded && !s.tasks.size) html = '<div class="agfiles-status" role="status">Loading…</div>';
    else {
      const rows = taskRows(), routines = routineRows();
      const parked = rows.filter(entry => entry.group === 'parked');
      if (s.failed) html += `<div class="agfiles-status" role="alert">${s.failed === 1 ? 'One project' : s.failed + ' projects'} could not be loaded. <button type="button" data-retry>Try again</button></div>`;
      if (!rows.length && !routines.length) {
        // Routines arrive on their own: nothing is called empty before they have, nor when a project failed to load.
        if (!s.tasksLoaded || !s.routinesLoaded) html += s.failed ? '' : '<div class="agfiles-status" role="status">Loading…</div>';
        else if (s.filter) html += '<p class="agfiles-status">Nothing matches.</p>';
        else if (!s.failed) html += '<p class="agfiles-status">No active work here.</p>';
      } else if (s.layout === 'board') {
        html += `<div class="agwork-board">${AGWORK_MOVING.map(status => {
          const cards = rows.filter(entry => entry.group === status);
          return `<section class="tsec agwork-column" data-group="${status}">${heading(T_LABEL[status], cards.length, '', `<span class="sdot ${status}"></span>`)}
            <div class="tsi">${cards.map(card).join('') || '<p class="agfiles-status">Nothing here.</p>'}</div></section>`;
        }).join('')}</div>`;
      } else {
        for (const status of AGWORK_MOVING) {
          const group = rows.filter(entry => entry.group === status);
          if (group.length) html += `<section class="tsec" data-group="${status}">${heading(T_LABEL[status], group.length)}<div class="tsi">${group.map(listRow).join('')}</div></section>`;
        }
      }
      if (rows.length || routines.length) html += parkedGroup(parked);
      html += routinesGroup(routines);
    }
    body.innerHTML = html;
    body.scrollTop = scroll;
    if (focus) {
      const again = [...body.querySelectorAll(`[${focus[0]}]`)].find(node => node.getAttribute(focus[0]) === focus[1]);
      again?.focus({preventScroll: true});
    }
  }

  // ---- the record reader. It replaces the list while a record is open; closing it brings the list back where it was.
  let workspace = null;
  const paired = () => p.workspaceRole === 'ordubashy' && typeof agfocusLeftChatReady === 'function' && agfocusLeftChatReady();
  function showRecord(open) {
    s.open = open;
    top.hidden = open; body.hidden = open; recordHost.hidden = !open;
    if (!open) body.scrollTop = s.scroll;
  }
  function reader() {
    if (workspace?.alive?.()) return workspace;
    recordHost.replaceChildren();
    const mount = document.createElement('div');
    mount.className = 'agwork-record-mount';
    recordHost.append(mount);
    workspace = p.workWorkspace = recordWorkspaceMount(mount, {entry: 'work', paired: paired(), independentChat: true,
      onPairedSource: (record, parentTable) => agfocusPairedSource(record, parentTable),
      visible: () => live() && _agpad === p && p.view === 'work' && !document.hidden,
      onClose: () => {
        workspace = p.workWorkspace = null;
        if (!live()) return;
        showRecord(false);
        reload(true);
      }});
    return workspace;
  }
  function findTask(scope, id) {
    for (const task of s.tasks.get(scope) || []) {
      if (task.id === id) return task;
      const child = (task.children || []).find(candidate => candidate.id === id);
      if (child) return child;
    }
    return null;
  }
  function recordOf(scope, id) {
    const task = findTask(scope, id) || {};
    const project = scope ? projectOf(scope) : null;
    return rwRecordFromItem({source: {type: 'task', id, project_id: project ? project.id || project.project_id || project.slug : null,
      ...(project ? {project_slug: project.slug, project_name: project.name} : {}), label: task.title || 'Untitled task', task_type: task.task_type || 'task', state: task.status}}, {can_thread: true});
  }
  async function openRecord(record) {
    if (!record || record.scopeMissing) { toast('This record is unavailable.'); return false; }
    if (!s.open) s.scroll = body.scrollTop;
    showRecord(true);
    const opened = await reader().open(record, {root: true});
    if (!live()) return false;
    if (opened === false && !workspace?.isOpen?.()) { showRecord(false); return false; }
    return opened !== false;
  }
  function select(key) {
    const at = key.indexOf(':');
    void openRecord(recordOf(key.slice(0, at), key.slice(at + 1)));
  }
  // A record named by the Chat area opens here too.
  async function openReference(ref) {
    if (!ref?.type || !ref.id) return false;
    const record = rwRecordFromItem({source: {type: ref.type, id: ref.id, project_id: ref.project_id ?? null, project_slug: ref.project_slug,
      project_name: ref.project_name, label: ref.label, state: ref.state, resource_type: ref.resource_type, task_type: ref.task_type}}, {can_thread: ref.can_thread});
    if (!record || record.scopeMissing) return false;
    return openRecord(record);
  }

  // ---- events
  shell.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (button && !button.disabled && shell.contains(button) && !recordHost.contains(button)) {
      if (button.hasAttribute('data-layout')) { s.layout = button.dataset.layout; paintControls(); paintBody(); return; }
      if (button.hasAttribute('data-scope')) {
        const items = [{value: '*', label: 'All projects'}, {value: '', label: 'Global'}, ...projects().map(project => ({value: project.slug, label: project.name || project.slug}))];
        dropdown(button, items, value => { if (value === s.scope) return; s.scope = value; paintControls(); body.scrollTop = 0; reload(false); }, s.scope);
        return;
      }
      if (button.hasAttribute('data-fold')) { s.parkedOpen = !s.parkedOpen; paintBody(); return; }
      if (button.hasAttribute('data-retry')) { reload(false); return; }
      if (button.hasAttribute('data-children')) {
        const key = button.dataset.children;
        if (s.expanded.has(key)) s.expanded.delete(key); else s.expanded.add(key);
        paintBody();
        return;
      }
    }
    const target = event.target.closest('[data-task]');
    if (target && body.contains(target)) select(target.dataset.task);
  });
  body.addEventListener('keydown', event => {
    const target = event.target;
    if (!target.hasAttribute?.('data-task') || (event.key !== 'Enter' && event.key !== ' ')) return;
    event.preventDefault();
    select(target.dataset.task);
  });
  shell.querySelector('input[type=search]').addEventListener('input', event => { s.filter = event.target.value.trim().toLocaleLowerCase(); paintBody(); });

  // The Chat area's thread header and a thread that has gone reach the reader through these, as they do in Focus.
  const pairedUnavailable = source => workspace?.invalidate?.(source) ?? false;
  const pairedReference = ref => openReference(ref);
  const pairedClose = () => workspace?.close?.();
  window.agentWorkspacePairedUnavailable = pairedUnavailable;
  window.agentWorkspacePairedReference = pairedReference;
  window.agentWorkspacePairedClose = pairedClose;
  // Leaving the Work tab asks before dropping unsaved edits in the open record.
  p.workCanLeave = async () => {
    if (!workspace?.isOpen?.() || !workspace.hasUnsaved?.()) return true;
    await workspace.close();
    return !workspace?.isOpen?.();
  };
  p.workspaceDispose = () => {
    p.workState = {scope: s.scope, layout: s.layout, parkedOpen: s.parkedOpen, expanded: [...s.expanded]};
    s.closed = true;
    workspace?.dispose?.(); workspace = p.workWorkspace = null;
    p.workMount = null; p.workCanLeave = null;
    if (window.agentWorkspacePairedUnavailable === pairedUnavailable) delete window.agentWorkspacePairedUnavailable;
    if (window.agentWorkspacePairedReference === pairedReference) delete window.agentWorkspacePairedReference;
    if (window.agentWorkspacePairedClose === pairedClose) delete window.agentWorkspacePairedClose;
    host.classList.remove('agfiles-host');
  };

  paintControls();
  reload(false);
}
