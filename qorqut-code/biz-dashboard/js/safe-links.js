// One small shared renderer for ordinary links in written prose (conversation messages and task progress notes). It recognises web/viewer links, absolute businesses file references and bare http(s)
// URLs and escapes everything else, so text can never become markup. Returns an HTML string; newlines and the
// surrounding text are untouched. Links open in a new tab and their clicks never reach a row, summary or composer.
const SAFE_LINK_RE = /!?\[((?:\\.|[^\[\]\\\n]){1,300})\]\((<[^<>\n]{1,4096}>|[^\s()<>\\]{1,4096})\)|(https?:\/\/[^\s<>"'`\\]+)/giu;
const SAFE_LINK_ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const safeLinkEsc = text => String(text ?? "").replace(/[&<>"']/g, char => SAFE_LINK_ESCAPE[char]);

// Absolute file references are routed to the owner's existing reader, which checks the configured businesses root.
// They never become file:// links or requests against a path on the owner's browser machine.
function safeFileReference(raw) {
  let path = raw;
  try { path = decodeURIComponent(path); } catch (_) { /* A literal percent in a filename is encoded below. */ }
  if (!/^\/(?:[^/]+\/)*businesses\/.+/.test(path) || /[\u0000-\u001f\u007f-\u009f\\]/u.test(path)
    || path.split('/').includes('..')) return null;
  return `/files/view?path=${encodeURIComponent(path)}`;
}

// The href a link may carry, or null: http(s) with a host and no credentials, or an in-app file/table viewer link.
// javascript:, data:, file:, protocol-relative and every other form is refused.
function safeLinkUrl(raw) {
  const input = String(raw ?? ""), text = input.startsWith('<') && input.endsWith('>') ? input.slice(1, -1) : input;
  if (!text || text.length > 4096 || /[\u0000-\u001f\u007f-\u009f\\]/u.test(text)) return null;
  const file = safeFileReference(text);
  if (file) return file;
  if (/\s/u.test(text)) return null;
  if (text.startsWith("/")) return /^\/(?:files|tables)\/view\?[A-Za-z0-9_.~%=&\/+-]+$/.test(text) ? text : null;
  if (!/^https?:\/\/[^\/?#]/i.test(text)) return null;
  try {
    const url = new URL(text);
    return (url.protocol === "http:" || url.protocol === "https:") && url.hostname && !url.username && !url.password ? url.href : null;
  } catch (error) { return null; }
}

function renderSafeLinks(text) {
  const source = String(text ?? "");
  let html = "", last = 0;
  for (const match of source.matchAll(SAFE_LINK_RE)) {
    const [whole, label, target, bare] = match;
    const shown = bare ? bare.replace(/[.,;:!?)\]}]+$/, "") : label.replace(/\\([\\\[\]`*_<>])/g, "$1");
    const href = safeLinkUrl(bare ? shown : target);
    if (!href) continue;
    html += safeLinkEsc(source.slice(last, match.index))
      + `<a class="safe-link" href="${safeLinkEsc(href)}" target="_blank" rel="noopener noreferrer">${safeLinkEsc(shown)}</a>`;
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
