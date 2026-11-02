// The one Chat presentation: message list, markdown, timestamps, reference chips and previews, scrolling, pending receipts and
// composer. Native Chat (js/global-chat.js), item discussions (js/conversation.js) and Action discussions (js/actions.js) all render
// through mountChatView; docs/SHARED-CHAT-VIEW-CONTRACT.md is its interface.
// Presentation only: this module makes no request, stores no draft, never sorts, never decides who may write and never acknowledges
// anything. Controllers hand it normalized data and receive plain callbacks.
const CHAT_VIEW_TIME = new Intl.DateTimeFormat(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
const CHAT_VIEW_FULL_TIME = new Intl.DateTimeFormat(undefined, {dateStyle:'full', timeStyle:'long'});
function chatViewTimestamp(value) {
  if (!value) return '';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '';
  const full = esc(CHAT_VIEW_FULL_TIME.format(date));
  return `<time class="gchat-time" datetime="${date.toISOString()}" title="${full}" aria-label="${full}">${esc(CHAT_VIEW_TIME.format(date))}</time>`;
}

// ---- Markdown (headings, lists, quotes, tables, code, emphasis, and the shared safe links)
const chatViewInline = text => {
  let html = '';
  text.split(/(`[^`\n]+`)/g).forEach(part => {
    if (part.length > 2 && part[0] === '`' && part.endsWith('`')) { html += `<code>${esc(part.slice(1, -1))}</code>`; return; }
    // Links come from the one shared renderer; emphasis is applied only outside the anchors it produced.
    html += renderSafeLinks(part).split(/(<a class="safe-link"[^>]*>[^<]*<\/a>)/g).map((piece, index) => index % 2 ? piece : piece
      .replace(/\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])/g, '$1<em>$2</em>')).join('');
  });
  return html;
};
// Pipe tables use the same single-backtick code spans as the inline renderer. Outer pipes are optional.
function chatViewTableCells(line) {
  const cells = [];
  let cell = '', code = false, lastPipe = false;
  const text = line.trim();
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    lastPipe = false;
    if (char === '\\' && text[i + 1] === '|') { cell += '|'; i++; continue; }
    if (char === '\\' && text[i + 1] === '\\') { cell += '\\\\'; i++; continue; }
    if (char === '`') code = !code && text.indexOf('`', i + 1) > 0;
    if (char === '|' && !code) { cells.push(cell); cell = ''; lastPipe = true; continue; }
    cell += char;
  }
  cells.push(cell);
  if (text.startsWith('|')) cells.shift();
  if (lastPipe && cells.length) cells.pop();
  return cells.map(item => item.trim());
}
const chatViewTableAlign = cell => {
  const match = /^(:)?-{3,}(:)?$/.exec(cell);
  return match ? (match[1] && match[2] ? 'center' : match[2] ? 'right' : match[1] ? 'left' : '') : null;
};
function chatViewTable(head, align, rows) {
  const cell = (tag, text, i) => `<${tag}${tag === 'th' ? ' scope="col"' : ''}${align[i] ? ` class="gc-${align[i]}"` : ''}>${chatViewInline(text)}</${tag}>`;
  const row = (tag, cells) => `<tr>${align.map((_, i) => cell(tag, cells[i] ?? '', i)).join('')}</tr>`;
  return `<div class="gc-table-wrap" role="region" aria-label="Table" tabindex="0"><table><thead>${row('th', head)}</thead>${rows.length ? `<tbody>${rows.map(cells => row('td', cells)).join('')}</tbody>` : ''}</table></div>`;
}
function chatViewMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let paragraph = [], list = null, quote = [];
  const flushParagraph = () => { if (paragraph.length) out.push(`<p>${paragraph.map(chatViewInline).join('<br>')}</p>`); paragraph = []; };
  const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map(item => `<li>${chatViewInline(item)}</li>`).join('')}</${list.tag}>`); list = null; };
  const flushQuote = () => { if (quote.length) out.push(`<blockquote>${quote.map(chatViewInline).join('<br>')}</blockquote>`); quote = []; };
  const flush = () => { flushParagraph(); flushList(); flushQuote(); };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const fence = /^\s*```\s*([\w+-]{0,20})\s*$/.exec(line);
    if (fence) {
      flush();
      const code = [];
      while (++index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index]);
      out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    const heading = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) { flush(); out.push(`<p class="gc-heading">${chatViewInline(heading[2])}</p>`); continue; }
    const bullet = /^\s*[-*+]\s+(.+)$/.exec(line), numbered = /^\s*\d{1,3}[.)]\s+(.+)$/.exec(line);
    if (bullet || numbered) {
      const tag = bullet ? 'ul' : 'ol';
      flushParagraph(); flushQuote();
      if (list && list.tag !== tag) flushList();
      (list ||= {tag, items: []}).items.push((bullet || numbered)[1]);
      continue;
    }
    const quoted = /^\s*>\s?(.*)$/.exec(line);
    if (quoted) { flushParagraph(); flushList(); quote.push(quoted[1]); continue; }
    flushList(); flushQuote();
    if (line.includes('|') && index + 1 < lines.length && lines[index + 1].includes('|')) {
      const head = chatViewTableCells(line), marks = chatViewTableCells(lines[index + 1]).map(chatViewTableAlign);
      if (head.length === marks.length && marks.length && marks.every(mark => mark !== null)) {
        flushParagraph();
        const rows = [];
        index++;
        while (index + 1 < lines.length && lines[index + 1].trim() && lines[index + 1].includes('|') && !/^\s*```/.test(lines[index + 1])) rows.push(chatViewTableCells(lines[++index]));
        out.push(chatViewTable(head, marks, rows));
        continue;
      }
    }
    paragraph.push(line);
  }
  flush();
  return out.join('');
}

