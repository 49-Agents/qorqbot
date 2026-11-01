// The standalone owner file viewer (/files/view). It reads one file through the owner-only /api/root/files
// routes and shows it; it never edits, polls, mounts terminals or loads the dashboard. All content reaches the
// DOM through textContent or the allowlist below, never innerHTML.
(() => {
"use strict";
const API = "/api/root/files", PAGE = 20, VIEWER = "/files/view";
const root = document.getElementById("fv"), toastBox = document.getElementById("fv-toast");
const search = location.search, query = new URLSearchParams(search);

function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
}

const ICONS = {
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  copy: 'M9 9h11v11H9zM15 5V3H3v12h2',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12m13 0a3 3 0 1 0-6 0 3 3 0 0 0 6 0',
  source: 'm8 5-7 7 7 7m8-14 7 7-7 7'
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({viewBox:'0 0 24 24', fill:'none', stroke:'currentColor', 'stroke-width':'1.7', 'stroke-linecap':'round', 'stroke-linejoin':'round', 'aria-hidden':'true'})) svg.setAttribute(key, value);
  const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', ICONS[name]); svg.append(path); return svg;
}
function iconButton(name, label, props = {}) {
  return h('button', {class:'fv-btn fv-icon', type:'button', title:label, 'aria-label':label, ...props}, icon(name));
}
function downloadButton(url) {
  return h('a', {class:'fv-btn fv-icon', href:url, download:'', title:'Download', 'aria-label':'Download'}, icon('download'));
}

let toastTimer = null;
function toast(text) {
  toastBox.textContent = text;
  toastBox.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastBox.classList.remove("on"), 2200);
}

const absolute = url => new URL(url, location.origin).href;

async function copyText(text, anchor, message = "Link copied") {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); toast(message); return; }
  } catch (error) { /* fall through to the selection fallback */ }
  // The dashboard is served over plain HTTP on the private network, which is not a secure context.
  const area = h("textarea", { class: "fv-offscreen", readonly: "", "aria-hidden": "true", tabindex: "-1" });
  area.value = text;
  document.body.append(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
  area.remove();
  if (copied) { toast(message); return; }
  const manual = h("textarea", { class: "fv-manual", readonly: "", "aria-label": message === "Link copied" ? "Link to copy" : "Text to copy" });
  manual.value = text;
  (anchor?.closest(".fv-head") || root).append(manual);
  manual.focus();
  manual.select();
  toast("Press Ctrl+C to copy");
}

function fmtSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${unit ? value.toFixed(value < 10 ? 1 : 0) : value} ${units[unit]}`;
}

function viewerUrl(documentId, businessId, version) {
  const params = new URLSearchParams({ document_id: documentId });
  if (businessId) params.set("business_id", businessId);
  if (version) params.set("version", String(version));
  return `${VIEWER}?${params}`;
}

async function call(action, extra) {
  let response;
  try { response = await fetch(`${API}/${action}${extra}`, { headers: { Accept: "application/json" }, cache: "no-store" }); }
  catch (error) { const failure = new Error("The server could not be reached."); failure.code = "network"; throw failure; }
  let body = null;
  try { body = await response.json(); } catch (error) { body = null; }
  if (!response.ok || !body || body.status !== "ok") {
    const failure = new Error((body && body.error && body.error.message) || "The request failed.");
    failure.code = (body && body.error && body.error.code) || "request_failed";
    failure.status = response.status;
    throw failure;
  }
  return body.data;
}

const ERRORS = {
  not_found: ["File not found", "This file is not available. It may have moved or been deleted, or the link may point at something that is not here."],
  symlink_not_followed: ["This file cannot be opened", "Symbolic links are not followed, so this path cannot be shown."],
  not_a_regular_file: ["This is not a file", "Only regular files can be shown. Folders and special files are not opened."],
  file_unreadable: ["This file cannot be read", "The server could not read this file."],
  forbidden: ["Not available", "Only the installation owner can open files here."],
  network: ["Connection problem", "The server could not be reached. Check the connection and try again."],
};
function showError(error, retry) {
  const [title, text] = ERRORS[error.code] || ["This file cannot be shown", error.message || "The request failed."];
  document.title = `${title} · Qorqut`;
  root.replaceChildren(h("section", { class: "fv-error", role: "alert" }, h("h2", {}, title), h("p", {}, text),
    error.code === "network" ? h("button", { class: "fv-btn", type: "button", onclick: retry }, "Try again") : null));
}

// ------------------------------------------------------------ Markdown: server HTML rebuilt through an allowlist
const MD_TAGS = new Set(["p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "code", "em", "strong", "s",
  "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "details", "summary", "span", "a"]);
const MD_DROP = new Set(["script", "style", "template", "noscript", "iframe", "object", "embed", "svg", "math", "form", "textarea"]);

function safeHref(href) {
  try {
    const url = new URL(href, location.origin);
    if (url.origin === location.origin) return url.pathname === VIEWER && url.search ? url.pathname + url.search : null;
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch (error) { return null; }
}

function rebuild(target, source) {
  for (const node of source.childNodes) {
    if (node.nodeType === Node.TEXT_NODE) { target.append(document.createTextNode(node.data)); continue; }
    if (node.nodeType !== Node.ELEMENT_NODE || MD_DROP.has(node.localName)) continue;
    if (!MD_TAGS.has(node.localName)) { rebuild(target, node); continue; }
    let tag = node.localName, href = null;
    if (tag === "a") { href = safeHref(node.getAttribute("href") || ""); if (!href) tag = "span"; }
    const copy = document.createElement(tag);
    if (href) { copy.setAttribute("href", href); copy.setAttribute("target", "_blank"); copy.setAttribute("rel", "noopener noreferrer"); }
    const cls = node.getAttribute("class") || "";
    if ((tag === "code" && /^language-[\w-]+$/.test(cls)) || (tag === "details" && cls === "frontmatter")) copy.className = cls;
    rebuild(copy, node);
    target.append(copy);
  }
}

function markdownNode(html) {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  const article = h("article", { class: "fv-md" });
  rebuild(article, parsed.body);
  return article;
}

// ------------------------------------------------------------ Previews
function textNode(text) { return h("pre", { class: "fv-text wrap", tabindex: "0" }, text); }

function csvNode(payload) {
  const [head, ...rows] = payload.rows;
  const cells = (row, tag) => h("tr", {}, row.map(cell => h(tag, {}, cell)));
  return h("div", { class: "fv-csv", tabindex: "0", role: "region", "aria-label": "CSV preview" },
    h("table", {}, head ? h("thead", {}, cells(head, "th")) : null, h("tbody", {}, rows.map(row => cells(row, "td")))));
}

function frameNode(payload) {
  // No allow-scripts, allow-same-origin, allow-forms or allow-top-navigation: the page is inert, and the
  // content security policy in the prefix blocks script, network, forms and base.
  const frame = h("iframe", { class: "fv-frame", title: "HTML preview (scripts and network disabled)", sandbox: "", referrerpolicy: "no-referrer" });
  frame.setAttribute("srcdoc", payload.frame_prefix + payload.text);
  return frame;
}

// ------------------------------------------------------------ History
function diffNode(text) {
  const pre = h("pre", { class: "fv-text fv-diff", tabindex: "0" });
  // The two "--- / +++" header lines only repeat internal identifiers; the heading above names the versions.
  const lines = text.split("\n");
  for (const line of /^--- /.test(lines[0] || "") && /^\+\+\+ /.test(lines[1] || "") ? lines.slice(2) : lines) {
    const cls = /^(@@|\+\+\+|---)/.test(line) ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
    pre.append(h("span", { class: cls }, line || " "));
  }
  return pre;
}

// ------------------------------------------------------------ The page
const state = { meta: null, payload: null, mode: "rendered", panel: false, history: null, compare: [], diff: null, drift: null };
const parts = {};

function isVersion() { return state.meta && state.meta.current === false; }
function managed() { return state.meta.managed; }
function historyQuery(extra) {
  const params = new URLSearchParams({ document_id: managed().document_id });
  if (managed().business_id) params.set("business_id", managed().business_id);
  for (const [key, value] of Object.entries(extra || {})) params.set(key, String(value));
  return `?${params}`;
}

function segment(label, pressed, onclick) {
  return iconButton(label === "Source" ? "source" : "eye", label, {"aria-pressed": String(pressed), onclick});
}

function renderHead() {
  const { meta } = state, version = isVersion();
  const copyButton = iconButton('link', version ? 'Copy version link' : 'Copy link', {onclick: event => copyText(absolute(meta.urls.copy), event.currentTarget)});
  const textCopy = state.payload && typeof state.payload.text === 'string'
    ? iconButton('copy', state.payload.truncated ? 'Copy preview' : 'Copy text', {onclick: event => copyText(state.payload.text, event.currentTarget, 'Text copied')}) : null;
  parts.head.replaceChildren(
    h("div", { class: "fv-title" }, h("h1", {}, meta.name), h("div", { class: "fv-path" }, meta.path)),
    h("div", { class: "fv-actions" }, copyButton, textCopy,
      downloadButton(meta.urls.download),
      meta.managed && meta.managed.history_available
        ? h("button", { class: "fv-btn", type: "button", "aria-pressed": String(state.panel), "aria-controls": "fv-history", onclick: toggleHistory }, "History") : null));
}

function renderNotices() {
  const { meta, payload } = state, notices = [];
  if (isVersion()) {
    notices.push(h("div", { class: "fv-banner info", role: "note" },
      `This is saved version ${meta.version.history_version}, not the current file. `,
      h("a", { class: "fv-btn", href: meta.urls.current }, "Back to current")));
  }
  if (payload && payload.truncated) {
    notices.push(h("div", { class: "fv-banner warn", role: "note" },
      `Showing the first ${fmtSize(payload.preview_bytes)} of ${fmtSize(payload.size)}. `,
      downloadButton(meta.urls.download)));
  }
  if (payload && payload.kind === "csv") {
    const cuts = [payload.rows_truncated && "rows", payload.columns_truncated && "columns", payload.cells_truncated && "long cells"].filter(Boolean);
    if (cuts.length) notices.push(h("div", { class: "fv-banner warn", role: "note" }, `The table preview is limited (${cuts.join(", ")} shortened). Download for everything.`));
  }
  if (payload && payload.kind === "text" && payload.csv_unparseable) notices.push(h("div", { class: "fv-banner info", role: "note" }, "This file could not be read as a table, so it is shown as text."));
  if (payload && payload.kind === "markdown" && payload.html == null) notices.push(h("div", { class: "fv-banner info", role: "note" }, "This document is too large to render, so its source is shown."));
  parts.notices.replaceChildren(...notices);
}

function renderTools() {
  const payload = state.payload, tools = [];
  if (payload) {
    if (payload.kind === "markdown" && payload.html != null) {
      tools.push(segment("Rendered", state.mode === "rendered", () => setMode("rendered")), segment("Source", state.mode === "source", () => setMode("source")));
    } else if (payload.kind === "csv") {
      tools.push(segment("Table", state.mode === "rendered", () => setMode("rendered")), segment("Source", state.mode === "source", () => setMode("source")));
    } else if (payload.kind === "html") {
      tools.push(segment("Preview", state.mode === "rendered", () => setMode("rendered")), segment("Source", state.mode === "source", () => setMode("source")));
    }
  }
  parts.tools.replaceChildren(...tools);
  parts.tools.hidden = !tools.length;
}

function textShown() {
  const payload = state.payload;
  if (!payload || payload.kind === "binary") return false;
  if (payload.kind === "text") return true;
  return state.mode === "source" || (payload.kind === "markdown" && payload.html == null);
}

function setMode(mode) { state.mode = mode; renderTools(); renderBody(); }

function renderBody() {
  const payload = state.payload;
  if (!payload) { parts.body.replaceChildren(h("p", { class: "fv-loading" }, "Loading…")); return; }
  if (payload.kind === "binary") {
    parts.body.replaceChildren(h("section", { class: "fv-error" }, h("h2", {}, "No preview for this file"),
      h("p", {}, "It is not text, so it is not shown here. Download it to open it elsewhere."),
      downloadButton(state.meta.urls.download)));
    return;
  }
  let node;
  if (textShown()) node = textNode(payload.text);
  else if (payload.kind === "markdown") node = markdownNode(payload.html);
  else if (payload.kind === "csv") node = csvNode(payload);
  else node = frameNode(payload);
  parts.body.replaceChildren(node);
}

// History panel: versions newest first, View, two-version compare, older pages.
async function toggleHistory() {
  state.panel = !state.panel;
  renderHead();
  if (!state.panel) { parts.panel.replaceChildren(); return; }
  renderHistory();
  parts.panel.querySelector("h2")?.focus();
  if (!state.history || state.history.error) { state.history = null; renderHistory(); await loadHistory(null); }
}

async function loadHistory(before) {
  try {
    const data = await call("history", historyQuery(before ? { before_version: before, limit: PAGE } : { limit: PAGE }));
    if (!before) { state.history = { versions: [], next: null, current: data.current_revision }; noteDrift(data); }
    state.history.versions.push(...data.versions);
    state.history.next = data.next_before_version;
    state.history.error = null;
  } catch (error) {
    state.history = state.history || { versions: [], next: null };
    state.history.error = error.message;
  }
  if (state.panel) renderHistory();
}

function noteDrift(data) {
  if (isVersion() || data.current_matches_latest == null) return;
  state.drift = !data.current_matches_latest;
  renderHead();
}

function versionRow(item, latest) {
  const number = item.history_version, viewing = isVersion() && state.meta.version.history_version === number;
  const link = viewerUrl(managed().document_id, managed().business_id, number);
  const when = sjDateTime(item.created_at);
  const checkbox = h("input", { type: "checkbox", checked: state.compare.includes(number), "aria-label": `Select version ${number} to compare`,
    onchange: event => {
      state.compare = event.target.checked ? [...state.compare, number].slice(-2) : state.compare.filter(value => value !== number);
      renderHistory();
    } });
  return h("li", {},
    h("span", { class: "ver" }, `v${number}`),
    h("span", { class: "meta" }, `${when} · ${item.author_name || item.author_id}`,
      latest || viewing ? h("small", {}, [latest ? "Latest saved version" : null, viewing ? "Viewing now" : null].filter(Boolean).join(" · ")) : null),
    h("span", { class: "fv-history-actions" },
      h("label", {}, checkbox, "Compare"),
      viewing ? null : h("a", { class: "fv-btn", href: link }, "View"),
      iconButton('link', 'Copy link', {onclick: event => copyText(absolute(link), event.currentTarget)})));
}

function renderHistory() {
  if (!state.panel) return;
  const hist = state.history, panel = h("section", { class: "fv-history", id: "fv-history", "aria-label": "Saved versions" });
  panel.append(h("h2", { tabindex: "-1" }, "Saved versions"),
    h("div", { class: "fv-muted" }, "Versions saved by Qorqut. The file shown as current is the file on disk."));
  if (!hist) panel.append(h("p", { class: "fv-loading" }, "Loading versions…"));
  else {
    if (hist.error) panel.append(h("p", { class: "fv-muted", role: "alert" }, hist.error));
    if (!hist.versions.length && !hist.error) panel.append(h("p", { class: "fv-muted" }, "No saved versions yet."));
    panel.append(h("ol", {}, hist.versions.map((item, index) => versionRow(item, index === 0))));
    const actions = h("div", { class: "fv-history-actions" },
      h("button", { class: "fv-btn", type: "button", disabled: state.compare.length !== 2, onclick: runCompare }, "Compare selected"),
      hist.next ? h("button", { class: "fv-btn", type: "button", onclick: () => loadHistory(hist.next) }, "Older versions") : null);
    panel.append(actions);
  }
  if (state.diff) {
    const { diff } = state;
    panel.append(h("h2", {}, `Changes from version ${diff.from_version} to version ${diff.to_version}`));
    if (diff.diff_truncated) panel.append(h("div", { class: "fv-banner warn" }, "The comparison is very long and is shown only in part."));
    panel.append(diff.diff ? diffNode(diff.diff) : h("p", { class: "fv-muted" }, "These versions are identical."));
  }
  parts.panel.replaceChildren(panel);
}

async function runCompare() {
  const [from, to] = [...state.compare].sort((a, b) => a - b);
  try { state.diff = await call("history", historyQuery({ from_version: from, to_version: to })); }
  catch (error) { state.history.error = error.message; state.diff = null; }
  renderHistory();
}

async function main() {
  if (!query.has("path") && !query.has("document_id")) { showError({ code: "not_found", message: "" }); return; }
  root.replaceChildren(h("p", { class: "fv-loading" }, "Loading file…"));
  let meta;
  try { meta = await call("metadata", search); } catch (error) { showError(error, main); return; }
  Object.assign(state, { meta, payload: null, mode: "rendered", history: null, panel: false, compare: [], diff: null, drift: null });
  document.title = `${meta.name} · Qorqut`;
  parts.head = h("header", { class: "fv-head" });
  parts.notices = h("div");
  parts.tools = h("div", { class: "fv-tools", role: "toolbar", "aria-label": "View options" });
  parts.panel = h("div");
  parts.body = h("div", { class: "fv-body" });
  root.replaceChildren(parts.head, parts.notices, parts.tools, parts.panel, parts.body);
  renderHead(); renderNotices(); renderTools(); renderBody();
  if (meta.managed && meta.managed.history_available && !isVersion()) loadHistory(null).catch(() => {});
  if (meta.binary) { state.payload = { kind: "binary", size: meta.size, preview_bytes: 0, truncated: false }; renderNotices(); renderTools(); renderBody(); return; }
  try { state.payload = await call("content", search); }
  catch (error) { showError(error, main); return; }
  if (state.payload.truncated && state.payload.kind === "html") state.mode = "source";
  renderHead(); renderNotices(); renderTools(); renderBody();
}

main();
})();
