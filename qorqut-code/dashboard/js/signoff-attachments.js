// Signoff attachments: the files, tables and Task records a Signoff's own text links to, shown inside the Signoff under its text
// (the way a report to read shows its text) instead of only as links to a new tab. References come from the rendered safe links
// of the explanation and rationale alone (never an email body or a change preview), in order, once each, at most eight. Web
// links stay links; pictures already shown inline are skipped. Each section reads on its own through the same owner routes the
// standalone viewers use, and a failure stays inside its section. Every file, table and record value is untrusted text: it is
// set with textContent, except a Markdown file's server HTML, which is rebuilt through the allowlist below (the file viewer's).
const SG_ATTACH_MAX = 8, SG_ATTACH_ROWS = 50;
const SG_ATTACH_KIND = {file: 'File', table: 'Table', task: 'Record'};

function sgAttachNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// The in-app references among a Signoff's rendered explanation/rationale links: [{href, kind, label, route}].
function sgAttachmentRefs(root) {
  const refs = [], seen = new Set();
  for (const link of root ? root.querySelectorAll('.sg-report a.safe-link, .sg-rationale a.safe-link') : []) {
    if (link.classList.contains('safe-image-link') || link.closest('.sg-attachments')) continue;
    // The href is renderSafeLinks' own validated output; each route parser below checks it again. It is not passed through
    // safeLinkUrl a second time, which would wrap a viewer link to a projects path into another viewer link.
    const href = link.getAttribute('href') || '';
    if (!href.startsWith('/') || href.startsWith('//') || seen.has(href)) continue;
    const url = new URL(href, location.origin);
    let ref = null;
    if (url.pathname === '/files/view' && url.searchParams.get('path')) ref = {kind: 'file', route: {search: url.search}};
    else if (url.pathname === '/tables/view') { const route = tableViewRoute(url.search); if (route) ref = {kind: 'table', route}; }
    else if (url.pathname === '/' && url.searchParams.has('open_task')) { const route = openTaskRoute(url.search); if (route && !route.invalid) ref = {kind: 'task', route}; }
    if (!ref) continue;
    seen.add(href);
    refs.push({...ref, href, label: link.textContent.trim() || SG_ATTACH_KIND[ref.kind]});
    if (refs.length >= SG_ATTACH_MAX) break;
  }
  return refs;
}

// ---- reads
const sgAttachDenied = error => [401, 403].includes(error?.httpStatus ?? error?.status) || ['forbidden', 'policy_denied', 'invalid_principal'].includes(error?.code);
const sgAttachFailure = (error, what) => sgAttachDenied(error) ? 'Only the owner can open this.' : `This ${what} could not be loaded.`;
async function sgAttachFile(action, search) {
  const response = await fetch(`/api/root/files/${action}${search}`, {headers: {Accept: 'application/json'}, cache: 'no-store'});
  let body = null;
  try { body = await response.json(); } catch (_) { body = null; }
  if (!response.ok || !body || body.status !== 'ok' || !body.data) throw Object.assign(new Error('failed'), {status: response.status, code: body?.error?.code});
  return body.data;
}
// Project operations route by slug; a reference with an id alone is read from the root route, which finds its project.
// A stated Project that is not known here is unavailable; it never falls back to another scope.
function sgAttachScope(projectId) {
  if (projectId === undefined) return {path: '/root/operations/', extra: {}};
  if (projectId === null) return {path: '/root/operations/', extra: {scope: 'global'}};
  const project = (typeof state !== 'undefined' ? state.projects || [] : []).find(b => b.id === projectId || b.project_id === projectId);
  if (!project?.slug) throw Object.assign(new Error('unavailable'), {code: 'not_found'});
  return {path: `/project/${encodeURIComponent(project.slug)}/operations/`, extra: {}, slug: project.slug};
}
const sgAttachCall = async (scope, op, args) => coreData(await post(scope.path + op, {...scope.extra, ...args}));