function chatViewReferenceTarget(href) {
  const safe = safeLinkUrl(href);
  if (!safe) return null;
  const url = new URL(safe, location.origin);
  const viewer = url.origin === location.origin && ['/files/view', '/tables/view'].includes(url.pathname);
  const path = viewer ? url.searchParams.get('path') || '' : url.pathname;
  const image = /\.(?:png|jpe?g|gif|webp)$/i.test(path);
  const kind = image ? 'Image' : viewer ? url.pathname === '/tables/view' ? 'Table' : 'File' : 'Web page';
  return {url, viewer, image, kind, path};
}

// A selected upload and a reference to the saved file use exactly the same face.
// Classification is presentation only; the existing viewer still decides how to open it.
function chatViewFileKind(name) {
  const ext = /\.([a-z0-9]+)$/i.exec(name)?.[1].toLowerCase() || '';
  if (/^(png|jpe?g|gif|webp|svg|avif|heic|bmp|tiff?)$/.test(ext)) return ['image', 'Image'];
  if (ext === 'pdf') return ['pdf', 'PDF'];
  if (/^(csv|tsv|xlsx?|ods)$/.test(ext)) return ['sheet', 'Spreadsheet'];
  if (/^(mp4|mov|webm|m4v|avi|mkv)$/.test(ext)) return ['video', 'Video'];
  if (/^(mp3|wav|m4a|ogg|flac|aac)$/.test(ext)) return ['audio', 'Audio'];
  if (/^(zip|gz|tgz|tar|7z|rar|bz2|xz)$/.test(ext)) return ['archive', 'Archive'];
  if (/^(js|jsx|ts|tsx|py|rb|go|rs|java|c|cpp|h|sh|css|html?|jsonl?|ya?ml|toml|xml|sql)$/.test(ext)) return ['code', 'Code'];
  if (/^(md|txt|docx?|odt|rtf|pages)$/.test(ext)) return ['document', ext === 'md' ? 'Markdown' : ext === 'txt' ? 'Text' : 'Document'];
  return ['file', 'File'];
}
const CHAT_VIEW_FILE_ICONS = {
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h6"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  sheet: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>',
  code: '<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16"/>',
  video: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m10 8 6 4-6 4z"/>',
  audio: '<path d="M9 18V5l12-2v13M9 9l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
  archive: '<rect x="3" y="3" width="18" height="4" rx="1"/><path d="M5 7v13h14V7M10 11h4M12 7v4"/>',
};
function chatViewFileFace(name, kind) {
  return `<span class="gchat-file-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${CHAT_VIEW_FILE_ICONS[kind[0]] || CHAT_VIEW_FILE_ICONS.file}</svg></span><span class="gchat-file-label"><span class="gchat-file-name">${esc(name)}</span><small>${kind[1]}</small></span>`;
}

