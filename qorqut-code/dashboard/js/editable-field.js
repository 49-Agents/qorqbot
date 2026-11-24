'use strict';
// One click-to-edit text field for every record and Task view (style: css/editable-field.css).
// At rest a field reads as plain text (readonly, auto-height); a click or keyboard focus makes it editable, and a
// Save / Discard pair appears under it only while the value differs from the saved one. `live` fields (create forms,
// forms with their own Save button) share the look and auto-height but are always editable and own no actions.
function efGrow(el) {
  if (el.tagName !== 'TEXTAREA') return;
  el.style.height = 'auto';
  const content = el.scrollHeight;
  if (!content) return;
  const css = getComputedStyle(el);
  el.style.height = Math.ceil(content + (parseFloat(css.borderTopWidth) || 0) + (parseFloat(css.borderBottomWidth) || 0)) + 'px';
}
// Options are read at event time so a re-wired owner only replaces `api.opts`: {live, onChange(el), onEscape(el), onEnter(el)}.
function efField(el, opts = {}) {
  if (el._ef) { Object.assign(el._ef.opts, opts); return el._ef; }
  el.classList.add('editable-field');
  const api = el._ef = {
    opts: {...opts}, saved: el.value,
    dirty: () => el.value !== api.saved,
    grow: () => efGrow(el),
    edit() { if (!api.opts.live) { el.readOnly = false; el.classList.add('ef-editing'); } },
    rest() { if (!api.opts.live) { el.readOnly = true; el.classList.remove('ef-editing'); } },
    // A new saved value: shown now unless the owner is typing, in which case the typed text stays.
    set(value, {keepTyped = false} = {}) {
      api.saved = value;
      if (!keepTyped) el.value = value;
      if (!api.dirty() && el.ownerDocument.activeElement !== el) api.rest();
      api.grow();
    },
    reset() { el.value = api.saved; if (el.ownerDocument.activeElement === el) el.blur(); api.rest(); api.grow(); },
  };
  if (!api.opts.live) el.readOnly = true;
  el.addEventListener('focus', api.edit);
  el.addEventListener('click', api.edit);
  el.addEventListener('blur', () => { if (!api.dirty()) api.rest(); });
  el.addEventListener('input', () => { api.grow(); api.opts.onChange?.(el); });
  el.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !api.opts.live) {
      event.stopPropagation();
      if (api.dirty() && api.opts.onEscape) api.opts.onEscape(el); else { el.blur(); api.rest(); }
    } else if (event.key === 'Enter' && el.hasAttribute('data-ef-single')) {
      event.preventDefault(); api.opts.onEnter?.(el);
    }
  });
  if (el.tagName === 'TEXTAREA') {
    api.grow();
    if (typeof ResizeObserver === 'function') {
      let width = -1;
      new ResizeObserver(() => { if (el.offsetWidth !== width) { width = el.offsetWidth; api.grow(); } }).observe(el);
    }
  }
  return api;
}
function efMount(root, opts = {}) { for (const el of root.querySelectorAll('.editable-field')) efField(el, opts); }
// After values were set from code (a reset form): they are the saved values, shown at rest.
function efSettle(root) { for (const el of root.querySelectorAll('.editable-field')) { const api = el._ef; if (api) { api.saved = el.value; api.rest(); api.grow(); } } }
// The Save / Discard pair. `attrs` are the data attributes the owner finds the buttons by.
function efActionsHtml({save = 'data-ef-save', discard = 'data-ef-discard'} = {}) {
  return `<div class="ef-actions" hidden><button type="button" class="ef-btn ef-discard" ${discard}>Discard</button><button type="button" class="ef-btn ef-save" ${save}>Save</button></div>`;
}
function efActions(el, {isDirty, onSave, onDiscard}) {
  if (el._efActions) return el._efActions;
  const [discard, save] = el.querySelectorAll('button');
  if (onSave) save.onclick = onSave;
  if (onDiscard) discard.onclick = onDiscard;
  return el._efActions = {
    el, save, discard,
    sync() { el.hidden = !isDirty(); },
    busy(on) { save.disabled = on; discard.disabled = on; },
  };
}
