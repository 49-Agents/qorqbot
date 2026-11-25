// Lines editor: the one text surface behind the Journal (js/notes.js) and the Focus notes (js/focus-text.js).
// LF separates blocks. U+2028 stores an explicit soft break inside a block, displayed as LF.
// Enter starts a block; Shift+Enter continues it. Pasted physical lines remain separate blocks.
// A block that starts with "-" is a task line with a status ball; its status is kept by its trimmed text.
const NT_SOFT_BREAK = " ";
const NT_DIVIDER = "⸻";                                  // Ctrl/Cmd+Alt+= inserts this exact line
// The status ball rules and markup (cycle, labels, the "-" task-line rule) are js/status-ball.js, loaded first.

// Split a raw line into its task marker / invisible bold token / display text (lines-editor.tsx parseRaw).
function ntParseRaw(raw) {
  raw = raw || "";
  let rest = raw, marker = "", lead = "";
  const mk = raw.match(/^([ \t]*)-[ \t]?/);
  if (mk) { marker = mk[0]; lead = mk[1] || ""; rest = raw.slice(mk[0].length); }
  const meta = { bold: false };
  const mm = rest.match(/^⁣([^⁣]*)⁣ ?/);
  if (mm) { rest = rest.slice(mm[0].length); mm[1].split(",").forEach(f => { if (f === "b") meta.bold = true; }); }
  return { marker, meta, display: (lead + rest).replaceAll(NT_SOFT_BREAK, "\n") };
}
const ntBuildMetaToken = meta => meta.bold ? "⁣b⁣ " : "";
const ntDisp = (es, i) => ntParseRaw(es.lines[i]).display;
const ntIsRuleAt = (es, i) => (es.lines[i] || "").trim() === NT_DIVIDER;

const ntStatusKey = (es, i) => ntDisp(es, i).trim();
function ntLineStatus(es, i) {
  return es.statuses[ntStatusKey(es, i)] || "todo";
}
function ntSetStatus(es, i, status) {
  const key = ntStatusKey(es, i);
  if (status === "todo") delete es.statuses[key]; else es.statuses[key] = status;
}
// Drop status entries whose key no longer matches any current line's trimmed display text — called
// right before a save so a renamed/deleted task line's stale status doesn't accumulate in the map forever.
function ntPruneStatuses(es) {
  const live = new Set(es.lines.map((_, i) => ntStatusKey(es, i)));
  Object.keys(es.statuses).forEach(k => { if (!live.has(k)) delete es.statuses[k]; });
}

const ntBlockText = b => b.textContent.replace(/​/g, "");
function ntBlocks(es) {
  return es.lines.map((raw, i) => {
    if (ntIsRuleAt(es, i)) return { rule: true, task: false, bold: false, text: "" };
    const p = ntParseRaw(raw);
    return { rule: false, task: ntIsTaskLine(raw), bold: p.meta.bold, text: p.display };
  });
}
function ntComposeRaw(b) {
  if (b.rule) return NT_DIVIDER;
  const t = b.text.replaceAll("\n", NT_SOFT_BREAK), meta = ntBuildMetaToken({ bold: b.bold });
  if (b.task) return `- ${meta}${t}`;
  return t === "" ? "" : `${meta}${t}`;
}
function ntMakeBlock(es, i) {
  const el = document.createElement("div");
  el.dataset.i = i;
  if (ntIsRuleAt(es, i)) { el.className = "ntline ntrule"; el.contentEditable = "false"; el.innerHTML = '<div class="ntrulehr"></div>'; return el; }
  const raw = es.lines[i], task = ntIsTaskLine(raw), { meta, display } = ntParseRaw(raw);
  el.className = ["ntline", task && "nttask", meta.bold && "bold"].filter(Boolean).join(" ");
  if (task) {
    const status = ntLineStatus(es, i);
    if (ntStatusClosed(status)) el.classList.add("done");
  }
  if (display) el.append(display);
  if (display === "" || display.endsWith("\n")) el.append(document.createElement("br"));
  return el;
}
const ntOrdered = s => (s.a.i < s.f.i || (s.a.i === s.f.i && s.a.pos <= s.f.pos)) ? [s.a, s.f] : [s.f, s.a];

