// The owner's operations tree. Lazy directory reads share the standalone file
// viewer's authority and preview implementation; no file contents enter this DOM.
const AGFILES_ICONS = {
  folder: 'M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z',
  file: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 13h8M8 17h5',
  code: 'm8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18',
  tasks: 'm3 6 1 1 2-2m-3 8 1 1 2-2m-3 8 1 1 2-2M10 6h11M10 13h11M10 20h11',
  signoffs: 'M8 15v-3c0-2 1-2 1-5a3 3 0 1 1 6 0c0 3 1 3 1 5v3M5 15h14l2 4H3ZM5 22h14',
  subprojects: 'M3 3h6v6H3ZM15 15h6v6h-6ZM15 3h6v6h-6ZM6 9v9h9M9 6h6',
  chevron: 'm9 5 7 7-7 7',
  refresh: 'M20 7a9 9 0 1 0 1 8M20 2v6h-6',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  external: 'M14 3h7v7m0-7L10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5',
  copy: 'M9 9h12v12H9ZM5 15H3V3h12v2',
};
function agentFilesIcon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${AGFILES_ICONS[name] || AGFILES_ICONS.file}"/></svg>`;
}
T_VIEW_LABEL.files = 'Files';
T_VIEW_ICON.files = agentFilesIcon('folder');