// ---- Markdown: server HTML rebuilt through the file viewer's allowlist (js/file-viewer.js)
const SG_ATTACH_MD_TAGS = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'em', 'strong', 's',
  'hr', 'br', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'details', 'summary', 'span', 'a']);
const SG_ATTACH_MD_DROP = new Set(['script', 'style', 'template', 'noscript', 'iframe', 'object', 'embed', 'svg', 'math', 'form', 'textarea']);
function sgAttachHref(href) {
  try {
    const url = new URL(href, location.origin);
    if (url.origin === location.origin) return url.pathname === '/files/view' && url.search ? url.pathname + url.search : null;
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : null;
  } catch (_) { return null; }
}
function sgAttachRebuild(target, source) {
  for (const node of source.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) { target.append(document.createTextNode(node.data)); continue; }
    if (node.nodeType !== Node.ELEMENT_NODE || SG_ATTACH_MD_DROP.has(node.localName)) continue;
    if (!SG_ATTACH_MD_TAGS.has(node.localName)) { sgAttachRebuild(target, node); continue; }
    let tag = node.localName, href = null;
    if (tag === 'a') { href = sgAttachHref(node.getAttribute('href') || ''); if (!href) tag = 'span'; }
    const copy = document.createElement(tag);
    if (href) { copy.setAttribute('href', href); copy.setAttribute('target', '_blank'); copy.setAttribute('rel', 'noopener noreferrer'); }
    sgAttachRebuild(copy, node);
    target.append(copy);
  }
}
function sgAttachMarkdown(html) {
  const article = sgAttachNode('div', 'sg-report sg-attachment-md');
  sgAttachRebuild(article, new DOMParser().parseFromString(String(html), 'text/html').body);
  return article;
}
const sgAttachCell = value => value == null ? '' : Array.isArray(value) ? value.map(sgAttachCell).join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
function sgAttachTable(head, rows) {
  const wrap = sgAttachNode('div', 'sg-attachment-table'), table = document.createElement('table');
  wrap.tabIndex = 0; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', 'Table preview');
  const tr = (cells, tag) => { const row = document.createElement('tr'); for (const cell of cells) { const c = sgAttachNode(tag, '', sgAttachCell(cell)); if (tag === 'th') c.scope = 'col'; row.append(c); } return row; };
  if (head) { const thead = document.createElement('thead'); thead.append(tr(head, 'th')); table.append(thead); }
  const tbody = document.createElement('tbody'); rows.forEach(cells => tbody.append(tr(cells, 'td'))); table.append(tbody);
  wrap.append(table);
  return wrap;
}