// Single-surface editor: the whole text is ONE contenteditable with a block <div> per line, so a
// drag, shift-click or Ctrl+A selects across tasks, dividers and prose alike. Typing inside a block is
// native and re-read into es.lines; anything that changes block structure (Enter, joins, edits that
// span blocks, paste, cut) is applied to the model and the surface is rebuilt.
//
// The host owns the text and its saving; the editor owns the surface, the selection and the undo history:
//   state    {lines: [raw line], statuses: {trimmed task text: status}}, edited in place
//   host     element that receives the text surface and the status-ball gutter
//   paper    optional surrounding element; a click on its blank space puts the caret at the end
//   overlay  optional element shown while the text is empty and unfocused
//   locked   () => true while the text must not be edited
//   changed  () => called after every edit, once state holds it
//   save     () => Ctrl/Cmd+S and Ctrl/Cmd+Enter
//   extra    optional {get(), focus(), set(value, focus)}: a host field (the Journal's title) that shares the history
function ntLinesEditor(o) {
  const es = o.state, locked = () => !!o.locked?.();
  const doc = document.createElement("div"), gutter = document.createElement("div");
  let history = [], historyIndex = 0, restoringHistory = false, shown = "";
  const stamp = () => JSON.stringify([es.lines, es.statuses]);

  function setEditable(on) {
    if (!on) { doc.contentEditable = "false"; return; }
    doc.contentEditable = "plaintext-only";
    if (doc.contentEditable !== "plaintext-only") doc.contentEditable = "true";
  }
  function docBlocks() {
    const out = [];
    for (const n of doc.childNodes) {
      if (n.nodeType === 3) { if (n.nodeValue) out.push({ rule: false, task: false, bold: false, text: n.nodeValue }); continue; }
      if (n.nodeType !== 1) continue;
      if (n.classList.contains("ntrule")) { out.push({ rule: true, task: false, bold: false, text: "" }); continue; }
      out.push({ rule: false, task: n.classList.contains("nttask"), bold: n.classList.contains("bold"), text: ntBlockText(n) });
    }
    return out.length ? out : [{ rule: false, task: false, bold: false, text: "" }];
  }

  // selection as {i: block index, pos: offset in that block's display text}
  function posOf(node, offset) {
    if (!doc.contains(node)) return null;
    const kids = doc.children;
    if (node === doc) {
      if (!kids.length) return { i: 0, pos: 0 };
      const i = Math.min(offset, kids.length - 1);
      return { i, pos: offset >= kids.length ? ntBlockText(kids[i]).length : 0 };
    }
    let b = node; while (b.parentNode !== doc) b = b.parentNode;
    const i = Array.prototype.indexOf.call(kids, b);
    if (b.classList.contains("ntrule")) return { i, pos: 0 };
    const r = document.createRange(); r.setStart(b, 0); r.setEnd(node, offset);
    return { i, pos: r.toString().replace(/​/g, "").length };
  }
  function selection() {
    const s = getSelection(); if (!s.rangeCount) return null;
    const a = posOf(s.anchorNode, s.anchorOffset), f = posOf(s.focusNode, s.focusOffset);
    return a && f ? { a, f } : null;
  }
  // Keep a triple-click inside this block: native paragraph selection can end at the
  // next block's start, making replacement consume the following line's boundary.
  function docMousedown(e) {
    if (e.button !== 0 || e.detail !== 3) return;
    const block = e.target.closest(".ntline");
    if (!block || block.classList.contains("ntrule")) return;
    e.preventDefault();
    const i = Number(block.dataset.i);
    doc.focus({ preventScroll: true });
    setSelection({ i, pos: 0 }, { i, pos: ntBlockText(block).length });
  }
  function domPoint(i, pos) {
    const b = doc.children[i];
    if (!b) return { node: doc, offset: doc.childNodes.length };
    if (b.classList.contains("ntrule")) return { node: doc, offset: i };
    const w = document.createTreeWalker(b, NodeFilter.SHOW_TEXT);
    let acc = 0, n;
    while ((n = w.nextNode())) { const len = n.nodeValue.length; if (pos <= acc + len) return { node: n, offset: pos - acc }; acc += len; }
    return { node: b, offset: 0 };
  }
  function setSelection(a, f) {
    const p = domPoint(a.i, a.pos), q = domPoint(f.i, f.pos);
    getSelection().setBaseAndExtent(p.node, p.offset, q.node, q.offset);
  }
  const clampPoint = p => { const i = Math.min(p.i, es.lines.length - 1); return { i, pos: Math.min(p.pos, ntBlockText(doc.children[i]).length) }; };
  function focusLine(index, pos) {
    if (!doc.children.length) return;
    const n = doc.children.length, isRule = k => doc.children[k].classList.contains("ntrule");
    let i = Math.max(0, Math.min(index, n - 1));
    while (i < n - 1 && isRule(i)) { i++; pos = 0; }
    while (i > 0 && isRule(i)) { i--; pos = Infinity; }
    if (isRule(i)) return;
    const p = Math.min(pos, ntBlockText(doc.children[i]).length);
    if (document.activeElement !== doc) doc.focus({ preventScroll: true });
    setSelection({ i, pos: p }, { i, pos: p });
    doc.children[i].scrollIntoView({ block: "nearest" });
  }

  function render() {
    setEditable(!locked());
    doc.replaceChildren(...es.lines.map((_, i) => ntMakeBlock(es, i)));
    renderBalls();
    paintOverlay();
    shown = stamp();
  }
  // Status balls live in a layer beside the editable text, not inside it, so they never sit in the
  // selectable flow (a non-editable node at a block start makes triple-click and range ends collapse).
  function renderBalls() {
    gutter.replaceChildren(...es.lines.flatMap((raw, i) => {
      if (!ntIsTaskLine(raw) || ntIsRuleAt(es, i)) return [];
      const w = document.createElement("span");
      w.className = "ntballwrap";
      w.innerHTML = ntBallHtml(ntLineStatus(es, i), i);
      const b = w.firstChild;
      b.onclick = e => { e.stopPropagation(); e.preventDefault(); cycleBall(i); };
      b.oncontextmenu = e => { e.preventDefault(); e.stopPropagation(); toggleBlocked(i); };
      return [w];
    }));
    placeBalls();
  }
  function placeBalls() {
    gutter.querySelectorAll(".ntball").forEach(b => {
      const blk = doc.children[Number(b.dataset.i)];
      if (blk) b.parentNode.style.top = `calc(${blk.offsetTop}px + 15.5px * 1.75 / 2)`;
    });
  }
  function paintOverlay() {
    if (!o.overlay) return;
    const empty = es.lines.length <= 1 && !es.lines[0];
    o.overlay.hidden = !(empty && document.activeElement !== doc);
  }
  // Every edit ends here: the history takes it, then the host hears about it.
  function changed() {
    shown = stamp();
    record();
    o.changed?.();
  }
  // Commit a structural change: rebuild the surface from new raw lines and put the caret back.
  function commit(newLines, focus) {
    es.lines = newLines;
    render();
    if (focus) focusLine(focus.index, focus.pos);
    changed();
  }
  // Replace the range [a,f] with `text` (each "\n" starts a new block; soft keeps it inside the block).
  function editRange(a, f, text, soft = false) {
    const bl = ntBlocks(es), first = bl[a.i], last = bl[f.i];
    const head = first.rule ? "" : first.text.slice(0, a.pos), tail = last.rule ? "" : last.text.slice(f.pos);
    const src = first.rule ? {} : first;
    const mk = t => ({ rule: false, task: !!src.task, bold: !!src.bold, text: t });
    const parts = soft ? [text] : text.split("\n");
    const mid = [];
    let pos;
    if (parts.length === 1) { mid.push(ntComposeRaw(mk(head + parts[0] + tail))); pos = head.length + parts[0].length; }
    else {
      mid.push(ntComposeRaw(mk(head + parts[0])));
      for (let k = 1; k < parts.length - 1; k++) mid.push(parts[k]);
      mid.push(parts[parts.length - 1] + tail.replaceAll("\n", NT_SOFT_BREAK));
      pos = parts[parts.length - 1].length;
    }
    commit([...bl.slice(0, a.i).map(ntComposeRaw), ...mid, ...bl.slice(f.i + 1).map(ntComposeRaw)],
      { index: a.i + (soft ? 0 : parts.length - 1), pos });
  }
  function rangeText(a, f) {
    const bl = ntBlocks(es), out = [];
    for (let i = a.i; i <= f.i; i++) {
      const b = bl[i], s = b.rule ? NT_DIVIDER : b.text;
      const from = i === a.i ? a.pos : 0, piece = s.slice(from, i === f.i ? f.pos : s.length);
      out.push(b.task && from === 0 ? "- " + piece : piece);
    }
    return out.join("\n");
  }

  function docBeforeInput(e) {
    const t = e.inputType;
    if (t === "historyUndo" || t === "historyRedo") { e.preventDefault(); undoRedo(t === "historyRedo"); return; }
    structuralInput(e);
    if (!e.defaultPrevented) rememberSelection();
  }
  function structuralInput(e) {
    const t = e.inputType;
    if (locked()) { e.preventDefault(); return; }
    if (e.isComposing) return;
    const s = selection(); if (!s) return;
    const [a, f] = ntOrdered(s), multi = a.i !== f.i, collapsed = !multi && a.pos === f.pos;
    const take = fn => { e.preventDefault(); rememberSelection(); fn(); };
    if (t === "insertParagraph") return take(() => editRange(a, f, "\n"));
    if (t === "insertLineBreak") return take(() => editRange(a, f, "\n", true));
    if (t === "insertFromDrop" || t === "deleteByDrag") return e.preventDefault();
    if (t.startsWith("delete")) {
      if (multi) return take(() => editRange(a, f, ""));
      if (!collapsed) return;
      const bl = ntBlocks(es), b = bl[a.i], n = bl.length;
      if (t.includes("Backward") && a.pos === 0) {
        if (b.task) return take(() => { b.task = false; commit(bl.map(ntComposeRaw), { index: a.i, pos: 0 }); });
        if (a.i === 0) return;
        return take(() => {
          const prev = bl[a.i - 1];
          if (prev.rule) { bl.splice(a.i - 1, 1); commit(bl.map(ntComposeRaw), { index: a.i - 1, pos: 0 }); return; }
          const at = prev.text.length; prev.text += b.text; bl.splice(a.i, 1);
          commit(bl.map(ntComposeRaw), { index: a.i - 1, pos: at });
        });
      }
      if (t.includes("Forward") && a.pos === b.text.length && a.i < n - 1) {
        return take(() => {
          const next = bl[a.i + 1];
          if (next.rule) { bl.splice(a.i + 1, 1); commit(bl.map(ntComposeRaw), { index: a.i, pos: b.text.length }); return; }
          const at = b.text.length; b.text += next.text; bl.splice(a.i + 1, 1);
          commit(bl.map(ntComposeRaw), { index: a.i, pos: at });
        });
      }
      return;
    }
    if (multi && t.startsWith("insert")) return take(() => editRange(a, f, e.data || ""));
  }
  // Native typing inside one block: re-read the surface; typing "-" at the start of a plain line makes it a task.
  function docInput(e) {
    const bl = docBlocks(), s = selection();
    if (s && s.a.i === s.f.i && !e.isComposing) {
      const b = bl[s.a.i];
      if (b && !b.rule && !b.task && b.text.startsWith("-")) {
        b.task = true; b.text = b.text.slice(1).replace(/^\s/, "");
        commit(bl.map(ntComposeRaw), { index: s.a.i, pos: Math.max(0, s.a.pos - 1) });
        return;
      }
    }
    const stray = [...doc.childNodes].some(n => n.nodeType !== 1 || !n.classList.contains("ntline"));
    if (stray) { commit(bl.map(ntComposeRaw), s ? { index: s.a.i, pos: s.a.pos } : null); return; }
    es.lines = bl.map(ntComposeRaw);
    placeBalls();
    paintOverlay();
    changed();
  }
  function docKeydown(e) {
    const mod = e.ctrlKey || e.metaKey, k = e.key;
    if (mod && !e.altKey && ["z", "y"].includes(k.toLowerCase())) {
      if (e.isComposing) return;
      e.preventDefault(); e.stopPropagation();
      undoRedo(k.toLowerCase() === "y" || e.shiftKey);
    }
    else if (k === "Enter" && !mod && !e.isComposing && !locked()) {
      // plaintext-only reports both Enter and Shift+Enter as insertLineBreak, so decide here
      const s = selection(); if (!s) return;
      e.preventDefault(); rememberSelection();
      const [a, f] = ntOrdered(s); editRange(a, f, "\n", e.shiftKey);
    }
    else if (mod && (k === "s" || k === "S" || k === "Enter")) { e.preventDefault(); o.save?.(); }
    else if (mod && (k === "b" || k === "B")) { e.preventDefault(); toggleBold(); }
    else if (mod && e.altKey && !e.isComposing && (k === "=" || k === "+")) { e.preventDefault(); insertDivider(); }
  }
  function docClipboard(e, cut) {
    const s = selection(); if (!s) return;
    const [a, f] = ntOrdered(s);
    if (a.i === f.i && a.pos === f.pos) return;
    e.preventDefault();
    e.clipboardData.setData("text/plain", rangeText(a, f));
    if (cut && !locked()) { rememberSelection(); editRange(a, f, ""); }
  }
  function docPaste(e) {
    e.preventDefault();
    if (locked()) return;
    const text = (e.clipboardData?.getData("text/plain") || "").replace(/\r\n?/g, "\n");
    const s = selection(); if (!text || !s) return;
    const [a, f] = ntOrdered(s);
    rememberSelection();
    editRange(a, f, text);
  }
  // Ctrl/Cmd+Alt+=: a blank block becomes the divider, otherwise the divider goes below the caret's block.
  function insertDivider() {
    const s = selection(); if (!s || locked()) return;
    const i = s.f.i, last = i === es.lines.length - 1;
    if (ntDisp(es, i).trim() === "") commit([...es.lines.slice(0, i), NT_DIVIDER, ...(last ? [""] : []), ...es.lines.slice(i + 1)], { index: i + 1, pos: 0 });
    else commit([...es.lines.slice(0, i + 1), NT_DIVIDER, ...(last ? [""] : []), ...es.lines.slice(i + 1)], { index: i + 2, pos: 0 });
  }

  // Ctrl/Cmd+B: bold for every text block in the selection (all bold -> unbold), stored as the invisible
  // "⁣b⁣ " token; patches classes in place so the caret and selection stay put.
  function toggleBold() {
    const sel = selection(); if (!sel || locked()) return;
    const [a, f] = ntOrdered(sel), bl = ntBlocks(es);
    const idx = []; for (let i = a.i; i <= f.i; i++) if (!bl[i].rule) idx.push(i);
    if (!idx.length) return;
    const on = !idx.every(i => bl[i].bold);
    idx.forEach(i => { bl[i].bold = on; blockClass(i, "bold", on); });
    es.lines = bl.map(ntComposeRaw);
    changed();
  }
  const blockClass = (i, cls, on) => doc.children[i]?.classList.toggle(cls, on);
  // Left-click cycle / right-click blocked-toggle on a task line's gutter ball — patches just that ball
  // (and its row's struck-through state) rather than a full rebuild.
  function cycleBall(i) {
    if (locked()) return;
    ntSetStatus(es, i, ntNextStatus(ntLineStatus(es, i)));
    patchBall(i);
    changed();
  }
  function toggleBlocked(i) {
    if (locked()) return;
    ntSetStatus(es, i, ntToggleBlockedStatus(ntLineStatus(es, i)));
    patchBall(i);
    changed();
  }
  function patchBall(i) {
    const status = ntLineStatus(es, i);
    const closed = ntStatusClosed(status);
    const btn = gutter.querySelector(`.ntball[data-i="${i}"]`); if (!btn) return;
    btn.className = `ntball ntb-${status}`;
    btn.setAttribute("aria-label", ntBallLabel(status));
    blockClass(i, "done", closed);
  }

  // History belongs to the open text, not the surface rebuilt by structural edits.
  function snapshot() {
    let focus = o.extra?.focus() || null;
    if (!focus && document.activeElement === doc) { const sel = selection(); if (sel) focus = { a: sel.a, f: sel.f }; }
    return { extra: o.extra?.get(), lines: es.lines.slice(), statuses: { ...es.statuses }, focus };
  }
  const resetHistory = () => { history = [snapshot()]; historyIndex = 0; };
  function rememberSelection() {
    history[historyIndex].focus = snapshot().focus;
  }
  function record() {
    if (restoringHistory) return;
    const next = snapshot(), last = history[historyIndex];
    if (JSON.stringify([next.extra, next.lines, next.statuses]) === JSON.stringify([last.extra, last.lines, last.statuses])) return;
    history.splice(historyIndex + 1);
    history.push(next);
    if (history.length > 200) history.shift();
    historyIndex = history.length - 1;
  }
  function undoRedo(redo) {
    if (locked()) return;
    const index = historyIndex + (redo ? 1 : -1);
    if (index < 0 || index >= history.length) return;
    const past = history[index];
    historyIndex = index; restoringHistory = true;
    es.lines = past.lines.slice(); es.statuses = { ...past.statuses };
    render();
    const f = past.focus;
    if (o.extra?.set(past.extra, f)) { /* the host field took the caret */ }
    else if (f?.a) { doc.focus({ preventScroll: true }); setSelection(clampPoint(f.a), clampPoint(f.f)); }
    else focusLine(0, 0);
    changed(); restoringHistory = false;
  }
  // The host replaced state behind the surface (a reload, a discard, another view of the same text): show it,
  // keep the caret where it was, and start the history again from this text.
  function sync() {
    if (shown === stamp()) return;
    const sel = document.activeElement === doc ? selection() : null;
    render();
    if (sel) setSelection(clampPoint(sel.a), clampPoint(sel.f));
    resetHistory();
  }

  doc.className = "ntdoc"; gutter.className = "ntgutter";
  if (o.docId) doc.id = o.docId;
  if (o.gutterId) gutter.id = o.gutterId;
  doc.setAttribute("role", "textbox"); doc.setAttribute("aria-multiline", "true"); doc.setAttribute("aria-label", o.label || "Note");
  doc.spellcheck = false;
  doc.onbeforeinput = docBeforeInput; doc.oninput = docInput; doc.onkeydown = docKeydown;
  doc.oncopy = e => docClipboard(e, false); doc.oncut = e => docClipboard(e, true); doc.onpaste = docPaste;
  doc.ondrop = e => e.preventDefault();
  doc.onmousedown = docMousedown;
  doc.onfocus = () => paintOverlay();
  doc.onblur = () => setTimeout(paintOverlay, 0);
  gutter.onmousedown = e => e.preventDefault();
  const resizeObserver = new ResizeObserver(() => placeBalls());
  resizeObserver.observe(doc);
  o.host.replaceChildren(doc, gutter);
  // click anywhere in the paper's empty space (not on a line/ball) focuses the end of the last line.
  // A drag that selects text and ends beside it (over the ball gutter) also fires a click on the paper;
  // that selection is kept, not replaced by a caret at the end that scrolls the text away.
  let pressed = null;
  if (o.paper) o.paper.addEventListener("mousedown", e => { pressed = e.target; }, true);
  if (o.paper) o.paper.addEventListener("click", e => {
    if (doc.contains(e.target) || e.target.closest(".ntball")) return;
    // Pressed on one element and released on another: a drag, whose click lands on their shared parent.
    if (pressed !== e.target) return;
    const s = getSelection();
    if (s.rangeCount && !s.isCollapsed && doc.contains(s.getRangeAt(0).commonAncestorContainer)) return;
    focusLine(es.lines.length - 1, Infinity);
  });
  render();
  resetHistory();

  // The caret while this text has it, and putting a remembered one back without scrolling the text.
  const caret = () => document.activeElement === doc ? selection() : null;
  function placeCaret(sel) {
    if (!sel?.a || !sel?.f || !doc.children.length) return false;
    doc.focus({ preventScroll: true });
    setSelection(clampPoint(sel.a), clampPoint(sel.f));
    return true;
  }

  return { doc, render, sync, setEditable, focusLine, placeBalls, record, rememberSelection, undoRedo, caret, placeCaret, destroy: () => resizeObserver.disconnect() };
}
