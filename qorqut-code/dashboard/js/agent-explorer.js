// The Explorer: the operational layer as one read-only tree. Global is the root; under it sit the Projects, then
// Global's own Tasks; under a Project or a Task sit its Tasks. A level is read when its row is first expanded
// (`tasks_list` by parent). Selecting a row opens the existing record reader (js/record-workspace.js) on the right.
// The tree is built from the Files tab's parts (css/agent-files.css); css/agent-explorer.css adds the few rules of its own.
T_VIEW_LABEL.explorer = 'Explorer';
T_VIEW_ICON.explorer = agentFilesIcon('subprojects');

const AGEXPLORER_PAGE = 100, AGEXPLORER_SEARCH_PAGE = 20;
// A Task's status as the shared status ball (css/status-ball.css) shows it.
const AGEXPLORER_BALL = {open: 'white', working: 'in_progress', blocked: 'blocked', waiting: 'white', needs_input: 'white', done: 'done', cancelled: 'cancelled'};
const agentExplorerFinished = status => status === 'done' || status === 'cancelled';
const agentExplorerGlobe = () => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${(typeof AICONS === 'object' && AICONS.globe) || ''}</svg>`;

function agentExplorerMount(p) {
  if (!agentPaneViewAllowed(p.workspaceRole, 'explorer')) return;
  const host = $('#agpad-body', p.el);
  if (!host) return;
  host.classList.add('agfiles-host');
  const saved = p.explorerState || {};
  // lists: one read level per key (scope + parent). nodes: every row seen so far, by its row id.
  const s = {lists: new Map(), nodes: new Map(), expanded: new Set(saved.expanded || ['global']), selected: saved.selected || null,
    finished: !!saved.finished, filter: '', found: null, closed: false, generation: 0, searchRun: 0, searchTimer: null};
  const shell = document.createElement('section');
  shell.className = 'agfiles agexplorer';
  shell.setAttribute('aria-label', 'Explorer');
  shell.innerHTML = `<div class="agexplorer-top"><div class="agfiles-search">${agentFilesIcon('search')}<input type="search" placeholder="Find a project or task" aria-label="Find a project or task"></div>
    <button type="button" class="agfiles-icon-btn" data-finished aria-pressed="${s.finished}" title="Show finished" aria-label="Show finished">${agentFilesIcon('tasks')}</button></div>
    <div class="agfiles-layout"><div class="agfiles-tree" role="tree" aria-label="Global, its projects and their tasks"></div><section class="agfiles-detail" aria-label="Selected record"></section></div>`;
  host.replaceChildren(shell);
  p.explorerMount = shell;
  const tree = shell.querySelector('.agfiles-tree'), detail = shell.querySelector('.agfiles-detail');
  const live = () => !s.closed && shell.isConnected;
  const projects = () => typeof workspaceProjects === 'function' ? workspaceProjects() : (state.projects || []);
  const projectOf = slug => (state.projects || []).find(project => project.slug === slug) || null;

  // ---- rows. A row id names its scope, so a Task id alone is never trusted across Projects.
  const GLOBAL = {id: 'global', kind: 'global', scope: '', name: 'Global', parent: null};
  s.nodes.set('global', GLOBAL);
  function projectNode(project) {
    const node = {id: 'p:' + project.slug, kind: 'project', scope: project.slug, name: project.name || project.slug, parent: 'global', project};
    s.nodes.set(node.id, node);
    return node;
  }
  function taskNode(task, scope, parent) {
    const id = `t:${scope}:${task.id}`;
    const node = {id, kind: 'task', scope, name: task.title || 'Untitled task', parent, task,
      status: task.status, type: task.task_type || 'task', children: task.children || [], more: !!task.children_has_more};
    s.nodes.set(id, node);
    return node;
  }
  const finished = node => node.kind === 'task' && agentExplorerFinished(node.status);
  // The level a row opens: a Project's or Global's top Tasks, or a Task's own Tasks.
  const listKey = node => node.scope + '|' + (node.kind === 'task' ? node.task.id : '');
  const listOf = node => s.lists.get(listKey(node));
  function expandable(node) {
    if (node.kind === 'global') return true;
    const list = listOf(node);
    if (list?.loaded) return list.rows.length > 0 || !!list.next_cursor;
    if (node.kind === 'project') return true;
    // Before its level is read, a Task says so itself: it opens when it has a Task that would show.
    return node.more || node.children.some(child => s.finished || !agentExplorerFinished(child.status));
  }

  async function load(node, more = false) {
    const key = listKey(node);
    let list = s.lists.get(key);
    if (list?.loading || (!more && list?.loaded)) return;
    list ||= {rows: []};
    s.lists.set(key, list);
    list.loading = true; list.error = null;
    const generation = s.generation;
    paintTree();
    try {
      const path = node.scope ? `/project/${encodeURIComponent(node.scope)}` : '/root';
      const data = coreData(await post(`${path}/operations/tasks_list`, {parent_id: node.kind === 'task' ? node.task.id : null,
        snooze_filter: 'all', include_closed: s.finished, limit: AGEXPLORER_PAGE, ...(more && list.next_cursor ? {cursor: list.next_cursor} : {})}));
      if (!live() || generation !== s.generation) return;
      const rows = (data.tasks || []).map(task => taskNode(task, node.scope, node.id));
      list.rows = more ? [...new Map([...list.rows, ...rows].map(row => [row.id, row])).values()] : rows;
      list.next_cursor = data.next_cursor || null; list.loaded = true;
      // A row that was left open (before a reload, or on an earlier visit) reads its own level too.
      for (const child of rows) if (s.expanded.has(child.id)) void load(child);
    } catch (error) {
      if (live() && generation === s.generation) list.error = error?.message || 'These tasks could not be loaded.';
    } finally {
      if (live() && generation === s.generation) { list.loading = false; paintTree(); }
    }
  }

  function icon(node) {
    if (node.kind === 'global') return agentExplorerGlobe();
    if (node.kind === 'project') return agentFilesIcon('folder');
    if (node.type !== 'task' && typeof taskTypeIcon === 'function') return taskTypeIcon(node.type);
    return `<span class="agexplorer-ball"><span class="ntball ntb-${AGEXPLORER_BALL[node.status] || 'white'}"><span class="ntdot"></span></span></span>`;
  }
  function row(node, level, flat = false) {
    const expanded = s.expanded.has(node.id), selected = s.selected === node.id, opens = !flat && expandable(node);
    const where = flat ? `<small>${esc(node.kind === 'project' ? 'Project' : node.scope ? projectOf(node.scope)?.name || node.scope : 'Global')}</small>` : '';
    return `<div class="agfiles-row${selected ? ' selected' : ''}${finished(node) ? ' agexplorer-done' : ''}" role="treeitem" aria-level="${level + 1}" aria-selected="${selected}" ${opens ? `aria-expanded="${expanded}"` : ''} tabindex="-1" data-row="${esc(node.id)}" style="--depth:${flat ? 0 : level}" title="${esc(node.name)}">
      ${opens ? `<button type="button" tabindex="-1" class="agfiles-toggle${expanded ? ' expanded' : ''}" data-toggle="${esc(node.id)}" aria-label="${expanded ? 'Collapse' : 'Expand'} ${esc(node.name)}">${agentFilesIcon('chevron')}</button>` : '<span class="agfiles-indent"></span>'}
      <button type="button" tabindex="-1" class="agfiles-name" data-select="${esc(node.id)}">${icon(node)}<span>${esc(node.name)}${where}</span></button></div>`;
  }
  function branch(node, depth) {
    const list = listOf(node);
    let html = '';
    if (node.kind === 'global') html += projects().map(project => { const child = projectNode(project); return row(child, depth) + group(child, depth); }).join('');
    if (!list) return html;
    html += list.rows.map(child => row(child, depth) + group(child, depth)).join('');
    if (list.loading) html += `<div class="agfiles-status" role="status">Loading…</div>`;
    if (list.error) html += `<div class="agfiles-status" role="alert">${esc(list.error)} <button type="button" data-retry="${esc(node.id)}">Try again</button></div>`;
    if (list.next_cursor) html += `<button type="button" class="agfiles-more" data-more="${esc(node.id)}" ${list.loading ? 'disabled' : ''}>Show more</button>`;
    return html;
  }
  const group = (node, depth) => s.expanded.has(node.id) && expandable(node) ? `<div role="group">${branch(node, depth + 1)}</div>` : '';

  function paintTree() {
    if (!live()) return;
    const focused = tree.contains(document.activeElement) ? document.activeElement.closest('[data-row]')?.dataset.row : null;
    const scroll = tree.scrollTop;
    if (s.filter) {
      const found = s.found;
      tree.innerHTML = !found ? '<div class="agfiles-status" role="status">Searching…</div>'
        : (found.rows.length ? found.rows.map(node => row(node, 0, true)).join('') : '<div class="agfiles-status">No project or task matches.</div>')
          + (found.failed ? `<div class="agfiles-status" role="alert">${found.failed === 1 ? 'One project' : found.failed + ' projects'} could not be searched.</div>` : '');
    } else tree.innerHTML = row(GLOBAL, 0) + group(GLOBAL, 0);
    const rows = [...tree.querySelectorAll('[data-row]')];
    const current = rows.find(node => node.dataset.row === (focused || s.selected)) || rows[0];
    if (current) { current.tabIndex = 0; if (focused) current.focus({preventScroll: true}); }
    tree.scrollTop = scroll;
  }

  // ---- search: Project names here, Tasks through each scope's own search, a few scopes at a time.
  function search(text) {
    s.filter = text;
    clearTimeout(s.searchTimer);
    const run = ++s.searchRun;
    s.found = null;
    paintTree();
    if (!text) return;
    s.searchTimer = setTimeout(async () => {
      const needle = text.toLocaleLowerCase(), list = projects();
      const rows = list.filter(project => `${project.name || ''} ${project.slug}`.toLocaleLowerCase().includes(needle)).map(projectNode);
      const queue = ['', ...list.map(project => project.slug)], found = {rows, failed: 0};
      await Promise.all(Array.from({length: Math.min(4, queue.length)}, async () => {
        while (queue.length && live() && run === s.searchRun) {
          const scope = queue.shift();
          try {
            const data = coreData(await post(`${scope ? `/project/${encodeURIComponent(scope)}` : '/root'}/operations/tasks_list`,
              {search: text, snooze_filter: 'all', include_closed: s.finished, limit: AGEXPLORER_SEARCH_PAGE}));
            if (run !== s.searchRun) return;
            for (const task of data.tasks || []) found.rows.push(s.nodes.get(`t:${scope}:${task.id}`) || taskNode(task, scope, task.parent_id ? `t:${scope}:${task.parent_id}` : scope ? 'p:' + scope : 'global'));
          } catch (_) { if (run === s.searchRun) found.failed++; }
        }
      }));
      if (live() && run === s.searchRun) { s.found = found; paintTree(); }
    }, 250);
  }

  // ---- the right side: the record reader, beside the Chat area when this is Ordubashy's pane.
  let workspace = null, globalPanel = null;
  const paired = () => p.workspaceRole === 'ordubashy' && typeof agfocusLeftChatReady === 'function' && agfocusLeftChatReady();
  function reader() {
    if (workspace?.alive?.()) return workspace;
    globalPanel?.dispose(); globalPanel = null;
    detail.replaceChildren();
    const mount = document.createElement('div');
    mount.className = 'agexplorer-record';
    detail.append(mount);
    workspace = p.explorerWorkspace = recordWorkspaceMount(mount, {entry: 'explorer', paired: paired(), independentChat: true,
      onPairedSource: (record, parentTable) => agfocusPairedSource(record, parentTable),
      visible: () => live() && _agpad === p && p.view === 'explorer' && !document.hidden,
      onClose: () => { workspace = p.explorerWorkspace = null; s.selected = null; paintDetail(); paintTree(); }});
    return workspace;
  }
  function recordOf(node) {
    if (node.kind === 'project') {
      const id = node.project.id || node.project.project_id || node.project.slug;
      return rwRecordFromItem({source: {type: 'project', id, project_id: id, project_slug: node.scope, label: node.name}}, {can_thread: true});
    }
    const project = node.scope ? projectOf(node.scope) : null;
    return rwRecordFromItem({source: {type: 'task', id: node.task.id, project_id: project ? project.id || project.project_id || project.slug : null,
      ...(project ? {project_slug: project.slug, project_name: project.name} : {}), label: node.name, task_type: node.type, state: node.status}}, {can_thread: true});
  }
  // Global has no record page: its row shows Global's timeline.
  function paintGlobal() {
    workspace?.dispose?.(); workspace = p.explorerWorkspace = null;
    globalPanel?.dispose();
    detail.innerHTML = `<div class="rw agexplorer-record"><div class="rw-records"><article class="rw-record" data-tab="timeline"><header class="rw-head"><span class="rw-title" role="heading" aria-level="2">Global</span></header>
      <div class="rw-body"><div class="rw-pane rw-timeline" tabindex="0"><section class="agent-timeline" aria-label="Timeline"></section></div></div></article></div></div>`;
    const item = {type: 'global', id: 'global', project_id: null};
    globalPanel = timelineJournalMount(detail.querySelector('.agent-timeline'), {
      read: async args => coreData(await post('/root/operations/timeline_view', {scope: 'global', item, history: true, limit: 50, ...args})),
    });
    void globalPanel.refresh();
  }
  function paintDetail() {
    if (workspace?.alive?.() || globalPanel) return;
    detail.innerHTML = '<p class="agfiles-status agexplorer-empty">Select a project or a task to read it.</p>';
  }
  async function select(id) {
    const node = s.nodes.get(id);
    if (!node) return;
    if (node.kind === 'global') {
      if (workspace?.isOpen?.()) { await workspace.close(); if (workspace?.isOpen?.()) return; }
      s.selected = id; paintTree(); paintGlobal();
      return;
    }
    const record = recordOf(node);
    if (!record || record.scopeMissing) { toast('This record is unavailable.'); return; }
    const opened = await reader().open(record, {root: true});
    if (!live()) return;
    if (opened === false) { paintDetail(); return; }
    s.selected = id; paintTree();
  }
  // A record named by the Chat area or the Global timeline opens here, whether or not its row is loaded.
  async function openReference(ref) {
    if (!ref?.type || !ref.id) return false;
    const record = rwRecordFromItem({source: {type: ref.type, id: ref.id, project_id: ref.project_id ?? null, project_slug: ref.project_slug,
      project_name: ref.project_name, label: ref.label, state: ref.state, resource_type: ref.resource_type, task_type: ref.task_type}}, {can_thread: ref.can_thread});
    if (!record || record.scopeMissing) return false;
    const opened = await reader().open(record, {root: true});
    if (!live() || opened === false) return false;
    const scope = record.project || '';
    s.selected = record.source.type === 'project' ? 'p:' + scope : record.source.type === 'task' ? `t:${scope}:${record.source.id}` : null;
    paintTree();
    return true;
  }

  function toggle(id) {
    const node = s.nodes.get(id);
    if (!node) return;
    if (s.expanded.has(id)) s.expanded.delete(id);
    else { s.expanded.add(id); void load(node); }
    paintTree();
  }
  // Global and the open Projects read their levels; an open Task follows when its parent's level arrives.
  function openLevels() {
    void load(GLOBAL);
    for (const project of projects()) if (s.expanded.has('p:' + project.slug)) void load(projectNode(project));
  }
  // Every open level is read again: the finished switch changes what a level holds.
  function reload() {
    s.generation++;
    s.lists.clear();
    paintTree();
    openLevels();
    if (s.filter) search(s.filter);
  }

  shell.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || button.disabled || !shell.contains(button) || detail.contains(button)) return;
    if (button.hasAttribute('data-toggle')) toggle(button.dataset.toggle);
    else if (button.hasAttribute('data-select')) void select(button.dataset.select);
    else if (button.hasAttribute('data-more')) { const node = s.nodes.get(button.dataset.more); if (node) void load(node, true); }
    else if (button.hasAttribute('data-retry')) { const node = s.nodes.get(button.dataset.retry); if (node) { const list = listOf(node); if (list) list.loaded = false; void load(node, !!list?.next_cursor); } }
    else if (button.hasAttribute('data-finished')) { s.finished = !s.finished; button.setAttribute('aria-pressed', String(s.finished)); reload(); }
  });
  tree.addEventListener('keydown', event => {
    const current = event.target.closest('[data-row]');
    if (!current) return;
    const rows = [...tree.querySelectorAll('[data-row]')], index = rows.indexOf(current), id = current.dataset.row, node = s.nodes.get(id);
    let next;
    if (event.key === 'ArrowDown') next = rows[Math.min(index + 1, rows.length - 1)];
    else if (event.key === 'ArrowUp') next = rows[Math.max(index - 1, 0)];
    else if (event.key === 'Home') next = rows[0];
    else if (event.key === 'End') next = rows.at(-1);
    else if (event.key === 'ArrowRight' && current.hasAttribute('aria-expanded')) {
      if (!s.expanded.has(id)) toggle(id); else next = rows[index + 1];
    } else if (event.key === 'ArrowLeft') {
      if (s.expanded.has(id) && current.hasAttribute('aria-expanded')) toggle(id); else next = rows.find(other => other.dataset.row === node?.parent);
    } else if (event.key === 'Enter' || event.key === ' ') void select(id);
    else return;
    event.preventDefault();
    if (next) { rows.forEach(other => { other.tabIndex = -1; }); next.tabIndex = 0; next.focus(); }
  });
  shell.querySelector('input[type=search]').addEventListener('input', event => search(event.target.value.trim()));

  // The Chat area's thread header and a thread that has gone reach the reader through these, as they do in Focus.
  const pairedUnavailable = source => workspace?.invalidate?.(source) ?? false;
  const pairedReference = ref => openReference(ref);
  const pairedClose = () => workspace?.close?.();
  window.agentWorkspacePairedUnavailable = pairedUnavailable;
  window.agentWorkspacePairedReference = pairedReference;
  window.agentWorkspacePairedClose = pairedClose;
  // Leaving the Explorer asks before dropping unsaved edits in the open record.
  p.explorerCanLeave = async () => {
    if (!workspace?.isOpen?.() || !workspace.hasUnsaved?.()) return true;
    await workspace.close();
    return !workspace?.isOpen?.();
  };
  p.workspaceDispose = () => {
    p.explorerState = {expanded: [...s.expanded], selected: null, finished: s.finished};
    s.closed = true;
    clearTimeout(s.searchTimer);
    globalPanel?.dispose(); globalPanel = null;
    workspace?.dispose?.(); workspace = p.explorerWorkspace = null;
    p.explorerMount = null; p.explorerCanLeave = null;
    if (window.agentWorkspacePairedUnavailable === pairedUnavailable) delete window.agentWorkspacePairedUnavailable;
    if (window.agentWorkspacePairedReference === pairedReference) delete window.agentWorkspacePairedReference;
    if (window.agentWorkspacePairedClose === pairedClose) delete window.agentWorkspacePairedClose;
    host.classList.remove('agfiles-host');
  };

  paintTree(); paintDetail();
  openLevels();
}
