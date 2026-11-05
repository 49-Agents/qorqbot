// Owner Focus text blocks: plain multiline notes and spacing saved per agent, outside operational Focus records.
// Drafts live in client state keyed by agent ID, so navigation never moves them to another agent or drops them.
const focusText = (() => {
  const DEBOUNCE_MS = 500, MAX_BLOCKS = 200, MAX_CHARS = 8000;
  const states = new Map();
  const strip = b => ({id: b.id, text: b.text, before_id: b.before_id ?? null, after_id: b.after_id ?? null});
  const json = blocks => JSON.stringify(blocks.map(strip));
  const flagsKey = p => `${+!!p.focusIncludearchived}${+!!p.focusIncludedeferred}`;
  const path = (id, p) => `/root/agents/${encodeURIComponent(id)}/focus-layout` + (p ? `?include_archived=${+!!p.focusIncludearchived}&include_deferred=${+!!p.focusIncludedeferred}` : '');
  const idOf = node => node.dataset.cardId || node.dataset.textId;

  function state(agentId) {
    let s = states.get(agentId);
    if (!s) {
      s = {agentId, loaded: false, loadError: '', revision: 0, blocks: [], serverJson: '[]', cardIds: [], status: '', error: '', conflict: false,
        timer: null, queue: null, loadSeq: 0, saveCount: 0, version: 0, views: new Set(), focusId: null};
      states.set(agentId, s);
    }
    return s;
  }
  const dirty = s => json(s.blocks) !== s.serverJson;
  const live = s => [...s.views].filter(p => p.agentId === s.agentId && p.focusMount?.isConnected);
  function changed(s) { s.version++; for (const p of live(s)) agfocusPaint(p); }
  function statusChanged(s) { for (const p of live(s)) paintStatus(p); }

  // ---- loading: layout plus the complete ordered card IDs that distinguish an off-page anchor from a missing one
  async function load(p, mode = 'merge') {
    if (!p.agentId) return;
    const s = state(p.agentId), seq = ++s.loadSeq, saves = s.saveCount;
    try {
      const d = coreData(await api(path(p.agentId, p)));
      if (seq !== s.loadSeq) return;
      if (!Array.isArray(d.blocks) || !Array.isArray(d.card_ids) || !Number.isInteger(d.revision)) throw new Error('Invalid notes response');
      s.loadError = '';
      if (s.cardIds.join() !== d.card_ids.join()) { s.cardIds = d.card_ids; s.version++; }
      if (mode === 'revision') s.revision = d.revision;
      else if (mode === 'adopt' || (!dirty(s) && !s.queue?.pending && saves === s.saveCount)) {
        const blocks = d.blocks.map(strip);
        if (json(blocks) !== json(s.blocks)) s.version++;
        s.blocks = blocks; s.serverJson = json(blocks); s.revision = d.revision;
        if (mode === 'adopt') { s.conflict = false; s.status = ''; s.error = ''; }
      }
      s.loaded = true;
    } catch (error) {
      if (seq !== s.loadSeq) return;
      s.loadError = error.message || 'Notes could not be read.';
    }
    changed(s); statusChanged(s);
  }

  // ---- saving: serial, debounced, one whole-layout write that never carries a draft to another agent
  function schedule(s, delay = DEBOUNCE_MS) {
    clearTimeout(s.timer);
    s.timer = setTimeout(() => save(s), delay);
    statusChanged(s);
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
    const sent = json(s.blocks);
    if (sent === s.serverJson) return true;
    s.saving = true; s.status = 'saving'; s.error = ''; s.saveCount++; statusChanged(s);
    let ok = false;
    try {
      const d = coreData(await post(path(s.agentId), {expected_revision: s.revision, blocks: JSON.parse(sent)}));
      s.revision = d.revision; s.serverJson = json(d.blocks);
      // Only canonical output for what was sent replaces local state; newer input stays untouched.
      if (json(s.blocks) === sent) { s.blocks = d.blocks.map(strip); s.version++; }
      s.status = 'saved'; ok = true;
    } catch (error) {
      s.error = error.message || 'The notes were not saved.';
      if (error.code === 'revision_conflict') { s.conflict = true; s.status = 'conflict'; } else s.status = 'failed';
    }
    s.saving = false;
    changed(s); statusChanged(s);
    return ok;
  }
  const label = s => s.conflict ? 'conflict' : s.status === 'failed' ? 'failed' : (s.saving || s.timer || dirty(s)) && s.loaded ? 'saving' : s.status;

  // ---- status line
  function paintStatus(p) {
    const el = p.focusMount?.querySelector('.agfocus-text-status');
    if (!el || !p.agentId) return;
    const s = state(p.agentId), kind = !s.loaded ? (s.loadError ? 'load' : '') : label(s);
    const message = {load: "Notes couldn't load. " + s.loadError, conflict: 'Notes changed elsewhere. Your edits are kept here.', failed: "Couldn't save notes. " + s.error, saving: 'Saving…', saved: 'Saved'}[kind] || '';
    el.hidden = !message || (kind === 'saved' && !s.blocks.length);
    el.dataset.state = kind;
    const text = el.querySelector('[data-text-message]');
    if (text.textContent !== message) text.textContent = message;
    el.querySelector('[data-text-retry]').hidden = !['load', 'failed'].includes(kind);
    el.querySelector('[data-text-reload]').hidden = kind !== 'conflict';
    el.querySelector('[data-text-overwrite]').hidden = kind !== 'conflict';
  }

  // ---- resize: plain textareas grow to the full content, trailing newlines included
  function resize(ta) {
    if (!ta.isConnected || !ta.offsetParent) return;
    ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px';
  }

  // ---- DOM nodes
  function makeNode(p, s, b) {
    const node = document.createElement('div');
    node.className = 'agfocus-text'; node.dataset.textId = b.id;
    node.innerHTML = '<button type="button" class="quiet agfocus-text-grip" aria-label="Move text block" aria-describedby="agfocus-move-instructions"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg></button>' +
      `<textarea class="agfocus-text-input" rows="1" maxlength="${MAX_CHARS}" aria-label="Pinned text" placeholder="Type a heading or note" spellcheck="true"></textarea>` +
      '<button type="button" class="quiet agfocus-text-delete" aria-label="Delete text block"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>';
    const ta = node.querySelector('textarea');
    ta.addEventListener('input', () => {
      const block = s.blocks.find(x => x.id === b.id);
      if (!block) return;
      block.text = ta.value; resize(ta);
      if (s.status === 'saved') s.status = '';
      schedule(s);
    });
    ta.addEventListener('blur', () => { if (s.loaded && !s.conflict && dirty(s)) save(s); });
    node.querySelector('.agfocus-text-delete').addEventListener('click', () => {
      s.blocks = s.blocks.filter(x => x.id !== b.id); s.status = '';
      changed(s); save(s); statusChanged(s);
    });
    node._state = s;
    return node;
  }
  function setValue(ta, text) {
    if (ta.value === text) return;
    const start = ta.selectionStart, end = ta.selectionEnd, focused = document.activeElement === ta;
    ta.value = text;
    if (focused) ta.setSelectionRange(Math.min(start, text.length), Math.min(end, text.length));
  }

  // ---- placement
  function placement(p, s, rootIds) {
    const onPage = new Set(rootIds), pageIds = new Set((p.focus?.items || []).map(item => item.id)), known = new Set([...s.cardIds, ...rootIds]);
    const exists = id => !!id && known.has(id) && (onPage.has(id) || !pageIds.has(id));
    const before = new Map(), after = new Map(), end = [], first = (p.focusOffset || 0) === 0;
    const add = (map, id, b) => { if (!map.has(id)) map.set(id, []); map.get(id).push(b); };
    for (const b of s.blocks) {
      if (exists(b.before_id)) { if (onPage.has(b.before_id)) add(before, b.before_id, b); }
      else if (exists(b.after_id)) { if (onPage.has(b.after_id)) add(after, b.after_id, b); }
      else if (first) end.push(b);
    }
    return {before, after, end};
  }

  function reconcile(p, host, list) {
    const s = state(p.agentId);
    p.focusTextNodes ||= new Map();
    if (p.focusTextState !== s) {
      for (const node of p.focusTextNodes.values()) node.remove();
      p.focusTextNodes.clear(); p.focusTextState = s;
    }
    const cards = [...list.children].filter(node => node.dataset.cardId), groups = [];
    for (const node of cards) {
      if (node._item?._workParent && groups.length) groups.at(-1).push(node); else groups.push([node]);
    }
    const place = s.loaded ? placement(p, s, groups.map(g => g[0].dataset.cardId)) : {before: new Map(), after: new Map(), end: []};
    const wanted = new Set(), desired = [];
    const push = b => {
      let node = p.focusTextNodes.get(b.id);
      if (!node) { node = makeNode(p, s, b); p.focusTextNodes.set(b.id, node); }
      setValue(node.querySelector('textarea'), b.text);
      wanted.add(b.id); desired.push(node);
    };
    for (const group of groups) {
      const id = group[0].dataset.cardId;
      (place.before.get(id) || []).forEach(push);
      desired.push(...group);
      (place.after.get(id) || []).forEach(push);
    }
    place.end.forEach(push);
    for (const [id, node] of [...p.focusTextNodes]) if (!wanted.has(id)) { node.remove(); p.focusTextNodes.delete(id); }
    const active = document.activeElement, selection = active?.selectionStart != null ? [active.selectionStart, active.selectionEnd] : null;
    desired.forEach((node, i) => { if (list.children[i] !== node) list.insertBefore(node, list.children[i] || null); });
    if (active && active !== document.activeElement && active.isConnected && host.contains(active)) {
      active.focus({preventScroll: true});
      if (selection) active.setSelectionRange(...selection);
    }
    for (const node of p.focusTextNodes.values()) resize(node.querySelector('textarea'));
    if (s.focusId) {
      const node = p.focusTextNodes.get(s.focusId);
      if (node?.offsetParent) { s.focusId = null; node.querySelector('textarea').focus(); node.scrollIntoView({block: 'nearest'}); }
    }
    paintStatus(p);
  }

  // ---- ordering: root-level sequence of card groups and text blocks, read from the DOM
  function sequence(list) {
    const seq = [];
    for (const node of list.children) {
      if (node.dataset.textId) seq.push({kind: 'text', id: node.dataset.textId, nodes: [node]});
      else if (node.dataset.cardId) {
        if (node._item?._workParent && seq.at(-1)?.kind === 'card') seq.at(-1).nodes.push(node);
        else seq.push({kind: 'card', id: node.dataset.cardId, nodes: [node]});
      }
    }
    return seq;
  }
  const placed = (blocks, order, anchors) => {
    const rendered = new Set(order), byId = new Map(blocks.map(b => [b.id, b]));
    const queue = order.filter(id => byId.has(id)).map(id => ({...byId.get(id), ...anchors.get(id)}));
    let k = 0;
    return blocks.map(b => rendered.has(b.id) ? queue[k++] : b);
  };
  // Intended interleaved order after moving one root entry; text anchors derive from neighbouring root cards.
  function plan(p, movedId, anchorId, side) {
    const s = state(p.agentId), seq = sequence($('.agfocus-items', p.focusMount));
    const from = seq.find(e => e.id === movedId), rest = seq.filter(e => e !== from), at = rest.findIndex(e => e.id === anchorId);
    if (!from || at < 0 || (seq.some(e => e.kind === 'text') && !s.loaded)) return null;
    const next = [...rest]; next.splice(at + (side === 'after_id' ? 1 : 0), 0, from);
    const oldCards = seq.filter(e => e.kind === 'card').map(e => e.id), newCards = next.filter(e => e.kind === 'card').map(e => e.id);
    const anchors = new Map(), order = next.filter(e => e.kind === 'text').map(e => e.id);
    next.forEach((e, i) => {
      if (e.kind !== 'text') return;
      const prev = next.slice(0, i).reverse().find(x => x.kind === 'card')?.id ?? null, following = next.slice(i + 1).find(x => x.kind === 'card')?.id ?? null;
      let after = prev;
      if (!after && following) { const k = s.cardIds.indexOf(following); if (k > 0) after = s.cardIds[k - 1]; }
      anchors.set(e.id, {before_id: following, after_id: after});
    });
    return {
      cardsChanged: oldCards.some((id, i) => id !== newCards[i]), newCards,
      blocksChanged: json(placed(s.blocks, order, anchors)) !== json(s.blocks),
      apply: () => { s.blocks = placed(s.blocks, order, anchors); s.status = ''; changed(s); return save(s); },
    };
  }

  // ---- adding: a new block goes atop the visible page with its neighbouring root-card anchors
  function ready(p) { const s = p.agentId && state(p.agentId); return !!s && s.loaded && s.blocks.length < MAX_BLOCKS; }
  function unavailable(p) {
    const s = state(p.agentId);
    if (!s.loaded) return s.loadError ? "Notes couldn't load. Retry from the Pinned header." : 'Notes are still loading.';
    return s.blocks.length >= MAX_BLOCKS ? `A Pinned layout holds at most ${MAX_BLOCKS} text blocks.` : '';
  }
  function add(p, text) {
    if (!ready(p) || text.length > MAX_CHARS) return false;
    const s = state(p.agentId);
    if (p.focusDetail) p.focusDetail.back();
    const first = sequence($('.agfocus-items', p.focusMount)).find(e => e.kind === 'card')?.id ?? null;
    const k = first ? s.cardIds.indexOf(first) : -1;
    const block = {id: newRequestId(), text, before_id: first, after_id: k > 0 ? s.cardIds[k - 1] : null};
    s.blocks = [block, ...s.blocks]; s.focusId = block.id; s.status = '';
    changed(s); schedule(s);
    return true;
  }

  function mount(p, board) {
    if (!p.agentId) return () => {};
    const s = state(p.agentId);
    s.views.add(p);
    const status = document.createElement('div');
    status.className = 'agfocus-text-status'; status.hidden = true; status.setAttribute('role', 'status');
    status.innerHTML = '<span data-text-message></span><button type="button" class="quiet" data-text-retry hidden>Retry</button><button type="button" class="quiet" data-text-reload hidden>Reload saved notes</button><button type="button" class="quiet" data-text-overwrite hidden>Save mine anyway</button>';
    board.querySelector('.agfocus-layout').before(status);
    status.querySelector('[data-text-retry]').onclick = () => { if (s.loaded) save(s); else load(p); };
    status.querySelector('[data-text-reload]').onclick = () => load(p, 'adopt');
    status.querySelector('[data-text-overwrite]').onclick = async () => {
      await load(p, 'revision');
      if (s.loadError) return;
      s.conflict = false; s.status = ''; save(s);
    };
    const list = board.querySelector('.agfocus-items');
    const observer = new ResizeObserver(() => { for (const node of p.focusTextNodes?.values() || []) resize(node.querySelector('textarea')); });
    observer.observe(list);
    void load(p);
    return () => {
      observer.disconnect(); s.views.delete(p); status.remove();
      for (const node of p.focusTextNodes?.values() || []) node.remove();
      p.focusTextNodes?.clear(); p.focusTextState = null;
      if (s.loaded && !s.conflict && dirty(s)) save(s);
    };
  }

  const signature = p => { const s = p.agentId && states.get(p.agentId); return s ? `${s.version}:${s.loaded}` : ''; };
  const hasBlocks = p => !!(p.agentId && states.get(p.agentId)?.blocks.length);
  const disabled = (p, node) => !!p.focusOrderPending || !!p.focusMoveSubmission || !node.isConnected;
  return {mount, load: p => load(p), reconcile, signature, hasBlocks, paintStatus, ready, unavailable, add, plan, idOf, disabled, state};
})();