// Chips represent rendered links, never code samples. No document/image request happens until a chip is opened.
function chatViewReferenceChips(body) {
  const chips = document.createElement('div'), seen = new Set();
  chips.className = 'gchat-references';
  for (const link of body.querySelectorAll('a.safe-link')) {
    const target = chatViewReferenceTarget(link.href);
    if (!target || seen.has(target.url.href)) continue;
    seen.add(target.url.href);
    const chip = document.createElement('button');
    chip.type = 'button'; chip.className = 'gchat-reference';
    chip.dataset.previewHref = target.url.href; chip.dataset.previewTitle = link.textContent;
    chip.setAttribute('aria-label', `Preview ${link.textContent}`);
    if ((target.viewer && target.url.pathname === '/files/view') || target.image) {
      const kind = chatViewFileKind(target.path);
      chip.classList.add('gchat-file-card'); chip.dataset.fileKind = kind[0];
      chip.innerHTML = chatViewFileFace(link.textContent, kind);
    } else {
      if (typeof AGVB_ICON !== 'undefined') chip.innerHTML = AGVB_ICON[target.kind === 'Table' ? 'table' : 'web'];
      const label = document.createElement('span'); label.textContent = link.textContent;
      const kind = document.createElement('small'); kind.textContent = target.kind;
      chip.append(label, kind);
    }
    chips.append(chip);
  }
  return chips.childElementCount ? chips : null;
}

