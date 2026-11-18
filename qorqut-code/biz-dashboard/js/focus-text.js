// Owner Focus notes: one plain text area per agent pane, saved as the owner's private dashboard preference.
// A line that starts with "-" shows a status ball in place of the dash, the same ball the Journal uses (click cycles,
// right-click toggles blocked). The ball is only a visual: nothing here touches the Journal or any operations layer.
// The ball's state is saved with the text, as a small invisible token after the dash on that line.
// No agent or model reads it. Until the owner saves anything at all, the area starts with a short sample text;
// once they change it (even to empty) it stays exactly as they left it.
// Drafts live in client state keyed by agent ID, so navigation never moves them to another agent or drops them.
const focusText = (() => {
  const DEBOUNCE_MS = 500, MAX_CHARS = 100000, FIRST_TEXT = 'Your notes here';
  const states = new Map();
  // Status balls: the cycle, labels, markup and the dash rule are the Journal's own (js/status-ball.js).
  const SEP = '\u2063';
  const TOKEN = /^([ \t]*-)\u2063(white|in_progress|done|blocked|cancelled)\u2063/;
  const path = id => `/root/agents/${encodeURIComponent(id)}/focus-layout`;
  // Earlier layouts could hold several separate blocks; they read as one text, in stored order.
  const joined = blocks => blocks.map(b => b.text).join('\n\n');

  // The saved text carries each non-open ball as a token after its dash; the editor and the balls work on the clean text.
  function parse(wire) {
    const marks = [];
    const text = wire.split('\n').map((line, i) => {
      const m = TOKEN.exec(line);
      if (!m) return line;
      marks[i] = m[2];
      return m[1] + line.slice(m[0].length);
    }).join('\n');
    return {text, marks};
  }
  const wire = s => s.text.split('\n').map((line, i) => s.marks[i] && s.marks[i] !== 'todo' && ntIsTaskLine(line)
    ? line.replace(/^([ \t]*-)/, `$1${SEP}${s.marks[i]}${SEP}`) : line).join('\n');
  // After an edit, each line keeps its ball: lines before and after the edited stretch map directly; inside it, an unchanged
  // line keeps its own ball (so a line pushed down by Enter takes it along) and an edited line keeps the one at its position.
  function remap(marks, before, after) {
    const a = before.split('\n'), b = after.split('\n');
    let head = 0, tail = 0;
    while (head < a.length && head < b.length && a[head] === b[head]) head++;
    while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
    const end = a.length - tail, out = b.map((_, i) => i < head ? marks[i] : i >= b.length - tail ? marks[a.length - (b.length - i)] : undefined);
    const claimed = new Set();
    let from = head;
    for (let i = head; i < b.length - tail; i++) {
      const j = a.indexOf(b[i], from);
      if (j !== -1 && j < end) { out[i] = marks[j]; claimed.add(j); from = j + 1; }
    }
    for (let i = head; i < b.length - tail; i++) if (out[i] === undefined && i < end && !claimed.has(i)) out[i] = marks[i];
    return out;
  }

  function state(agentId) {
    let s = states.get(agentId);
    if (!s) {
      s = {agentId, loaded: false, loadError: '', revision: 0, blockId: null, text: '', marks: [], baseline: '', status: '', error: '',
        conflict: false, saving: false, timer: null, queue: null, loadSeq: 0, saveCount: 0, views: new Set()};
      states.set(agentId, s);
    }
    return s;
  }
  const dirty = s => s.loaded && wire(s) !== s.baseline;
  const live = s => [...s.views].filter(p => p.agentId === s.agentId && p.focusMount?.isConnected);
  function changed(s) { for (const p of live(s)) paint(p); }

  // ---- loading
  function adopt(s, d) {
    // Nothing ever saved for this agent: the very first start shows the sample text.
    const fresh = d.revision === 0 && !d.blocks.length;
    s.blockId = d.blocks[0]?.id || null;
    s.baseline = fresh ? FIRST_TEXT : joined(d.blocks);
    ({text: s.text, marks: s.marks} = parse(s.baseline));
    s.revision = d.revision;
  }
  async function load(p, mode = 'merge') {
    if (!p.agentId) return;
    const s = state(p.agentId), seq = ++s.loadSeq, saves = s.saveCount;
    try {
      const d = coreData(await api(path(p.agentId)));
      if (seq !== s.loadSeq) return;
      if (!Array.isArray(d.blocks) || !Number.isInteger(d.revision)) throw new Error('Invalid notes response');
      s.loadError = '';
      if (mode === 'revision') s.revision = d.revision;
      else if (mode === 'adopt' || !s.loaded || (!dirty(s) && !s.queue?.pending && saves === s.saveCount)) {
        adopt(s, d);
        if (mode === 'adopt') { s.conflict = false; s.status = ''; s.error = ''; }
      }
      s.loaded = true;
    } catch (error) {
      if (seq !== s.loadSeq) return;
      s.loadError = error.message || 'Notes could not be read.';
    }
    changed(s);
  }

  // ---- saving: serial, debounced, one whole-text write that never carries a draft to another agent
  function schedule(s, delay = DEBOUNCE_MS) {
    clearTimeout(s.timer);
    s.timer = setTimeout(() => save(s), delay);
    changed(s);
  }
  function save(s) {
    clearTimeout(s.timer); s.timer = null;
    const run = () => saveOnce(s).finally(() => { if (s.queue === next) s.queue = null; });
    const next = s.queue = (s.queue || Promise.resolve()).then(run);
    next.pending = true;
    return next;
  }
  async function saveOnce(s) {
    if (!s.loaded || s.conflict) return false;
    const sent = wire(s);
    if (sent === s.baseline) return true;
    s.blockId ||= newRequestId();
    s.saving = true; s.status = 'saving'; s.error = ''; s.saveCount++; changed(s);
    let ok = false;
    try {
      const d = coreData(await post(path(s.agentId), {expected_revision: s.revision, blocks: [{id: s.blockId, text: sent, before_id: null, after_id: null}]}));
      s.revision = d.revision; s.baseline = joined(d.blocks); s.blockId = d.blocks[0]?.id || s.blockId;
      s.status = 'saved'; ok = true;
    } catch (error) {
      s.error = error.message || 'The notes were not saved.';
      if (error.code === 'revision_conflict') { s.conflict = true; s.status = 'conflict'; } else s.status = 'failed';
    }
    s.saving = false;
    changed(s);
    return ok;
  }
  const label = s => s.conflict ? 'conflict' : s.status === 'failed' ? 'failed' : (s.saving || s.timer || dirty(s)) ? 'saving' : s.status;

  // ---- painting
  function paintStatus(p) {
    const el = p.focusMount?.querySelector('.agfocus-text-status');
    if (!el || !p.agentId) return;
    const s = state(p.agentId), kind = !s.loaded ? (s.loadError ? 'load' : '') : label(s);
    const message = {load: "Notes couldn't load. " + s.loadError, conflict: 'Notes changed elsewhere. Your edits are kept here.', failed: "Couldn't save notes. " + s.error, saving: 'Saving…', saved: 'Saved'}[kind] || '';
    el.hidden = !message;
    el.dataset.state = kind;
    const text = el.querySelector('[data-text-message]');
    if (text.textContent !== message) text.textContent = message;
    el.querySelector('[data-text-retry]').hidden = !['load', 'failed'].includes(kind);
    el.querySelector('[data-text-reload]').hidden = kind !== 'conflict';
    el.querySelector('[data-text-overwrite]').hidden = kind !== 'conflict';
  }
  // The balls layer repeats the text exactly over the (transparent-text) area, with each task line's dash swapped for its ball.
  const esc = t => t.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  function paintMarks(p, s, ta) {
    const layer = p.focusMount?.querySelector('.agfocus-notes-marks'), inner = layer?.firstElementChild;
    if (!inner) return;
    const html = s.text.split('\n').map((line, i) => {
      const m = /^([ \t]*)-(.*)$/.exec(line);
      if (!m) return `<div class="agfocus-mline">${esc(line) || '<br>'}</div>`;
      const status = s.marks[i] || 'todo';
      return `<div class="agfocus-mline${ntStatusClosed(status) ? ' done' : ''}">${esc(m[1])}<span class="agfocus-dash">-${ntBallHtml(status, i, ' tabindex="-1"')}</span>${esc(m[2])}</div>`;
    }).join('');
    if (inner._html !== html) { inner.innerHTML = html; inner._html = html; }
    layer.style.width = ta.clientWidth + 'px';
    inner.style.transform = `translateY(${-ta.scrollTop}px)`;
    layer.classList.toggle('disabled', ta.disabled);
  }
  function paint(p) {
    const ta = p.focusMount?.querySelector('.agfocus-notes-input');
    if (ta && p.agentId) {
      const s = state(p.agentId);
      ta.disabled = !s.loaded;
      if (ta.value !== s.text) {
        const start = ta.selectionStart, end = ta.selectionEnd, focused = document.activeElement === ta;
        ta.value = s.text;
        if (focused) ta.setSelectionRange(Math.min(start, s.text.length), Math.min(end, s.text.length));
      }
      paintMarks(p, s, ta);
    }
    paintStatus(p);
  }

  function mount(p, board) {
    const host = board.querySelector('.agfocus-notes');
    if (!p.agentId || !host) return () => {};
    const s = state(p.agentId);
    s.views.add(p);
    host.innerHTML = `<div class="agfocus-notes-box"><textarea class="agfocus-notes-input" maxlength="${MAX_CHARS}" aria-label="Notes" spellcheck="true" disabled></textarea><div class="agfocus-notes-marks"><div></div></div></div>` +
      '<div class="agfocus-text-status" role="status" hidden><span data-text-message></span><button type="button" class="quiet" data-text-retry hidden>Retry</button><button type="button" class="quiet" data-text-reload hidden>Reload saved notes</button><button type="button" class="quiet" data-text-overwrite hidden>Save mine anyway</button></div>';
    const ta = host.querySelector('textarea'), status = host.querySelector('.agfocus-text-status'), layer = host.querySelector('.agfocus-notes-marks');
    ta.addEventListener('input', () => {
      if (!s.loaded) return;
      const before = s.text;
      s.text = ta.value;
      s.marks = remap(s.marks, before, s.text);
      if (s.status === 'saved') s.status = '';
      schedule(s);
    });
    ta.addEventListener('scroll', () => { layer.firstElementChild.style.transform = `translateY(${-ta.scrollTop}px)`; });
    const resize = new ResizeObserver(() => { layer.style.width = ta.clientWidth + 'px'; });
    resize.observe(ta);
    // A ball: click cycles open, white, in progress, done; right-click toggles blocked. Same as in the Journal.
    const setBall = (button, next) => {
      const i = Number(button.dataset.i);
      if (!s.loaded || !Number.isInteger(i)) return;
      const now = s.marks[i] || 'todo';
      s.marks[i] = next(now);
      if (s.status === 'saved') s.status = '';
      schedule(s);
    };
    layer.addEventListener('mousedown', e => { if (e.target.closest('.ntball')) e.preventDefault(); });
    layer.addEventListener('click', e => {
      const b = e.target.closest('.ntball');
      if (!b) return;
      e.preventDefault();
      setBall(b, ntNextStatus);
    });
    layer.addEventListener('contextmenu', e => {
      const b = e.target.closest('.ntball');
      if (!b) return;
      e.preventDefault();
      setBall(b, ntToggleBlockedStatus);
    });
    ta.addEventListener('blur', () => { if (s.loaded && !s.conflict && dirty(s)) save(s); });
    status.querySelector('[data-text-retry]').onclick = () => { if (s.loaded) save(s); else load(p); };
    status.querySelector('[data-text-reload]').onclick = () => load(p, 'adopt');
    status.querySelector('[data-text-overwrite]').onclick = async () => {
      await load(p, 'revision');
      if (s.loadError) return;
      s.conflict = false; s.status = ''; save(s);
    };
    paint(p);
    void load(p);
    return () => {
      s.views.delete(p); resize.disconnect(); host.replaceChildren();
      if (s.loaded && !s.conflict && dirty(s)) save(s);
    };
  }

  return {mount, load: p => load(p), paint, paintStatus, state, FIRST_TEXT};
})();
