// The model and effort pill at the bottom left of an agent's chat composer. The lists are the client's own: the server asks
// Claude Code or Codex which models it offers and which effort levels each model takes (GET <chat>/models, see
// qorqut/managed_runtime/model_catalog.py). The choice is the agent's stored launch option (GET/POST <chat>/options); a value
// that is set also reaches an open session at once, so it applies from the next message (the reply's `applied`).
// `pickerModel` and `changeFor` are DOM-free (tested with node --test); `mount` draws the pill and its menu.
const chatModelPicker = (() => {
  const EFFORT_NAMES = {minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High', xhigh: 'Extra high', max: 'Max', ultra: 'Ultra'};
  const effortName = value => EFFORT_NAMES[value] || value;
  const shortLabel = label => String(label || '').replace(/\s*\(recommended\)\s*$/i, '');
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  // What the pill and the menu show. `options` is the options view, `catalog` is {client, models}.
  function pickerModel(options, catalog) {
    const models = Array.isArray(catalog?.models) ? catalog.models : [];
    if (!options || !models.length) return null;
    const stored = options.model_fits === false ? '' : (typeof options.model === 'string' ? options.model : '');
    // No stored model: the client's own default (Claude Code lists it as "default"; Codex marks one model `default`).
    const fallback = models.find(model => model.default) || models[0];
    const chosen = stored ? models.find(model => model.id === stored) || {id: stored, label: stored, description: '', efforts: [], default_effort: null}
      : fallback;
    const efforts = Array.isArray(chosen.efforts) ? chosen.efforts : [];
    const effort = efforts.includes(options.effort) ? options.effort : efforts.includes(chosen.default_effort) ? chosen.default_effort : '';
    const selectedId = stored || fallback.id;
    const row = model => ({id: model.id, label: model.label, description: model.description || '', selected: model.id === selectedId});
    return {
      pill: [shortLabel(chosen.label), effort ? effortName(effort) : ''].filter(Boolean).join(' · '),
      main: models.filter(model => !model.older).map(row),
      older: models.filter(model => model.older).map(row),
      efforts: efforts.map(value => ({value, label: effortName(value), selected: value === effort})),
    };
  }

  // The POST body for picking a model or an effort. Picking the client's default model clears the stored one; a stored effort the
  // new model does not take is cleared with it.
  function changeFor(kind, value, options, catalog) {
    if (kind === 'effort') return {effort: value};
    const models = Array.isArray(catalog?.models) ? catalog.models : [];
    const model = models.find(item => item.id === value);
    const body = {model: model && model.default && (catalog.client === 'claude' || !options?.model) ? null : value};
    if (options?.effort && model && !(model.efforts || []).includes(options.effort)) body.effort = null;
    return body;
  }

  const CHEVRON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

  // `base` is the agent's chat route; `label` names the agent. `read(path)` and `write(path, body)` return the response data.
  function mount({base, label, read, write, onError = () => {}, doc = globalThis.document}) {
    const el = doc.createElement('div');
    el.className = 'gchat-model-wrap'; el.hidden = true;
    el.innerHTML = `<button type="button" class="gchat-model" aria-haspopup="true" aria-expanded="false"></button><div class="gchat-model-menu" role="dialog" aria-label="Model and effort for ${escapeHtml(label)}" hidden></div>`;
    const pill = el.querySelector('.gchat-model'), menu = el.querySelector('.gchat-model-menu');
    let options = null, catalog = null, older = false, saving = false, note = '', disposed = false;

    function paint() {
      if (disposed) return;
      const view = pickerModel(options, catalog);
      el.hidden = !view;
      if (!view) { menu.hidden = true; return; }
      pill.innerHTML = `<span>${escapeHtml(view.pill)}</span>${CHEVRON}`;
      pill.title = `Model and effort for ${label}`;
      pill.disabled = saving;
      if (menu.hidden) return;
      const item = model => `<button type="button" class="gchat-model-item" data-model="${escapeHtml(model.id)}" aria-pressed="${model.selected}"><span>${escapeHtml(shortLabel(model.label))}</span>${model.description ? `<small>${escapeHtml(model.description)}</small>` : ''}</button>`;
      menu.innerHTML = `<p class="gchat-model-head">Model</p>${view.main.map(item).join('')}
        ${view.older.length ? `<button type="button" class="gchat-model-older" aria-expanded="${older}">${older ? 'Hide older versions' : `Older versions (${view.older.length})`}</button>${older ? view.older.map(item).join('') : ''}` : ''}
        ${view.efforts.length ? `<p class="gchat-model-head">Effort</p><div class="gchat-model-efforts">${view.efforts.map(effort => `<button type="button" data-effort="${escapeHtml(effort.value)}" aria-pressed="${effort.selected}">${escapeHtml(effort.label)}</button>`).join('')}</div>` : ''}
        ${note ? `<p class="gchat-model-note" role="status">${escapeHtml(note)}</p>` : ''}`;
      for (const button of menu.querySelectorAll('button')) button.disabled = saving;
    }
    async function load() {
      try {
        const [view, list] = await Promise.all([read(`${base}/options`), read(`${base}/models`)]);
        options = view; catalog = list;
      } catch (_) { options = null; catalog = null; }
      paint();
    }
    async function choose(kind, value) {
      if (saving || !options) return;
      saving = true; note = ''; paint();
      try {
        const view = await write(`${base}/options`, changeFor(kind, value, options, catalog));
        options = view;
        note = view?.applied ? 'Applies from the next message.' : `Applies the next time ${label} starts.`;
      } catch (error) {
        onError(error?.message || 'The model could not be changed. Try again.');
      } finally { saving = false; paint(); }
    }
    const open = () => { menu.hidden = false; note = ''; pill.setAttribute('aria-expanded', 'true'); paint(); if (!catalog?.models?.length) void load(); };
    const close = () => { menu.hidden = true; pill.setAttribute('aria-expanded', 'false'); };
    pill.addEventListener('click', () => (menu.hidden ? open() : close()));
    menu.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button || button.disabled) return;
      if (button.classList.contains('gchat-model-older')) { older = !older; paint(); return; }
      if (button.dataset.model !== undefined) void choose('model', button.dataset.model);
      else if (button.dataset.effort !== undefined) void choose('effort', button.dataset.effort);
    });
    const outside = event => { if (!menu.hidden && !el.contains(event.target)) close(); };
    const escape = event => { if (event.key === 'Escape' && !menu.hidden) { close(); pill.focus(); } };
    doc.addEventListener('pointerdown', outside); doc.addEventListener('keydown', escape);
    void load();
    return {el, reload: load, dispose() { disposed = true; doc.removeEventListener('pointerdown', outside); doc.removeEventListener('keydown', escape); el.remove(); }};
  }

  return {pickerModel, changeFor, mount, effortName};
})();
if (typeof module !== 'undefined') module.exports = chatModelPicker;