// ---- content per kind; each returns the nodes for the section body and sets its subtitle
async function sgAttachFileBody(ref, section) {
  const meta = await sgAttachFile('metadata', ref.route.search);
  section.setSubtitle(typeof meta.name === 'string' && meta.name !== ref.label ? meta.name : '');
  if (meta.preview_kind === 'image') {
    const src = safeImageSrc(ref.href);
    if (!src) return [sgAttachNode('p', 'sg-notice', 'This file cannot be shown here.')];
    const img = sgAttachNode('img', 'sg-attachment-image');
    img.alt = ref.label; img.decoding = 'async'; img.referrerPolicy = 'no-referrer'; img.src = src;
    img.onerror = () => img.replaceWith(sgAttachNode('p', 'sg-notice', 'This image could not be displayed.'));
    return [img];
  }
  // A PDF shows in the browser's own viewer, from the route that serves only files whose header says PDF.
  if (meta.preview_kind === 'pdf' && typeof meta.urls?.pdf === 'string' && meta.urls.pdf.startsWith('/api/root/files/pdf?')) {
    const frame = sgAttachNode('iframe', 'sg-attachment-pdf');
    frame.title = ref.label; frame.src = meta.urls.pdf;
    section.setTall?.();
    return [frame];
  }
  if (meta.binary) return [sgAttachNode('p', 'sg-notice', 'This file cannot be shown here.')];
  const payload = await sgAttachFile('content', ref.route.search), out = [];
  if (payload.truncated) out.push(sgAttachNode('p', 'sg-notice', 'Only the first part of this file is shown.'));
  if (payload.kind === 'markdown' && payload.html != null) out.push(sgAttachMarkdown(payload.html));
  else if (payload.kind === 'csv' && Array.isArray(payload.rows)) { const [head, ...rows] = payload.rows; out.push(sgAttachTable(head, rows)); }
  else if (['markdown', 'text'].includes(payload.kind) && typeof payload.text === 'string') out.push(sgAttachNode('pre', 'sg-attachment-text', payload.text));
  else return [sgAttachNode('p', 'sg-notice', 'This file cannot be shown here.')];
  return out;
}
async function sgAttachTableBody(ref, section) {
  const scope = sgAttachScope(ref.route.projectId);
  const table = (await sgAttachCall(scope, 'tables_get', {table_id: ref.route.tableId})).table;
  if (!table || !Array.isArray(table.columns)) throw new Error('invalid');
  const crm = table.table_kind === 'crm';
  section.setSubtitle(`${crm ? 'CRM · ' : ''}${typeof table.title === 'string' && table.title !== ref.label ? table.title : ''}`.replace(/ · $/, ''));
  const page = await sgAttachCall(scope, crm ? 'crm_entries_list' : 'tables_rows_list', {table_id: table.id || ref.route.tableId, limit: SG_ATTACH_ROWS,
    ...(crm ? {include_archived: false, include_removed: false} : {})});
  if (!Array.isArray(page.items)) throw new Error('invalid');
  if (!page.items.length) return [sgAttachNode('p', 'sg-notice', 'This table has no rows.')];
  const value = (row, column) => row.cells && Object.hasOwn(row.cells, column.id) ? row.cells[column.id] : null;
  const stages = new Map((table.crm_config?.stages || []).map(stage => [stage.id, stage.name]));
  const head = crm ? ['Record', ...table.columns.map(c => c.name), 'Stage', 'Task state'] : table.columns.map(c => c.name);
  const rows = page.items.map(row => !crm ? table.columns.map(c => value(row, c))
    : row.unavailable || !row.record ? ['Unavailable record', ...table.columns.map(() => ''), '', '']
    : [row.record.title || row.record.name || row.record_ref?.id, ...table.columns.map(c => value(row, c)), stages.get(row.stage_id) || '', row.record.status]);
  const out = [sgAttachTable(head, rows)], total = Number.isInteger(table.row_count) ? table.row_count : null;
  if (page.next_cursor || (total != null && total > rows.length)) {
    const more = sgAttachNode('p', 'sg-notice sg-attachment-more', total != null && total > rows.length ? `${total - rows.length} more rows — ` : 'More rows — ');
    const open = sgAttachNode('button', 'sg-command', 'Open'); open.type = 'button'; open.onclick = () => section.open();
    more.append(open); out.push(more);
  }
  return out;
}
async function sgAttachTaskBody(ref, section) {
  const scope = sgAttachScope(ref.route.projectId);
  const task = (await sgAttachCall(scope, 'tasks_get', {task_id: ref.route.taskId})).task;
  if (!task || String(task.id) !== String(ref.route.taskId)) throw Object.assign(new Error('missing'), {code: 'not_found'});
  section.record = {task, slug: scope.slug || null};
  section.setSubtitle(typeof task.title === 'string' && task.title !== ref.label ? task.title : '');
  const fields = sgAttachNode('dl', 'sg-attachment-fields');
  const field = (label, value) => { const text = sgAttachCell(value).trim(); if (!text) return; fields.append(sgAttachNode('dt', '', label), sgAttachNode('dd', '', text)); };
  field('Title', task.title); field('Task type', task.task_type); field('Task state', task.status);
  const profile = task.customer_profile && typeof task.customer_profile === 'object' ? task.customer_profile : {};
  for (const [key, label] of [['company', 'Company'], ['contact_name', 'Contact name'], ['email', 'Email'], ['phone', 'Phone'], ['website', 'Website']]) field(label, profile[key]);
  const out = [fields];
  if (typeof task.overview === 'string' && task.overview.trim()) {
    const overview = sgAttachNode('div', 'sg-report sg-attachment-overview');
    if (typeof chatViewMarkdown === 'function') overview.innerHTML = chatViewMarkdown(task.overview); else overview.textContent = task.overview;
    out.push(overview);
  }
  return out;
}

