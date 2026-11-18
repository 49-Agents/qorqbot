// Owner Focus notes: one text per agent pane, saved as the owner's private dashboard preference.
// The text surface is the Journal's lines editor (js/lines-editor.js): the same blocks, "-" task lines with status
// balls, bold, dividers, undo and paste. This file owns loading, saving and the conflict notice.
// No agent or model reads it. Until the owner saves anything at all, the area starts with a short sample text;
// once they change it (even to empty) it stays exactly as they left it.
// Drafts live in client state keyed by agent ID, so navigation never moves them to another agent or drops them.
const focusText = (() => {
  const DEBOUNCE_MS = 500, FIRST_TEXT = 'Your notes here';
  const states = new Map(), editors = new WeakMap();
  const path = id => `/root/agents/${encodeURIComponent(id)}/focus-layout`;
  // Earlier layouts could hold several separate blocks; they read as one text, in stored order.
  const joined = blocks => blocks.map(b => b.text).join('\n\n');
  // Notes saved by an earlier version kept a ball's state as an invisible token after its dash ("-", U+2063, state, U+2063).
  // They read as statuses now, keyed by the line's text like a Journal page, and the next save writes them that way.
  const TOKEN = /^([ \t]*-)\u2063(white|in_progress|done|blocked|cancelled)\u2063/;
  function untoken(text, statuses) {
    const kept = {...statuses};
    const lines = text.split('\n').map(line => {
      const m = TOKEN.exec(line);
      if (!m) return line;
      const clean = m[1] + line.slice(m[0].length), key = ntParseRaw(clean).display.trim();
      if (!(key in kept)) kept[key] = m[2];
      return clean;
    });
    return {lines, statuses: kept};
  }
  // What a save would store: the text, and the statuses of the task lines still in it.
  function content(lines, statuses) {
    const kept = {lines, statuses: {...statuses}};
    ntPruneStatuses(kept);
    return {text: lines.join('\n'), statuses: Object.fromEntries(Object.entries(kept.statuses).sort())};
  }
  const stamp = (lines, statuses) => JSON.stringify(content(lines, statuses));

  function state(agentId) {
    let s = states.get(agentId);
    if (!s) {
      s = {agentId, loaded: false, loadError: '', revision: 0, blockId: null, lines: [''], statuses: {}, baseline: stamp([''], {}), status: '', error: '',
        conflict: false, saving: false, timer: null, queue: null, loadSeq: 0, saveCount: 0, views: new Set()};
      states.set(agentId, s);
    }
    return s;
  }
  const dirty = s => s.loaded && stamp(s.lines, s.statuses) !== s.baseline;
  const live = s => [...s.views].filter(p => p.agentId === s.agentId && p.focusMount?.isConnected);
  function changed(s) { for (const p of live(s)) paint(p); }

  // ---- loading
  function adopt(s, d) {
    // Nothing ever saved for this agent: the very first start shows the sample text.
    const fresh = d.revision === 0 && !d.blocks.length;
    s.blockId = d.blocks[0]?.id || null;
    ({lines: s.lines, statuses: s.statuses} = untoken(fresh ? FIRST_TEXT : joined(d.blocks), d.statuses || {}));
    s.baseline = stamp(s.lines, s.statuses);
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
    const sent = content(s.lines, s.statuses);
    // An edit that was undone again leaves nothing to save; the notice still has to stop saying "Saving…".
    if (JSON.stringify(sent) === s.baseline) { changed(s); return true; }
    s.blockId ||= newRequestId();
    s.saving = true; s.status = 'saving'; s.error = ''; s.saveCount++; changed(s);
    let ok = false;
    try {
      const d = coreData(await post(path(s.agentId), {expected_revision: s.revision, blocks: [{id: s.blockId, text: sent.text, before_id: null, after_id: null}], statuses: sent.statuses}));
      s.revision = d.revision; s.baseline = stamp(joined(d.blocks).split('\n'), d.statuses || {}); s.blockId = d.blocks[0]?.id || s.blockId;
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
  function paint(p) {
    const editor = editors.get(p);
    if (editor && p.agentId) {
      const loaded = state(p.agentId).loaded;
      editor.doc.closest('.agfocus-notes-input').classList.toggle('loading', !loaded);
      editor.setEditable(loaded);
      editor.sync();
    }
    paintStatus(p);
  }

  function mount(p, board) {
    const host = board.querySelector('.agfocus-notes');
    if (!p.agentId || !host) return () => {};
    const s = state(p.agentId);
    s.views.add(p);
    host.innerHTML = '<div class="agfocus-notes-input ntlinewrap"><div class="ntlines"></div></div>' +
      '<div class="agfocus-text-status" role="status" hidden><span data-text-message></span><button type="button" class="quiet" data-text-retry hidden>Retry</button><button type="button" class="quiet" data-text-reload hidden>Reload saved notes</button><button type="button" class="quiet" data-text-overwrite hidden>Save mine anyway</button></div>';
    const paper = host.querySelector('.agfocus-notes-input'), status = host.querySelector('.agfocus-text-status');
    const editor = ntLinesEditor({
      state: s, host: paper.firstChild, paper, label: 'Notes',
      locked: () => !s.loaded,
      changed: () => { if (s.status === 'saved') s.status = ''; schedule(s); },
      save: () => save(s),
    });
    editors.set(p, editor);
    paper.onpointerdown = () => editor.rememberSelection();
    editor.doc.addEventListener('blur', () => { if (s.loaded && !s.conflict && dirty(s)) save(s); });
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
      s.views.delete(p); editors.delete(p); editor.destroy(); host.replaceChildren();
      if (s.loaded && !s.conflict && dirty(s)) save(s);
    };
  }

  return {mount, load: p => load(p), paint, paintStatus, state, FIRST_TEXT};
})();
