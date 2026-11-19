// Which client, model and effort each agent uses: one row per agent in Settings > Agents, so the owner never has to type
// claude or codex anywhere. Server (qorqut/managed_runtime/options.py), owner only:
//   GET  /root/agents/<id>/chat/options -> {client, model, effort, effective_client, clients: {claude: {ready}, codex: {ready}}, session_open}
//   POST the same path with any of {client, model, effort} (null clears) -> the same view.
// A change of client is refused with `session_open` while the agent's session is open. Model and effort apply at the next start.
// `body`, `refusal`, `rowModel` and `controller` are DOM-free (tested with node --test); `mount` draws the rows.
const agentOptions = (() => {
  const CLIENTS = ['claude', 'codex'];
  const NAMES = {claude: 'Claude Code', codex: 'Codex'};
  const EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
  // Names the codebase already uses; any other name can be typed.
  const SUGGESTIONS = {claude: ['opus', 'sonnet', 'haiku'], codex: ['gpt-6-luna']};
  const MODEL_MAX = 100;
  const MODEL_OK = /^[A-Za-z0-9._:/\[\]][A-Za-z0-9._:/\[\]-]*$/;
  const NOTE = 'Model and effort apply the next time the agent’s session starts. A change of client needs the session ended first.';
  const base = id => `/root/agents/${encodeURIComponent(id)}/chat/options`;
  const defaultRead = async id => coreData(await api(base(id)));
  const defaultWrite = async (id, change) => coreData(await post(base(id), change));

  const text = value => typeof value === 'string' ? value.trim() : '';
  // The server's view in the shape a row shows. Empty means default.
  function normalize(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    const ready = name => !!(raw.clients && raw.clients[name] && raw.clients[name].ready);
    return {client: CLIENTS.includes(raw.client) ? raw.client : '', model: text(raw.model), effort: EFFORTS.includes(raw.effort) ? raw.effort : '',
      effective: CLIENTS.includes(raw.effective_client) ? raw.effective_client : '', ready: {claude: ready('claude'), codex: ready('codex')},
      open: raw.session_open === true};
  }
  // The request for one changed field; an empty value clears it. Returns {error} for a value the server would refuse.
  function body(field, value) {
    const v = text(value);
    if (field === 'client') return CLIENTS.includes(v) || !v ? {body: {client: v || null}} : {error: 'Choose Claude Code or Codex.'};
    if (field === 'effort') return EFFORTS.includes(v) || !v ? {body: {effort: v || null}} : {error: 'Choose one of the listed efforts.'};
    if (field === 'model') {
      if (v.length > MODEL_MAX) return {error: `A model name has at most ${MODEL_MAX} characters.`};
      if (v && !MODEL_OK.test(v)) return {error: 'A model name uses letters, digits and . _ : / [ ] - only.'};
      return {body: {model: v || null}};
    }
    return {error: 'Unknown setting.'};
  }
  // What the owner reads when a change does not go through.
  function refusal(error, field = '') {
    const code = String((error && error.code) || '');
    if (code === 'session_open') return 'This agent’s session is open. End the session in its Chat first, then change the client.';
    if (error && error.httpStatus === 404) return 'This setting isn’t available for this agent.';
    const message = error && typeof error.message === 'string' ? error.message.trim() : '';
    return message || `The ${field || 'setting'} could not be saved. Try again.`;
  }
  // The row: the client choices ({value, label, ready}), the shown values and the line under it.
  function rowModel(view) {
    const v = normalize(view);
    const choices = [{value: '', label: v.effective ? `Automatic (${NAMES[v.effective]})` : 'Automatic'}].concat(CLIENTS.map(name =>
      ({value: name, label: v.ready[name] ? NAMES[name] : `${NAMES[name]} (not connected)`, ready: v.ready[name]})));
    const using = v.client || v.effective;
    const notice = using && !v.ready[using] ? `${NAMES[using]} isn’t connected yet. Connect it under Claude Code and Codex above.` : '';
    return {client: v.client, model: v.model, effort: v.effort, choices, suggestions: SUGGESTIONS[using] || [], notice, open: v.open};
  }
  const isMissing = error => !!error && (error.httpStatus === 404 || error.httpStatus === 405);

  // Holds one row per agent and sends changes. No DOM: `onChange(state)` repaints. `agents` is [{agent_id, name}].
  function controller({agents, read = defaultRead, write = defaultWrite, onChange = () => {}} = {}) {
    const state = {loading: true, unavailable: false, rows: agents.map(a => ({id: a.agent_id, name: a.name, view: null, error: '', saved: false, saving: false, missing: false}))};
    const emit = () => onChange(state);
    const rowOf = id => state.rows.find(r => r.id === id);
    async function load() {
      emit();
      await Promise.all(state.rows.map(async row => {
        try { row.view = await read(row.id); row.error = ''; }
        catch (error) { if (isMissing(error)) row.missing = true; else row.error = refusal(error); }
      }));
      state.loading = false;
      // The server may not have the routes yet: the section says so once instead of a row of errors.
      state.unavailable = state.rows.length > 0 && state.rows.every(r => r.missing);
      emit();
    }
    // One field of one agent. Resolves true when it was saved.
    async function change(id, field, value) {
      const row = rowOf(id); if (!row || row.saving) return false;
      const request = body(field, value);
      row.saved = false;
      // Leaving a field as it was sends nothing.
      if (!request.error && row.view && (normalize(row.view)[field] || null) === Object.values(request.body)[0]) return true;
      if (request.error) { row.error = request.error; emit(); return false; }
      row.saving = true; row.error = ''; emit();
      try { row.view = await write(id, request.body); row.saved = true; return true; }
      catch (error) { row.error = refusal(error, field); return false; }
      finally { row.saving = false; emit(); }
    }
    return {state, load, change};
  }

  const optionHtml = (value, label, selected) => `<option value="${esc(value)}"${selected ? ' selected' : ''}>${esc(label)}</option>`;
  function rowHtml(row, index) {
    const label = `${esc(row.name)}`;
    if (row.missing) return `<div class="ao-main"><div class="ao-name">${label}</div><p class="ao-note">Not available for this agent.</p></div>`;
    if (!row.view) return `<div class="ao-main"><div class="ao-name">${label}</div><p class="ao-note ${row.error ? 'is-error' : ''}" ${row.error ? 'role="alert"' : ''}>${esc(row.error || 'Reading…')}</p></div>`;
    const m = rowModel(row.view), id = `ao-${index}`;
    return `<div class="ao-main"><div class="ao-name">${label}</div>
      <p class="ao-note${row.error ? ' is-error' : ''}" data-ao-note ${row.error ? 'role="alert"' : 'role="status"'}>${esc(row.error || (row.saved ? 'Saved.' : m.notice))}</p></div>
      <div class="ao-fields">
        <label class="ao-field"><span>Client</span><select data-ao-field="client" ${row.saving ? 'disabled' : ''}>${m.choices.map(c => optionHtml(c.value, c.label, c.value === m.client)).join('')}</select></label>
        <label class="ao-field"><span>Model</span><input type="text" data-ao-field="model" list="${id}-models" value="${esc(m.model)}" placeholder="default" maxlength="${MODEL_MAX}" autocomplete="off" spellcheck="false" ${row.saving ? 'disabled' : ''}>
          <datalist id="${id}-models">${m.suggestions.map(name => `<option value="${esc(name)}"></option>`).join('')}</datalist></label>
        <label class="ao-field"><span>Effort</span><select data-ao-field="effort" ${row.saving ? 'disabled' : ''}>${optionHtml('', 'Default', !m.effort)}${EFFORTS.map(name => optionHtml(name, name, name === m.effort)).join('')}</select></label>
      </div>`;
  }

  // Draws the rows into `container`. Returns {root, load}; `load()` reads every agent again.
  function mount(container, agents, options = {}) {
    const root = document.createElement('div');
    root.className = 'ao-list';
    container.replaceChildren(root);
    const ctl = controller({agents, read: options.read, write: options.write, onChange: paint});
    let seen = '';
    function paint(state) {
      if (!root.isConnected) return;
      if (state.unavailable) { root.innerHTML = '<p class="ao-empty">Choosing a client, model and effort for each agent isn’t available in this version yet.</p>'; seen = ''; root.dispatchEvent(new CustomEvent('agent-options-painted', {bubbles: true})); return; }
      if (!state.rows.length) { root.innerHTML = '<p class="ao-empty">No agents yet.</p>'; return; }
      // A row is redrawn only when what it shows changes, so a field being typed in keeps its focus.
      const signature = JSON.stringify(state.rows.map(r => [r.id, r.view, r.error, r.saved, r.saving, r.missing]));
      if (signature === seen) return;
      seen = signature;
      const active = document.activeElement, keep = active && root.contains(active) ? {row: active.closest('[data-ao-row]')?.dataset.aoRow, field: active.dataset.aoField} : null;
      root.innerHTML = state.rows.map((row, i) => `<div class="ao-row" data-ao-row="${esc(row.id)}" data-search="${esc(`${row.name} client model effort claude codex agent`.toLowerCase())}">${rowHtml(row, i)}</div>`).join('')
        + `<p class="ao-foot">${esc(NOTE)}</p>`;
      if (keep && keep.field) root.querySelector(`[data-ao-row="${CSS.escape(keep.row)}"] [data-ao-field="${keep.field}"]:not(:disabled)`)?.focus({preventScroll: true});
      root.dispatchEvent(new CustomEvent('agent-options-painted', {bubbles: true}));
    }
    root.addEventListener('change', event => {
      const field = event.target.closest('[data-ao-field]'), row = event.target.closest('[data-ao-row]');
      if (field && row) void ctl.change(row.dataset.aoRow, field.dataset.aoField, field.value);
    });
    void ctl.load();
    return {root, load: ctl.load, state: ctl.state};
  }

  return {normalize, body, refusal, rowModel, controller, mount, CLIENTS, NAMES, EFFORTS, SUGGESTIONS, NOTE};
})();
if (typeof module !== 'undefined') module.exports = agentOptions;