function chatViewOwnerReferences(body) {
  const chips = chatViewReferenceChips(body);
  let removed = false;
  // A copied attachment is shown once as its reference card. Its local path
  // stays in the native transcript, rather than cluttering the visible text.
  for (const link of body.querySelectorAll('a.safe-link')) {
    const target = chatViewReferenceTarget(link.href), path = target?.url.searchParams.get('path') || '';
    if (target?.viewer && /(?:^|\/)agents\/[^/]+\/downloads\//.test(path)) { link.remove(); removed = true; }
  }
  if (removed) {
    const text = body.querySelector('.gchat-text');
    text?.normalize();
    if (text?.lastChild?.nodeType === Node.TEXT_NODE) text.lastChild.textContent = text.lastChild.textContent.trimEnd();
    if (!body.textContent.trim()) body.hidden = true;
  }
  return chips;
}

// ---- the view
// host: element that receives the shared root. render() patches presentation fields; see the contract for their shapes.
function mountChatView(host, options = {}) {
  const defaults = {
    log: options.logLabel || 'Conversation', composer: options.composerLabel || 'Message', placeholder: options.placeholder || 'Message…',
    maxLength: options.maxLength || 20000, older: options.olderLabel || 'Load older messages',
  };
  const model = {
    entries: [], pending: [],
    composer: {hidden: false, inputDisabled: false, sendDisabled: false, sendLabel: 'Send', placeholder: defaults.placeholder, ariaLabel: defaults.composer, maxLength: defaults.maxLength},
    older: {visible: false, disabled: false, label: defaults.older},
    banner: {text: '', role: 'status'},
    empty: {text: '', action: null},
  };
  const nodes = new Map();                       // entry key -> element
  const pendingNodes = new Map();                // pending key -> element
  const cleanups = [];
  let disposed = false, ready = false, initial = true, atBottom = true, unseen = 0, deferBottom = false, pendingState = null;
  let lastState = {top: 0, atBottom: true, anchorKey: null, anchorOffset: 0};

  const root = document.createElement('div');
  root.className = 'chat-view';
  root.innerHTML = `<div class="gchat-banner" role="status" hidden></div>
    <div class="chat-view-feed"><div class="gchat-scroll" tabindex="-1"><button type="button" class="quiet gchat-older" hidden></button>
      <div class="gchat-log" role="log" aria-live="polite" aria-label="${esc(defaults.log)}"></div>
      <div class="gchat-pending-list"></div>
      <div class="gchat-empty" hidden></div></div>
      <button type="button" class="gchat-jump" hidden>New messages ↓</button>
      <section class="gchat-preview-window" role="dialog" aria-label="Document preview" hidden>
        <header><span class="gchat-preview-title"></span><a class="gchat-preview-open" target="_blank" rel="noopener noreferrer" aria-label="Open preview in new tab">↗</a><button type="button" class="quiet" data-preview-close aria-label="Close preview">×</button></header>
        <div class="gchat-preview-content"></div>
      </section></div>`;
  const form = document.createElement('form');
  form.className = 'gchat-composer';
  form.innerHTML = '<textarea rows="2" autocomplete="off"></textarea><button type="submit" class="gchat-send">Send</button>';
  const attachments = document.createElement('div');
  attachments.className = 'gchat-attachments'; attachments.hidden = true;
  form.prepend(attachments);
  let picker = null, attach = null;
  if (options.onFiles) {
    attach = document.createElement('button'); attach.type = 'button'; attach.className = 'gchat-attach quiet';
    attach.setAttribute('aria-label', 'Attach files'); attach.title = 'Attach files';
    attach.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 13a5 5 0 0 0 7 .1l3-3a5 5 0 0 0-7-7l-1.7 1.7M14 11a5 5 0 0 0-7-.1l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>';
    picker = document.createElement('input'); picker.type = 'file'; picker.multiple = true; picker.hidden = true;
    form.append(attach, picker);
  }
  const $q = selector => root.querySelector(selector);
  const banner = $q('.gchat-banner'), scroller = $q('.gchat-scroll'), log = $q('.gchat-log'), pendingHost = $q('.gchat-pending-list'), emptyNote = $q('.gchat-empty');
  const olderButton = $q('.gchat-older'), jump = $q('.gchat-jump'), preview = $q('.gchat-preview-window'), previewContent = $q('.gchat-preview-content');
  const input = form.querySelector('textarea'), send = form.querySelector('.gchat-send');
  host.append(root);
  if (options.composerHost) options.composerHost.append(form);
  else { if (options.footer) root.append(options.footer); root.append(form); }
  if (options.composerHost && options.footer) form.before(options.footer);
  // A collapsed inline composer keeps the one shared banner beside its form so a failed send stays visible.
  if (options.bannerHost) options.bannerHost.append(banner);

  const listen = (target, type, handler, opts) => { target.addEventListener(type, handler, opts); cleanups.push(() => target.removeEventListener(type, handler, opts)); };
  const emit = (name, ...args) => { if (!disposed && ready) options[name]?.(...args); };

  // ---- geometry
  const shown = () => !disposed && root.isConnected && !root.closest('[inert]') && scroller.getClientRects().length > 0 && scroller.clientHeight > 0
    && getComputedStyle(root).visibility !== 'hidden';
  const nearBottom = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
  const entryNodes = () => [...log.children];
  function capture() {
    const edge = scroller.getBoundingClientRect().top;
    let anchorKey = null, anchorOffset = 0;
    for (const node of entryNodes()) {
      const box = node.getBoundingClientRect();
      if (box.bottom > edge + 1) { anchorKey = node.dataset.id; anchorOffset = box.top - edge; break; }
    }
    return {top: scroller.scrollTop, atBottom: nearBottom(), anchorKey, anchorOffset};
  }
  function restoreAnchor(anchor) {
    const node = anchor?.anchorKey != null && nodes.get(anchor.anchorKey);
    if (!node?.isConnected) return false;
    const delta = node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - anchor.anchorOffset;
    if (delta) scroller.scrollTop += delta;
    return true;
  }
  function toBottom() {
    scroller.scrollTop = scroller.scrollHeight;
    atBottom = true; unseen = 0; jump.hidden = true; initial = false; pendingState = null; deferBottom = false;
  }
  function applyState(saved) {
    if (saved.atBottom) toBottom();
    else if (!restoreAnchor(saved)) scroller.scrollTop = saved.top || 0;
    atBottom = nearBottom();
  }
  // Applies whatever was deferred while the container had no layout.
  function settle() {
    if (!shown()) return;
    if (pendingState && (model.entries.length || pendingState.atBottom)) { const saved = pendingState; pendingState = null; initial = false; deferBottom = false; applyState(saved); }
    else if (deferBottom || (initial && !pendingState && model.entries.length)) toBottom();
    atBottom = nearBottom();
    lastState = capture();
  }
  listen(scroller, 'scroll', () => {
    atBottom = nearBottom();
    if (atBottom) { unseen = 0; jump.hidden = true; }
    if (shown()) lastState = capture();
    emit('onViewportChange');
  });
  jump.onclick = () => { toBottom(); input.focus(); emit('onViewportChange'); };
  cleanups.push(() => { jump.onclick = null; });
  // Per-element only: a container that gains layout (details, tab, dock) settles its deferred scroll; a bottom-pinned feed stays pinned.
  if (typeof ResizeObserver === 'function') {
    const resizes = new ResizeObserver(() => {
      if (disposed || !shown()) return;
      if (deferBottom || pendingState || initial) settle();
      else if (atBottom) scroller.scrollTop = scroller.scrollHeight;
    });
    resizes.observe(scroller);
    cleanups.push(() => resizes.disconnect());
  }

  // ---- preview of a rendered link: one on-demand window, released on close and dispose
  let previewTrigger = null;
  const clearPreview = () => {
    previewContent.querySelectorAll('iframe').forEach(frame => { frame.src = 'about:blank'; });
    previewContent.replaceChildren();
  };
  const closePreview = (restoreFocus = true) => {
    clearPreview(); preview.hidden = true;
    if (restoreFocus && previewTrigger?.isConnected) previewTrigger.focus({preventScroll:true});
    previewTrigger = null;
  };
  cleanups.push(() => closePreview(false));
  const openPreview = button => {
    const target = chatViewReferenceTarget(button.dataset.previewHref);
    if (!target) return;
    clearPreview(); previewTrigger = button;
    const title = button.dataset.previewTitle;
    $q('.gchat-preview-title').textContent = title;
    $q('.gchat-preview-open').href = target.url.href;
    const media = document.createElement(target.image ? 'img' : 'iframe');
    media.referrerPolicy = 'no-referrer';
    if (target.image) {
      media.alt = title; media.decoding = 'async';
      media.src = target.viewer ? `/api/root/files/image${target.url.search}` : target.url.href;
      media.addEventListener('error', () => { if (media.isConnected) previewContent.textContent = 'This image could not be displayed.'; });
    } else {
      media.setAttribute('aria-label', title + ' preview');
      // Viewbox uses these same reader shells; arbitrary websites retain its sandbox.
      if (!target.viewer) media.setAttribute('sandbox', 'allow-scripts allow-forms');
      media.src = target.url.href;
    }
    previewContent.append(media); preview.hidden = false;
    $q('[data-preview-close]').focus({preventScroll:true});
  };
  listen(preview, 'keydown', event => { if (event.key === 'Escape') { event.stopPropagation(); closePreview(); } });

  // ---- entries
  const entrySignature = entry => JSON.stringify([entry.speaker, entry.text, entry.partial === true, entry.timestamp, entry.title, entry.showAuthor,
    entry.author?.name, entry.author?.avatarHtml, entry.notice, entry.collapsedSource,
    (entry.references || []).map(ref => [ref.key, ref.label, !!ref.disabled]), (entry.actions || []).map(action => [action.key, action.label, !!action.disabled])]);
  const authorHtml = entry => entry.author && entry.showAuthor !== false
    ? `<div class="gchat-author">${entry.author.avatarHtml ? `<span class="gchat-avatar" aria-hidden="true">${entry.author.avatarHtml}</span>` : ''}<small class="gchat-author-name">${esc(entry.author.name || '')}</small></div>` : '';
  const titleHtml = entry => entry.title ? `<strong class="gchat-title">${esc(entry.title)}</strong>` : '';
  const markerHtml = notice => notice?.label ? `<span class="gchat-marker">${esc(notice.label)}</span>` : '';
  const telegramBody = text => `<details class="gchat-body gchat-telegram"><summary><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M21.4 3.6 18.1 20c-.2 1.2-.9 1.5-1.8.9l-5-3.7-2.4 2.3c-.3.3-.5.5-1 .5l.4-5.1L17.6 6.5c.4-.4-.1-.6-.6-.3L5.5 13.4.6 11.9c-1.1-.4-1.1-1.1.2-1.6L20 2.9c.9-.3 1.7.2 1.4.7Z"/></svg><span>Telegram</span></summary><span class="gchat-text">${esc(text)}</span></details>`;
  function paintEntry(entry) {
    let node = nodes.get(entry.key);
    const isNew = !node;
    if (isNew) { node = document.createElement('div'); nodes.set(entry.key, node); }
    const speaker = entry.speaker || 'agent';
    node.className = `gchat-msg gchat-${speaker}`;
    node.dataset.id = entry.key;
    if (entry.position != null) node.dataset.position = String(entry.position); else delete node.dataset.position;
    node._entry = entry;
    // Replayed entries keep their chips and keyboard focus.
    const signature = entrySignature(entry);
    if (node.chatSignature === signature) return {node, isNew};
    node.chatSignature = signature;
    node.classList.toggle('gchat-partial', entry.partial === true);
    const notice = entry.notice;
    if (speaker === 'notice' && notice && typeof notice.verified === 'boolean') node.dataset.verified = String(notice.verified); else delete node.dataset.verified;
    if (speaker === 'notice' && notice?.inputType) node.dataset.inputType = notice.inputType; else delete node.dataset.inputType;
    let body;
    if (entry.collapsedSource === 'telegram') body = telegramBody(entry.text);
    else if (speaker === 'agent') body = `<div class="gchat-body md">${titleHtml(entry)}${chatViewMarkdown(entry.text)}</div>`;
    else if (speaker === 'notice') body = `<div class="gchat-body">${markerHtml(notice)}${notice?.markerOnly ? '' : `<span class="gchat-text">${esc(entry.text)}</span>`}</div>`;
    else body = `<div class="gchat-body">${titleHtml(entry)}<span class="gchat-text">${renderSafeLinks(entry.text)}</span></div>`;
    node.innerHTML = authorHtml(entry) + body;
    const refs = (speaker === 'user' ? chatViewOwnerReferences(node.querySelector('.gchat-body'))
      : speaker === 'agent' && entry.partial !== true && chatViewReferenceChips(node.querySelector('.gchat-body'))) || document.createElement('div');
    refs.className = 'gchat-references';
    for (const ref of entry.references || []) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'gchat-reference quiet'; button.dataset.refKey = ref.key; button.disabled = !!ref.disabled;
      const label = document.createElement('span'); label.textContent = ref.label || '';
      button.append(label); refs.append(button);
    }
    if (refs.childElementCount) node.append(refs);
    if (entry.actions?.length) {
      const actions = document.createElement('div');
      actions.className = 'gchat-entry-actions';
      for (const action of entry.actions) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'quiet'; button.dataset.entryAction = action.key; button.disabled = !!action.disabled; button.textContent = action.label || '';
        actions.append(button);
      }
      node.append(actions);
    }
    node.insertAdjacentHTML('beforeend', chatViewTimestamp(entry.timestamp));
    return {node, isNew};
  }
  // Entries arrive in canonical order; nodes are reused and moved only when their position changes.
  function reconcile(list, host, map, paint) {
    const keep = new Set(list.map(item => item.key));
    for (const [key, node] of map) if (!keep.has(key)) { node.remove(); map.delete(key); }
    let cursor = host.firstChild;
    for (const item of list) {
      const {node} = paint(item);
      if (node === cursor) cursor = cursor.nextSibling; else host.insertBefore(node, cursor);
    }
  }

  // ---- pending receipts: normalized by the controller; the view only draws them
  function paintPending(item) {
    let node = pendingNodes.get(item.key);
    if (!node) { node = document.createElement('div'); pendingNodes.set(item.key, node); }
    const speaker = item.speaker === 'notice' ? 'notice' : 'user';
    node.className = `gchat-msg gchat-pending gchat-${speaker}`;
    node.dataset.inputId = item.key; node.dataset.state = item.state || ''; node.dataset.inputType = item.inputType || '';
    node.dataset.resendable = String(!!item.resendable);
    const signature = JSON.stringify([item.state, item.stateLabel, item.text, item.notice?.label, !!item.resendable, item.collapsedSource,
      (item.actions || []).map(action => [action.key, action.label, !!action.disabled])]);
    if (node.chatSignature !== signature) {
      node.chatSignature = signature;
      const actions = (item.actions || []).map(action => `<button type="button" class="quiet" data-pending-action="${esc(action.key)}" data-${esc(action.key)}="${esc(item.key)}" ${action.disabled ? 'disabled' : ''}>${esc(action.label)}</button>`).join('');
      node.innerHTML = `${item.collapsedSource === 'telegram' ? telegramBody(item.text) : `<div class="gchat-body">${markerHtml(item.notice)}${item.text ? `<span class="gchat-text">${speaker === 'user' ? renderSafeLinks(item.text) : esc(item.text)}</span>` : ''}</div>`}
        <span class="gchat-state" data-state="${esc(item.state || '')}">${esc(item.stateLabel || '')}</span>
        ${actions ? `<span class="gchat-pending-actions">${actions}</span>` : ''}`;
      if (speaker === 'user' && !item.collapsedSource) {
        const refs = chatViewOwnerReferences(node.querySelector('.gchat-body'));
        if (refs) node.insertBefore(refs, node.querySelector('.gchat-state'));
      }
    }
    return {node};
  }

  // ---- fields
  function paintComposer() {
    const c = model.composer;
    form.hidden = !!c.hidden;
    input.disabled = !!c.inputDisabled; send.disabled = !!c.sendDisabled;
    if (attach) attach.disabled = !!c.inputDisabled || !!c.filesDisabled;
    const files = c.attachments || [];
    attachments.hidden = !files.length;
    const signature = JSON.stringify([files, !!c.filesDisabled]);
    if (attachments.signature !== signature) {
      attachments.signature = signature;
      attachments.innerHTML = files.map(file => {
        const kind = chatViewFileKind(file.name);
        return `<span class="gchat-file-choice gchat-file-card" data-file-kind="${kind[0]}">${chatViewFileFace(file.name, kind)}<button type="button" data-remove-attachment="${esc(file.key)}" aria-label="Remove ${esc(file.name)}" ${c.filesDisabled ? 'disabled' : ''}>×</button></span>`;
      }).join('');
    }
    if (send.textContent !== c.sendLabel) send.textContent = c.sendLabel;
    input.placeholder = c.placeholder; input.setAttribute('aria-label', c.ariaLabel);
    if (input.maxLength !== c.maxLength) input.maxLength = c.maxLength;
  }
  function paintOlder() {
    olderButton.hidden = !model.older.visible;
    olderButton.disabled = !!model.older.disabled;
    olderButton.textContent = model.older.label;
  }
  function paintBanner() {
    const {text, role} = model.banner;
    banner.hidden = !text; banner.textContent = text || '';
    banner.setAttribute('role', role || 'status');
  }
  function paintEmpty() {
    const {text, action} = model.empty;
    emptyNote.hidden = !text;
    emptyNote.replaceChildren();
    if (!text) return;
    const line = document.createElement('p'); line.textContent = text; emptyNote.append(line);
    if (action) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'quiet'; button.dataset.emptyAction = action.key; button.textContent = action.label || '';
      emptyNote.append(button);
    }
  }
  let emptySignature = '';

  function render(patch = {}, opts = {}) {
    if (disposed) return;
    const mode = opts.scroll || 'auto';
    const visible = shown();
    const wasBottom = initial || (visible ? nearBottom() : atBottom);
    const anchor = visible && !pendingState && (mode === 'prepend' || mode === 'preserve' || (mode === 'auto' && !wasBottom)) ? capture() : null;
    const before = new Set(nodes.keys());
    if (patch.composer) { Object.assign(model.composer, patch.composer); paintComposer(); }
    if (patch.older) { Object.assign(model.older, patch.older); paintOlder(); }
    if (patch.banner) { model.banner = {text: '', role: 'status', ...patch.banner}; paintBanner(); }
    if (patch.empty) {
      model.empty = {text: '', action: null, ...patch.empty};
      const signature = JSON.stringify(model.empty);
      if (signature !== emptySignature) { emptySignature = signature; paintEmpty(); }
    }
    let appended = 0;
    if (patch.entries) {
      model.entries = patch.entries;
      reconcile(model.entries, log, nodes, paintEntry);
      let last = -1;
      model.entries.forEach((entry, index) => { if (before.has(entry.key)) last = index; });
      model.entries.forEach((entry, index) => { if (index > last && !before.has(entry.key) && entry.speaker === 'agent') appended++; });
    }
    if (patch.pending) { model.pending = patch.pending; reconcile(model.pending, pendingHost, pendingNodes, paintPending); }
    if (!visible) {
      if (mode === 'bottom') deferBottom = true;
      return;
    }
    if (mode === 'bottom') toBottom();
    else if (pendingState) settle();
    else if (initial && model.entries.length) toBottom();
    else if (anchor) {
      restoreAnchor(anchor);
      if (mode === 'auto' && appended) { unseen += appended; jump.hidden = false; jump.textContent = unseen > 1 ? `${unseen} new messages ↓` : 'New messages ↓'; }
    } else if (mode === 'auto' && wasBottom) toBottom();
    atBottom = nearBottom();
    lastState = capture();
    emit('onViewportChange');
  }

  // ---- interaction
  listen(root, 'click', event => {
    const chip = event.target.closest('[data-preview-href]');
    if (chip) { openPreview(chip); return; }
    if (event.target.closest('[data-preview-close]')) { closePreview(); return; }
    const entryNode = event.target.closest('.gchat-msg[data-id]');
    const ref = event.target.closest('[data-ref-key]');
    if (ref && entryNode) {
      const reference = entryNode._entry?.references?.find(item => item.key === ref.dataset.refKey);
      if (reference && !reference.disabled) emit('onReference', reference.value, entryNode.dataset.id);
      return;
    }
    const entryAction = event.target.closest('[data-entry-action]');
    if (entryAction && entryNode) { emit('onEntryAction', entryAction.dataset.entryAction, entryNode.dataset.id); return; }
    const pendingAction = event.target.closest('[data-pending-action]');
    if (pendingAction) { emit('onPendingAction', pendingAction.dataset.pendingAction, pendingAction.closest('[data-input-id]')?.dataset.inputId); return; }
    const emptyAction = event.target.closest('[data-empty-action]');
    if (emptyAction) emit('onEmptyAction', emptyAction.dataset.emptyAction);
  });
  olderButton.onclick = () => emit('onOlder');
  cleanups.push(() => { olderButton.onclick = null; });
  listen(input, 'input', () => emit('onDraft', input.value));
  if (picker) {
    listen(attach, 'click', () => picker.click());
    listen(picker, 'change', () => { emit('onFiles', [...picker.files]); picker.value = ''; });
    listen(attachments, 'click', event => {
      const button = event.target.closest('[data-remove-attachment]');
      if (button && !button.disabled) emit('onAttachmentRemove', button.dataset.removeAttachment);
    });
    let depth = 0;
    const fileDrag = event => [...(event.dataTransfer?.types || [])].includes('Files');
    const clearDrop = () => { depth = 0; root.classList.remove('gchat-file-drop'); };
    listen(host, 'dragenter', event => { if (fileDrag(event)) { event.preventDefault(); depth++; if (!attach.disabled) root.classList.add('gchat-file-drop'); } });
    listen(host, 'dragover', event => { if (fileDrag(event)) { event.preventDefault(); event.dataTransfer.dropEffect = attach.disabled ? 'none' : 'copy'; } });
    listen(host, 'dragleave', event => { if (fileDrag(event) && --depth <= 0) clearDrop(); });
    listen(host, 'drop', event => { if (fileDrag(event)) { event.preventDefault(); clearDrop(); if (!attach.disabled) emit('onFiles', [...event.dataTransfer.files]); } });
    cleanups.push(clearDrop);
  }
  listen(input, 'keydown', event => {
    // Enter sends, Shift+Enter inserts a newline, Ctrl/Meta+Enter always sends, and a composition in progress (IME) is never interrupted.
    if (event.key === 'Enter' && !event.isComposing && (!event.shiftKey || event.ctrlKey || event.metaKey)) {
      event.preventDefault(); form.requestSubmit();
    }
  });
  listen(form, 'submit', event => {
    event.preventDefault();
    // inputDisabled only locks editing: a visible, enabled Send (a retry of a locked draft) still dispatches.
    if (model.composer.hidden || send.disabled) return;
    emit('onSubmit', input.value);
  });

  paintComposer(); paintOlder(); paintBanner();
  ready = true;
  return {
    root,
    elements: {scroll: scroller, log, pending: pendingHost, composer: form, input, send, older: olderButton, jump, banner, empty: emptyNote},
    render,
    setDraft(text) { const value = String(text ?? ''); if (!disposed && input.value !== value) input.value = value; },
    getDraft: () => input.value,
    focusComposer() { if (!disposed && !input.disabled && !form.hidden) input.focus(); },
    getScrollState() { if (!disposed && shown()) lastState = capture(); return {...lastState}; },
    setScrollState(saved) {
      if (disposed || !saved) return;
      pendingState = {top: saved.top || 0, atBottom: !!saved.atBottom, anchorKey: saved.anchorKey ?? null, anchorOffset: saved.anchorOffset || 0};
      initial = false; deferBottom = false;
      settle();
    },
    layout() { if (disposed) return; settle(); emit('onViewportChange'); },
    scrollToBottom() { if (!disposed && shown()) { toBottom(); lastState = capture(); emit('onViewportChange'); } else if (!disposed) deferBottom = true; },
    isAtBottom: () => (shown() ? nearBottom() : atBottom),
    getVisibleKeys() {
      if (disposed || !shown()) return [];
      const edge = scroller.getBoundingClientRect(), keys = [];
      for (const node of entryNodes()) {
        const box = node.getBoundingClientRect();
        if (box.bottom > edge.top && box.top < edge.bottom) keys.push(node.dataset.id);
      }
      return keys;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      cleanups.splice(0).forEach(cleanup => cleanup());
      nodes.clear(); pendingNodes.clear();
      input.disabled = true; send.disabled = true;
      form.remove(); banner.remove(); root.remove();
    },
  };
}
