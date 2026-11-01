// One small shared renderer for ordinary links in written prose (conversation messages and task progress notes,
// story descriptions and timeline notes). It recognises `[label](https://… or /files/view?…)` and bare http(s)
// URLs and escapes everything else, so text can never become markup. Returns an HTML string; newlines and the
// surrounding text are untouched. Links open in a new tab and their clicks never reach a row, summary or composer.
const SAFE_LINK_RE = /\[((?:\\.|[^\[\]\\\n]){1,300})\]\(([^\s()<>\\]{1,4096})\)|(https?:\/\/[^\s<>"'`\\]+)/giu;
const SAFE_LINK_ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const safeLinkEsc = text => String(text ?? "").replace(/[&<>"']/g, char => SAFE_LINK_ESCAPE[char]);

// The href a link may carry, or null: http(s) with a host and no credentials, or an in-app file viewer link.
// javascript:, data:, file:, protocol-relative and every other form is refused.
function safeLinkUrl(raw) {
  const text = String(raw ?? "");
  if (!text || text.length > 4096 || /[\u0000- \u007f-\u009f\\]/u.test(text)) return null;
  if (text.startsWith("/")) return /^\/files\/view\?[A-Za-z0-9_.~%=&\/+-]+$/.test(text) ? text : null;
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
  return String(text ?? "").replace(/\[((?:\\.|[^\[\]\\\n]){1,300})\]\(([^\s()<>\\]{1,4096})\)/gu,
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
