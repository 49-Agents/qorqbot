// One small shared renderer for ordinary links in written prose (conversation messages and task progress notes). It recognises web/viewer links, absolute projects file references and bare http(s)
// URLs and escapes everything else, so text can never become markup. Returns an HTML string; newlines and the
// surrounding text are untouched. Links open in a new tab and their clicks never reach a row, summary or composer.
const SAFE_LINK_RE = /!?\[((?:\\.|[^\[\]\\\n]){1,300})\]\((<[^<>\n]{1,4096}>|[^\s()<>\\]{1,4096})\)|(https?:\/\/[^\s<>"'`\\]+)/giu;
const SAFE_LINK_ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const safeLinkEsc = text => String(text ?? "").replace(/[&<>"']/g, char => SAFE_LINK_ESCAPE[char]);

// Absolute file references are routed to the owner's existing reader, which checks the configured projects root.
// They never become file:// links or requests against a path on the owner's browser machine.
function safeFileReference(raw) {
  let path = raw;
  try { path = decodeURIComponent(path); } catch (_) { /* A literal percent in a filename is encoded below. */ }
  if (!/^\/(?:[^/]+\/)*(?:projects\/.+|operations\/agents\/[^/]+\/downloads\/.+)/.test(path) || /[\u0000-\u001f\u007f-\u009f\\]/u.test(path)
    || path.split('/').includes('..')) return null;
  return `/files/view?path=${encodeURIComponent(path)}`;
}

// The one-time record link: /?open_task=<task id>#/global/agents finds the Task from its id; /?open_task=<task id>&scope=global#/global/agents
// and /?open_task=<task id>&project_id=<uuid>#/global/agents name where it is. Returns {taskId, projectId} (projectId null = Global),
// {taskId} for an id alone, {invalid: true} when the route is present but malformed, or null when `open_task` is absent.
// It validates only; nothing is read here.
// Projects and Tables have UUIDs. A Task id has no fixed shape (new ones are five characters, older ones UUIDs or slugs),
// so it is held only to the length and character rules of a stored id.
const SAFE_LINK_RECORD_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// The read-only table route: /tables/view?table_id=<id>[&project_id=<id>]. Returns {tableId, projectId} for a stated project and
// {tableId} for an id alone, which finds the table from its id; repeated, unknown, or malformed parameters are invalid.
function tableViewRoute(search) {
  const params = new URLSearchParams(search), keys = [...params.keys()];
  const count = name => keys.filter(key => key === name).length;
  const tableId = params.get('table_id'), projectId = params.get('project_id');
  if (count('table_id') !== 1 || count('project_id') > 1 || keys.some(key => !['table_id', 'project_id'].includes(key))
    || !SAFE_LINK_RECORD_UUID.test(tableId || '') || (count('project_id') === 1 && !SAFE_LINK_RECORD_UUID.test(projectId || ''))) return null;
  return count('project_id') ? {tableId, projectId} : {tableId};
}
function openTaskRoute(search) {
  const params = new URLSearchParams(search), keys = [...params.keys()];
  if (!keys.includes('open_task')) return null;
  const count = name => keys.filter(key => key === name).length;
  const taskId = params.get('open_task');
  if (count('open_task') !== 1 || keys.some(key => !['open_task', 'scope', 'project_id'].includes(key)) || count('scope') + count('project_id') > 1
    || !taskId || !taskId.trim() || taskId.length > 200 || /[\u0000-\u001f\u007f-\u009f]/u.test(taskId)) return {invalid: true};
  if (!count('scope') && !count('project_id')) return {taskId};
  if (count('scope')) return params.get('scope') === 'global' ? {taskId, projectId: null} : {invalid: true};
  const projectId = params.get('project_id');
  return SAFE_LINK_RECORD_UUID.test(projectId) ? {taskId, projectId} : {invalid: true};
}
// The same-origin href for a Task in a stated scope, or null: Global is explicit, a Project needs its UUID.
function openTaskHref(taskId, projectId) {
  const url = new URL('/', 'http://local.invalid');
  url.searchParams.set('open_task', String(taskId ?? ''));
  if (projectId == null) url.searchParams.set('scope', 'global'); else url.searchParams.set('project_id', String(projectId));
  const href = `/${url.search}#/global/agents`;
  const route = openTaskRoute(url.search);
  return route && !route.invalid ? href : null;
}

// The href a link may carry, or null: http(s) with a host and no credentials, or an in-app file/table viewer link.
// javascript:, data:, file:, protocol-relative and every other form is refused.
function safeLinkUrl(raw) {
  const input = String(raw ?? ""), text = input.startsWith('<') && input.endsWith('>') ? input.slice(1, -1) : input;
  if (!text || text.length > 4096 || /[\u0000-\u001f\u007f-\u009f\\]/u.test(text)) return null;
  const file = safeFileReference(text);
  if (file) return file;
  if (/\s/u.test(text)) return null;
  if (text.startsWith("/")) {
    if (/^\/\?open_task=/.test(text)) {
      // Only the exact, validated record route: root path, its three allowed keys and the fixed workspace hash.
      const [path, hash] = text.split('#');
      return hash === '/global/agents' && text.split('#').length === 2 && openTaskRoute(path.slice(1)) && !openTaskRoute(path.slice(1)).invalid ? text : null;
    }
    return /^\/(?:files|tables)\/view\?[A-Za-z0-9_.~%=&\/+-]+$/.test(text) ? text : null;
  }
  if (!/^https?:\/\/[^\/?#]/i.test(text)) return null;
  try {
    const url = new URL(text);
    return (url.protocol === "http:" || url.protocol === "https:") && url.hostname && !url.username && !url.password ? url.href : null;
  } catch (error) { return null; }
}

// The raster source for a `![caption](path)` whose safe href is an in-app file viewer link to a PNG/JPEG/GIF/WEBP; otherwise null.
// External image URLs are never loaded: they stay ordinary links.
function safeImageSrc(href) {
  const match = /^\/files\/view(\?[^#]*)$/.exec(href);
  if (!match) return null;
  let path = '';
  try { path = new URLSearchParams(match[1]).get('path') || ''; } catch (_) { return null; }
  return /\.(?:png|jpe?g|gif|webp)$/i.test(path) ? `/api/root/files/image${match[1]}` : null;
}

function renderSafeLinks(text) {
  const source = String(text ?? "");
  let html = "", last = 0;
  for (const match of source.matchAll(SAFE_LINK_RE)) {
    const [whole, label, target, bare] = match;
    const shown = bare ? bare.replace(/[.,;:!?)\]}]+$/, "") : label.replace(/\\([\\\[\]`*_<>])/g, "$1");
    const href = safeLinkUrl(bare ? shown : target);
    if (!href) continue;
    const image = !bare && whole.startsWith('!') ? safeImageSrc(href) : null;
    html += safeLinkEsc(source.slice(last, match.index)) + (image
      ? `<a class="safe-link safe-image-link" href="${safeLinkEsc(href)}" target="_blank" rel="noopener noreferrer"><img class="safe-image" src="${safeLinkEsc(image)}" alt="${safeLinkEsc(shown)}" loading="lazy" decoding="async" referrerpolicy="no-referrer"></a>`
      : `<a class="safe-link" href="${safeLinkEsc(href)}" target="_blank" rel="noopener noreferrer">${safeLinkEsc(shown)}</a>`);
    last = match.index + (bare ? shown.length : whole.length);
  }
  return html + safeLinkEsc(source.slice(last));
}

// The same text with each safe Markdown link reduced to its label, for a one-line preview that cannot hold a link.
function plainSafeLinks(text) {
  return String(text ?? "").replace(/!?\[((?:\\.|[^\[\]\\\n]){1,300})\]\((<[^<>\n]{1,4096}>|[^\s()<>\\]{1,4096})\)/gu,
    (whole, label, target) => safeLinkUrl(target) ? label.replace(/\\([\\\[\]`*_<>])/g, "$1") : whole);
}

// A click or Enter/Space on a safe link belongs to the link alone: stop it before any row, summary or editor handler.
// The browser's own default (opening the link) is left alone.
(() => {
  const onLink = event => event.target instanceof Element && event.target.closest("a.safe-link");
  for (const type of ["click", "auxclick", "dblclick"]) document.addEventListener(type, event => { if (onLink(event)) event.stopPropagation(); }, true);
  for (const type of ["keydown", "keyup", "keypress"]) document.addEventListener(type, event => {
    if ((event.key === "Enter" || event.key === " ") && onLink(event)) event.stopPropagation();
  }, true);
})();

// A picture that cannot load becomes its caption as an ordinary link to the same file.
document.addEventListener("error", event => {
  const img = event.target;
  if (!(img instanceof HTMLImageElement) || !img.classList.contains("safe-image")) return;
  const link = img.closest("a.safe-image-link");
  if (!link) return;
  link.classList.remove("safe-image-link");
  link.textContent = img.alt;
}, true);
