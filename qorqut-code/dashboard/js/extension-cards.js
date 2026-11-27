// The Connections card of an extension that follows the contract: drawn from its entry in /root/connectors (`kind`
// `contract`, shaped by qorqut.extension_connections), with no copy written for it here. It shows the name, summary,
// status with its note, the set-up step and an on/off switch per agent. Set-up kinds `none` and `key` are set up here;
// `sign-in` and `pairing` show their status and say that the step is not available yet. Every action is one POST to
// /root/connections/<id>, answered with the entry as it is then. A typed value is posted once and never shown again.
// A downloadable extension also has the owner's own Install, Update and Remove from its `offers`; his click is the
// approval. Remove opens the same two choices as the card for Ordubashy's ask. While its change waits for the restart
// the card says so (`result`), and after the restart it shows what happened.
const extensionCards = (() => {
  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3v4M15 3v4M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v5"/></svg>';
  const STATUS = {
    not_set_up: {label: 'Not set up', tone: 'idle'}, setting_up: {label: 'Setting up', tone: 'warn'},
    ready: {label: 'Ready', tone: 'ok'}, needs_attention: {label: 'Needs attention', tone: 'warn'},
    unsupported: {label: 'Not available', tone: 'idle'}, not_installed: {label: 'Not installed', tone: 'idle'},
  };
  const UNKNOWN = {label: 'Unknown', tone: 'idle'};
  const NOT_YET = 'Setting this up from Qorqbot is not available yet.';
  const REMOVE_AFTER = 'Qorqbot restarts once. Projects, tables and records it created stay.';
  const status = item => STATUS[item.status] || UNKNOWN;
  const kind = item => item.setup?.kind || null;
  // Whether set-up can work, decided by the server (`can_set_up`): a refused folder or code part only shows its note.
  const usable = item => !!item.setup && item.can_set_up === true;
  // The requests the card sends (the shapes of qorqut.extension_connections.parse_action).
  const path = id => `/root/connections/${encodeURIComponent(id)}`;
  const request = {
    start: () => ({action: 'start'}), cancel: () => ({action: 'cancel'}), disconnect: () => ({action: 'disconnect'}),
    accept: values => ({action: 'accept', values}),
    switch: (agentId, on) => ({action: 'switch', agent_id: agentId, on}),
    install: version => ({action: 'install', version}), update: version => ({action: 'update', version}),
    remove: () => ({action: 'remove', delete_data: false}), 'remove-delete': () => ({action: 'remove', delete_data: true}),
  };
  // The owner's own downloads, from the entry's offers. 'remove-choose' opens the two removal choices.
  function downloads(item) {
    return (item.offers || []).map(offer => offer.action === 'remove'
      ? {kind: 'remove-choose', label: 'Remove', quiet: true}
      : {kind: offer.action, label: offer.action === 'update' ? `Update to ${offer.version}` : 'Install', version: offer.version});
  }
  function removal(item, esc) {
    if (!(item.offers || []).some(offer => offer.action === 'remove')) return '';
    return `<div class="cx-key" data-ext-remove="${esc(item.id)}" hidden><p class="cx-note" data-tone="info">${esc(REMOVE_AFTER)}</p>
      <div class="cx-key-actions"><button type="button" data-ext-action="remove" data-cx-id="${esc(item.id)}">Remove</button>
      <button type="button" data-ext-action="remove-delete" data-cx-id="${esc(item.id)}">Remove and delete its data</button>
      <button type="button" class="quiet" data-ext-remove-cancel>Close</button></div></div>`;
  }
  // The set-up step: 'key' opens the fields form, 'cancel' ends a set-up Ordubashy started, 'disconnect' forgets the key.
  function steps(item) {
    if (!usable(item) || kind(item) !== 'key') return [];
    if (item.status === 'ready') return [{kind: 'disconnect', label: 'Disconnect', quiet: true}];
    const open = {kind: 'key', label: item.status === 'needs_attention' ? 'Try again' : 'Set up'};
    return item.status === 'setting_up' ? [open, {kind: 'cancel', label: 'Cancel', quiet: true}] : [open];
  }
  // {text, tone}: the status note, or for sign-in and pairing the sentence that the step is not here yet.
  function note(item) {
    if (item.note) return {text: item.note, tone: ['needs_attention', 'setting_up'].includes(item.status) ? 'warn' : 'info'};
    if (usable(item) && ['sign-in', 'pairing'].includes(kind(item)) && item.status !== 'ready') return {text: NOT_YET, tone: 'info'};
    return null;
  }
  // One switch per agent the extension may be on for, none while set-up cannot work (they would do nothing). `waiting`
  // says it takes effect once the extension is ready.
  function switches(item) {
    if (!usable(item)) return [];
    return (item.agents || []).map(agent => ({agentId: agent.agent_id, name: agent.name, on: !!agent.on,
      waiting: !!agent.on && !agent.in_effect}));
  }
  // The note is text: escaped first, then a word in backticks is shown as inline code, as elsewhere in the dashboard.
  const noteHtml = (text, esc) => esc(text).replace(/`([^`]+)`/g, '<code>$1</code>');

  function form(item, esc) {
    if (kind(item) !== 'key' || !usable(item) || item.status === 'ready') return '';
    const fields = (item.setup.fields || []).map(field => `<label><span>${esc(field.label)}</span>
        <input type="${field.secret ? 'password' : 'text'}" name="${esc(field.id)}" autocomplete="off" spellcheck="false" required></label>`).join('');
    return `<form class="cx-key" data-ext-form="${esc(item.id)}" hidden>${fields}
      <div class="cx-key-actions"><button type="submit">Save</button><button type="button" class="quiet" data-ext-form-cancel>Close</button></div>
      <p class="cx-note" data-ext-error hidden></p></form>`;
  }
  function agentRows(item, esc) {
    const rows = switches(item);
    if (!rows.length) return '';
    return `<div class="cx-ext-agents" data-ext-agents>${rows.map(row => `<label class="cx-ext-agent">
        <input type="checkbox" data-ext-switch data-cx-id="${esc(item.id)}" data-agent-id="${esc(row.agentId)}"${row.on ? ' checked' : ''}>
        <span>${esc(row.name)}</span>${row.waiting ? '<span class="cx-ext-wait">On once it is ready</span>' : ''}</label>`).join('')}</div>`;
  }
  // The card; `rows` draws it as a Settings row, otherwise as a grid card. `esc` is the dashboard's escaping function.
  function render(item, {rows = false, esc}) {
    const shown = status(item), told = note(item);
    const buttons = [...steps(item), ...downloads(item)].map(step => `<button type="button" class="${step.quiet ? 'quiet' : ''}" data-ext-action="${esc(step.kind)}" data-cx-id="${esc(item.id)}"${step.version ? ` data-ext-version="${esc(step.version)}"` : ''}>${esc(step.label)}</button>`).join('');
    const title = `<h3>${esc(item.name)}</h3><span class="cx-status" data-tone="${shown.tone}">${esc(shown.label)}</span>`;
    const result = item.result ? `<p class="cx-note" data-tone="info" data-ext-result>${noteHtml(item.result, esc)}</p>` : '';
    const body = `${told ? `<p class="cx-note" data-tone="${told.tone}">${noteHtml(told.text, esc)}</p>` : ''}${result}${form(item, esc)}${removal(item, esc)}${agentRows(item, esc)}`;
    const search = [item.name, item.summary, shown.label].join(' ').toLowerCase();
    if (rows) return `<div class="cx-row" data-cx-card="${esc(item.id)}" data-cx-contract data-tone="${shown.tone}" data-search="${esc(search)}">
      <span class="cx-icon">${icon}</span>
      <div class="cx-row-main"><div class="cx-row-title">${title}</div><p class="cx-row-does">${esc(item.summary)}</p>${body}</div>
      <div class="cx-row-action">${buttons}</div></div>`;
    return `<article class="cx-card" data-cx-card="${esc(item.id)}" data-cx-contract data-tone="${shown.tone}">
      <div class="cx-card-head"><span class="cx-icon">${icon}</span><div class="cx-card-title">${title}</div>${buttons}</div>
      <p class="cx-does">${esc(item.summary)}</p>${body}</article>`;
  }
  return {STATUS, NOT_YET, REMOVE_AFTER, status, steps, downloads, note, noteHtml, switches, path, request, render};
})();
if (typeof module !== 'undefined') module.exports = extensionCards;