// ---- opening a reference in full
async function sgAttachOpen(ref, section) {
  const fallback = () => { window.open(ref.href, '_blank', 'noopener'); };
  try {
    if (ref.kind === 'file' && typeof agentFilePreviewOpen === 'function' && await agentFilePreviewOpen({href: ref.href, title: ref.label})) return;
    if (ref.kind === 'task' && typeof agentOpenRecordReference === 'function') {
      const task = section.record?.task, projectId = ref.route.projectId !== undefined ? ref.route.projectId : task ? task.project_id ?? null : undefined;
      if (projectId !== undefined && await agentOpenRecordReference({type: 'task', id: ref.route.taskId, project_id: projectId, project_slug: section.record?.slug || undefined,
        label: task?.title, task_type: task?.task_type, state: task?.status, can_thread: true})) return;
    }
  } catch (_) { /* The viewer page still opens it. */ }
  fallback();
}

// One section per reference, each loading on its own. Returns its disposer.
function sgAttachSection(ref) {
  const section = sgAttachNode('section', 'sg-attachment');
  section.dataset.sgAttachment = ref.href; section.dataset.kind = ref.kind; section.tabIndex = -1;
  section.setAttribute('aria-label', `${SG_ATTACH_KIND[ref.kind]}: ${ref.label}`);
  const head = sgAttachNode('header', 'sg-attachment-head'), names = sgAttachNode('div', 'sg-attachment-names');
  const subtitle = sgAttachNode('small', 'sg-attachment-sub');
  names.append(sgAttachNode('strong', 'sg-attachment-title', ref.label), sgAttachNode('span', 'sg-attachment-kind', SG_ATTACH_KIND[ref.kind]), subtitle);
  const open = sgAttachNode('button', 'sg-command sg-attachment-open', 'Open'); open.type = 'button';
  open.setAttribute('aria-label', `Open ${ref.label}`);
  const full = sgAttachNode('button', 'quiet sg-attachment-full-toggle'); full.type = 'button';
  head.append(names, full, open);
  const body = sgAttachNode('div', 'sg-attachment-body');
  body.append(sgAttachNode('p', 'sg-notice', 'Loading…'));
  section.append(head, body);
  const control = {setSubtitle: text => { subtitle.textContent = text ? `· ${text}` : ''; }, setTall: () => body.classList.add('sg-attachment-body-pdf'),
    open: () => sgAttachOpen(ref, control), record: null};
  open.onclick = event => { event.stopPropagation(); void control.open(); };
  sgAttachFullView(section, full, ref.label);
  const reader = ref.kind === 'file' ? sgAttachFileBody : ref.kind === 'table' ? sgAttachTableBody : sgAttachTaskBody;
  const what = ref.kind === 'file' ? 'file' : ref.kind === 'table' ? 'table' : 'record';
  Promise.resolve().then(() => reader(ref, control)).then(nodes => { if (section.isConnected || section.parentNode) body.replaceChildren(...nodes); },
    error => { if (section.isConnected || section.parentNode) body.replaceChildren(sgAttachNode('p', 'sg-notice sg-attachment-error', sgAttachFailure(error, what))); });
  return section;
}