function agentFilesMetrics(project) {
  const unavailable = 'Unavailable for this folder: no active accessible project record';
  return `<span class="agfiles-metrics">${[
    ['tasks', project?.open_tasks, project ? 'Open tasks, including snoozed tasks; all assignees' : unavailable],
    ['signoffs', project?.signoffs, project ? 'Pending signoffs visible to you' : unavailable],
    ['subprojects', project?.subprojects, 'Subprojects: no project hierarchy recorded'],
  ].map(([icon, value, title]) => `<span title="${esc(title)}" aria-label="${esc(title)}: ${value == null ? 'unavailable' : value}">${agentFilesIcon(icon)}<span>${value ?? '—'}</span></span>`).join('')}</span>`;
}
function agentFilesEntryIcon(entry) {
  return agentFilesIcon(entry.kind === 'directory' ? entry.name === 'code' ? 'code' : 'folder' : 'file');
}
function agentFilesSize(bytes) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function agentFilesMount(p) {
  const host = $('#agpad-body', p.el);
  if (!host) return;
  host.classList.add('agfiles-host');
  const saved = p.filesState || {};
  const s = {dirs:new Map(), expanded:new Set(saved.expanded || ['', 'projects']), selected:saved.selected || {path:'projects', name:'Projects', kind:'directory'},
    hidden:!!saved.hidden, filter:'', closed:false, generation:0, controllers:new Set()};
  const shell = document.createElement('section');
  shell.className = 'agfiles';
  shell.setAttribute('aria-label', 'Operations files');
  shell.innerHTML = `<header class="agfiles-head"><div><strong>Operations</strong><span>Projects, notes & agent workspaces</span></div>
    <button type="button" class="agfiles-icon-btn" data-refresh title="Refresh folders and counts" aria-label="Refresh folders and counts">${agentFilesIcon('refresh')}</button></header>
    <div class="agfiles-search">${agentFilesIcon('search')}<input type="search" placeholder="Filter loaded folders and files…" aria-label="Filter loaded folders and files"><label><input type="checkbox" data-hidden ${s.hidden ? 'checked' : ''}> Hidden</label></div>
    <nav class="agfiles-crumbs" aria-label="File location"></nav>
    <div class="agfiles-layout"><div class="agfiles-tree" role="tree" aria-label="Operations folder tree"></div><section class="agfiles-detail" aria-label="Selected file or folder"></section></div>
    <footer class="agfiles-footer"><span>Operations folders</span><span title="Open tasks · pending signoffs · subprojects">${agentFilesIcon('tasks')} Tasks ${agentFilesIcon('signoffs')} Signoffs ${agentFilesIcon('subprojects')} Subprojects</span></footer>`;
  host.replaceChildren(shell);
  const tree = shell.querySelector('.agfiles-tree'), detail = shell.querySelector('.agfiles-detail'), crumbs = shell.querySelector('.agfiles-crumbs');
  let viewerPath = null;
  const live = () => !s.closed && shell.isConnected;
  const parentOf = path => path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  const directoryEntry = path => ({path, name:path.split('/').pop() || 'Operations', kind:'directory'});
  const projectRow = entry => parentOf(entry.path) === 'projects' && entry.kind === 'directory';
  const available = entry => ['directory', 'file'].includes(entry.kind);
  const label = entry => entry.path === 'projects' ? 'Projects' : entry.path === 'agents' ? 'Agent workspaces' : entry.name;
  const errorText = error => error?.message || 'This folder could not be loaded.';

  async function load(path, more = false) {
    let dir = s.dirs.get(path);
    if (dir?.loading || (!more && dir?.loaded)) return;
    dir ||= {entries:[]};
    s.dirs.set(path, dir);
    dir.loading = true; dir.error = null;
    const generation = s.generation, controller = new AbortController();
    s.controllers.add(controller);
    paint();
    try {
      const params = new URLSearchParams({path, hidden:s.hidden ? '1' : '0'});
      if (more && dir.next_cursor) params.set('cursor', dir.next_cursor);
      const response = await fetch(`/api/root/files/directory?${params}`, {cache:'no-store', signal:controller.signal, headers:{Accept:'application/json'}});
      const body = await response.json();
      if (!response.ok || body.status !== 'ok') throw new Error(body.error?.message || 'This folder could not be loaded.');
      if (!live() || generation !== s.generation) return;
      const data = body.data;
      const entries = more ? [...new Map([...dir.entries, ...data.entries].map(entry => [entry.path, entry])).values()] : data.entries;
      Object.assign(dir, data, {entries, loaded:true});
    } catch (error) {
      if (live() && generation === s.generation && error.name !== 'AbortError') dir.error = errorText(error);
    } finally {
      s.controllers.delete(controller);
      if (live() && generation === s.generation) { dir.loading = false; paint(); }
    }
  }

  function row(entry, level, flat = false) {
    const folder = entry.kind === 'directory', expanded = s.expanded.has(entry.path), blocked = !available(entry);
    const badge = projectRow(entry) ? agentFilesMetrics(entry.project) : '';
    const title = blocked ? `${entry.path} — ${entry.kind === 'symlink' ? 'symbolic links are not followed' : 'special files cannot be opened'}` : entry.path;
    return `<div class="agfiles-row${s.selected.path === entry.path ? ' selected' : ''}${blocked ? ' unavailable' : ''}" role="treeitem" aria-level="${level + 1}" aria-selected="${s.selected.path === entry.path}" ${folder ? `aria-expanded="${expanded}"` : ''} ${blocked ? 'aria-disabled="true"' : ''} tabindex="-1" data-row="${esc(entry.path)}" style="--depth:${flat ? 0 : level}" title="${esc(title)}">
      ${folder && !flat ? `<button type="button" tabindex="-1" class="agfiles-toggle${expanded ? ' expanded' : ''}" data-toggle="${esc(entry.path)}" aria-label="${expanded ? 'Collapse' : 'Expand'} ${esc(entry.name)}">${agentFilesIcon('chevron')}</button>` : '<span class="agfiles-indent"></span>'}
      <button type="button" tabindex="-1" class="agfiles-name" data-select="${esc(entry.path)}" ${blocked ? 'disabled' : ''}>${agentFilesEntryIcon(entry)}<span>${esc(label(entry))}${flat ? `<small>${esc(parentOf(entry.path) || 'Operations')}</small>` : ''}</span></button>${badge}</div>`;
  }
  function branch(path, depth) {
    const dir = s.dirs.get(path);
    if (!dir) return '';
    let html = dir.entries.map(entry => row(entry, depth) + (entry.kind === 'directory' && s.expanded.has(entry.path) ? `<div role="group">${branch(entry.path, depth + 1)}</div>` : '')).join('');
    if (dir.loading) html += '<div class="agfiles-status" role="status">Loading…</div>';
    if (dir.error) html += `<div class="agfiles-status" role="alert">${esc(dir.error)} <button type="button" data-retry="${esc(path)}">Try again</button></div>`;
    if (dir.loaded && !dir.entries.length && !dir.loading) html += '<div class="agfiles-status">This folder is empty.</div>';
    if (dir.next_cursor) html += `<button type="button" class="agfiles-more" data-more="${esc(path)}" ${dir.loading ? 'disabled' : ''}>Show more (${dir.entries.length} of ${dir.total})</button>`;
    if (dir.skipped) html += `<div class="agfiles-status">${dir.skipped} unsupported file name(s) omitted.</div>`;
    return html;
  }
  function findEntry(path) {
    for (const dir of s.dirs.values()) {
      const found = dir.entries.find(entry => entry.path === path);
      if (found) return found;
    }
    return directoryEntry(path);
  }
  function paintTree() {
    const focused = tree.contains(document.activeElement) ? document.activeElement.closest('[data-row]')?.dataset.row : null;
    const scroll = tree.scrollTop;
    if (s.filter) {
      const entries = [...new Map([...s.dirs.values()].flatMap(dir => dir.entries).map(entry => [entry.path, entry])).values()];
      const matches = entries.filter(entry => `${entry.name} ${entry.path} ${entry.project?.name || ''} ${entry.project?.description || ''}`.toLocaleLowerCase().includes(s.filter));
      tree.innerHTML = matches.length ? matches.map(entry => row(entry, 0, true)).join('') : '<div class="agfiles-status">No matches in loaded folders. Expand a folder to include its files.</div>';
    } else tree.innerHTML = branch('', 0);
    const rows = [...tree.querySelectorAll('[data-row]')];
    const current = rows.find(node => node.dataset.row === (focused || s.selected.path)) || rows[0];
    if (current) { current.tabIndex = 0; if (focused) current.focus({preventScroll:true}); }
    tree.scrollTop = scroll;
  }
  function paintCrumbs() {
    const parts = s.selected.path.split('/').filter(Boolean);
    crumbs.innerHTML = `<button type="button" data-folder="">Operations</button>${parts.map((part, index) => `${agentFilesIcon('chevron')}<button type="button" data-folder="${esc(parts.slice(0, index + 1).join('/'))}" ${index === parts.length - 1 ? 'disabled aria-current="location"' : ''}>${esc(part)}</button>`).join('')}`;
  }
  function paintDetail() {
    const entry = s.selected;
    if (entry.kind === 'file') {
      if (viewerPath === entry.path) return;
      viewerPath = entry.path;
      const url = `/files/view?${new URLSearchParams({path:entry.path})}`;
      detail.innerHTML = `<div class="agfiles-file-tools"><button type="button" data-copy>${agentFilesIcon('copy')} Copy path</button><a href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="Open file viewer" aria-label="Open file viewer">${agentFilesIcon('external')}</a></div>`;
      const frame = document.createElement('iframe');
      frame.className = 'agfiles-viewer'; frame.title = `Preview: ${entry.name}`;
      frame.referrerPolicy = 'no-referrer'; frame.src = url + '#explorer';
      detail.append(frame);
      return;
    }
    viewerPath = null;
    const dir = s.dirs.get(entry.path), project = dir?.project || findEntry(entry.path)?.project;
    const description = project?.description || (entry.path === 'projects' ? 'Project folders, notes, plans and code.' : entry.path === 'agents' ? 'Working files and notes from your agents.' : entry.path === '' ? 'The files your agents work with, organized by folder.' : 'Select a file to read its contents.');
    detail.innerHTML = `<div class="agfiles-folder-head"><div class="agfiles-folder-icon">${agentFilesEntryIcon(entry)}</div><h2>${esc(projectRow(entry) && project ? project.name : label(entry))}</h2><p>${esc(description)}</p>${projectRow(entry) ? agentFilesMetrics(project) : ''}<div class="agfiles-folder-path">${esc(entry.path || 'operations/')}</div></div>
      <div class="agfiles-folder-items"><div class="agfiles-section-title">In this folder <span>${dir?.total ?? ''}</span></div>
      ${(dir?.entries || []).map(child => `<button type="button" class="agfiles-child" data-select="${esc(child.path)}" ${available(child) ? '' : 'disabled'}>${agentFilesEntryIcon(child)}<span>${esc(child.name)}</span><small>${child.kind === 'file' ? agentFilesSize(child.size) : child.kind === 'directory' ? 'Folder' : child.kind}</small></button>`).join('')}
      ${dir?.loading ? '<p class="agfiles-status" role="status">Loading…</p>' : ''}${dir?.error ? `<p class="agfiles-status" role="alert">${esc(dir.error)} <button type="button" data-retry="${esc(entry.path)}">Try again</button></p>` : ''}
      ${dir?.loaded && !dir.entries.length ? '<p class="agfiles-status">This folder is empty.</p>' : ''}
      ${dir?.next_cursor ? `<button type="button" class="agfiles-more" data-more="${esc(entry.path)}" ${dir.loading ? 'disabled' : ''}>Show more (${dir.entries.length} of ${dir.total})</button>` : ''}</div>`;
  }
  function paint() { if (live()) { paintTree(); paintCrumbs(); paintDetail(); } }
  function select(path) {
    const entry = findEntry(path);
    if (!available(entry)) return;
    s.selected = entry;
    for (let parent = parentOf(path); parent; parent = parentOf(parent)) s.expanded.add(parent);
    if (entry.kind === 'directory') { s.expanded.add(path); load(path); }
    paint();
  }
  function toggle(path) {
    if (s.expanded.has(path)) s.expanded.delete(path);
    else { s.expanded.add(path); load(path); }
    paintTree();
  }
  async function refresh() {
    const generation = ++s.generation;
    for (const controller of s.controllers) controller.abort();
    s.dirs.clear(); viewerPath = null;
    const paths = new Set(['', ...s.expanded, s.selected.kind === 'directory' ? s.selected.path : parentOf(s.selected.path)]);
    // Bound concurrent filesystem reads when refreshing a large expanded tree.
    const queue = [...paths];
    await Promise.all(Array.from({length:Math.min(4, queue.length)}, async () => {
      while (queue.length && live() && s.generation === generation) await load(queue.shift());
    }));
  }
  shell.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (button.hasAttribute('data-toggle')) toggle(button.dataset.toggle);
    else if (button.hasAttribute('data-select')) select(button.dataset.select);
    else if (button.hasAttribute('data-folder')) select(button.dataset.folder);
    else if (button.hasAttribute('data-more')) load(button.dataset.more, true);
    else if (button.hasAttribute('data-retry')) { const dir = s.dirs.get(button.dataset.retry); if (dir) dir.loaded = false; load(button.dataset.retry, !!dir?.next_cursor); }
    else if (button.hasAttribute('data-refresh')) { button.disabled = true; try { await refresh(); } finally { button.disabled = false; } }
    else if (button.hasAttribute('data-copy')) {
      try { await navigator.clipboard.writeText(s.selected.path); toast('File path copied.'); }
      catch (_) { toast('The path could not be copied.'); }
    }
  });
  tree.addEventListener('keydown', event => {
    const current = event.target.closest('[data-row]');
    if (!current) return;
    const rows = [...tree.querySelectorAll('[data-row]')], index = rows.indexOf(current), path = current.dataset.row;
    let next;
    if (event.key === 'ArrowDown') next = rows[Math.min(index + 1, rows.length - 1)];
    else if (event.key === 'ArrowUp') next = rows[Math.max(index - 1, 0)];
    else if (event.key === 'Home') next = rows[0];
    else if (event.key === 'End') next = rows.at(-1);
    else if (event.key === 'ArrowRight' && findEntry(path).kind === 'directory') {
      if (!s.expanded.has(path)) toggle(path); else next = rows[index + 1];
    } else if (event.key === 'ArrowLeft') {
      if (s.expanded.has(path)) toggle(path); else next = rows.find(node => node.dataset.row === parentOf(path));
    } else if (event.key === 'Enter' || event.key === ' ') select(path);
    else return;
    event.preventDefault();
    if (next) { rows.forEach(node => node.tabIndex = -1); next.tabIndex = 0; next.focus(); }
  });
  shell.querySelector('input[type=search]').addEventListener('input', event => { s.filter = event.target.value.trim().toLocaleLowerCase(); paintTree(); });
  shell.querySelector('[data-hidden]').addEventListener('change', event => { s.hidden = event.target.checked; refresh(); });
  p.workspaceDispose = () => {
    p.filesState = {selected:s.selected, expanded:[...s.expanded], hidden:s.hidden};
    s.closed = true;
    host.classList.remove('agfiles-host');
    for (const controller of s.controllers) controller.abort();
  };
  refresh();
}