// "View in full": the attachment covers the whole right pane it sits in (the Focus board, else the page), showing only its
// body and one minimize button in the top right corner. Escape or the button brings it back. The cover follows the pane's
// size while open.
const SG_ATTACH_EXPAND = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>';
const SG_ATTACH_SHRINK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>';
function sgAttachFullView(section, button, label) {
  let observer = null;
  const pane = () => section.closest('.agfocus') || section.closest('.tbody') || document.documentElement;
  const place = () => {
    const box = pane().getBoundingClientRect();
    Object.assign(section.style, {top: `${Math.max(0, box.top)}px`, left: `${Math.max(0, box.left)}px`,
      width: `${Math.min(box.width, innerWidth)}px`, height: `${Math.min(box.height, innerHeight - Math.max(0, box.top))}px`});
  };
  const paint = on => {
    button.innerHTML = on ? SG_ATTACH_SHRINK : SG_ATTACH_EXPAND;
    const text = on ? 'Minimize' : `View ${label} in full`;
    button.setAttribute('aria-label', text); button.title = on ? 'Minimize' : 'View in full';
    button.setAttribute('aria-expanded', on ? 'true' : 'false');
  };
  const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); set(false); } };
  function set(on) {
    if (on === section.classList.contains('sg-attachment-full')) return;
    section.classList.toggle('sg-attachment-full', on);
    paint(on);
    if (on) {
      place();
      observer = new ResizeObserver(place); observer.observe(pane());
      addEventListener('resize', place); section.addEventListener('keydown', onKey);
      section.focus({preventScroll: true});
    } else {
      observer?.disconnect(); observer = null;
      removeEventListener('resize', place); section.removeEventListener('keydown', onKey);
      for (const key of ['top', 'left', 'width', 'height']) section.style[key] = '';
      button.focus({preventScroll: true});
    }
  }
  paint(false);
  button.onclick = event => { event.stopPropagation(); set(!section.classList.contains('sg-attachment-full')); };
  // A Signoff that closes or rerenders takes its attachment out of the page; the pane watcher goes with it.
  new MutationObserver((_, watcher) => { if (!section.isConnected && observer) { set(false); watcher.disconnect(); } })
    .observe(document.body, {childList: true, subtree: true});
}

// Mounts (or keeps) the attachments for a Signoff detail: `host` is its attachments area, `source` the rendered preview.
// The same references keep their loaded sections; changed references replace them.
function sgAttachmentsMount(host, source) {
  if (!host) return;
  const refs = sgAttachmentRefs(source), key = refs.map(ref => ref.href).join('\n');
  if (host._sgAttachKey === key) return;
  host._sgAttachKey = key;
  host.replaceChildren();
  host.hidden = !refs.length;
  if (!refs.length) return;
  host.append(sgAttachNode('h3', 'sg-attachments-heading', 'Attachments'), ...refs.map(sgAttachSection));
}
function sgAttachmentsClear(host) { if (host) { host._sgAttachKey = ''; host.replaceChildren(); host.hidden = true; } }

// A plain click on a reference in a Signoff's text goes to its attachment, not a new tab. Modified and middle clicks keep the
// browser's default. It runs before safe-links' document capture, which stops the event from reaching anything below it.
window.addEventListener('click', event => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target instanceof Element ? event.target.closest('.sg-detail a.safe-link') : null;
  if (!link || link.closest('.sg-attachments')) return;
  const href = link.getAttribute('href'), detail = link.closest('.sg-detail');
  const target = href && [...detail.querySelectorAll('.sg-attachments [data-sg-attachment]')].find(node => node.dataset.sgAttachment === href);
  if (!target) return;
  event.preventDefault();
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  target.scrollIntoView({behavior: reduce ? 'auto' : 'smooth', block: 'start'});
  target.focus({preventScroll: true});
  target.classList.remove('sg-attachment-flash'); void target.offsetWidth; target.classList.add('sg-attachment-flash');
  clearTimeout(target._sgFlash); target._sgFlash = setTimeout(() => target.classList.remove('sg-attachment-flash'), 1600);
}, true);
