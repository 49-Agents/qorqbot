// Settings > Model/Provider: the Claude Code folder every agent runs with (CLAUDE_CONFIG_DIR), and a button that restarts the
// open Claude sessions with it. An owner who keeps a second Claude account in a folder such as ~/.claude-personal (often through
// a shell alias, which agents never see) names that folder here.
// Server: GET /root/agents/claude-folder -> {path, effective, default}; POST {path?: text|null, restart?: bool} -> the same view,
// plus restart: {restarted, continued, failed, terminals} when asked (qorqut/claude_folder.py). A server without the route
// (404 or 405) hides the row. `rowHelp` and `restartNote` are pure (tested with node --test, which also runs `mount`).
const claudeFolder = (() => {
  const ROUTE = '/root/agents/claude-folder';
  const routeMissing = error => !!error && [404, 405].includes(error.httpStatus);
  const str = value => typeof value === 'string' ? value : '';

  function rowHelp(view) {
    if (!view) return 'Checking…';
    const using = str(view.effective);
    return view.path
      ? `Agents use the Claude Code account and settings in ${using}. Clear the field to go back to ${str(view.default)}.`
      : `Agents use ${using}. To run them as another Claude account, enter the folder that account uses, such as ~/.claude-personal.`;
  }

  function restartNote(result) {
    if (!result || typeof result !== 'object') return '';
    if (result.terminals) return 'Agents run in terminals: each one uses this folder when its terminal is next started.';
    const count = key => Math.max(0, Math.floor(Number(result[key]) || 0));
    const restarted = count('restarted'), failed = count('failed');
    if (!restarted && !failed) return 'No Claude agents were running. Each one uses this folder when it next starts.';
    const lead = restarted ? `${restarted} agent${restarted === 1 ? '' : 's'} restarted with this folder.` : '';
    return `${lead}${failed ? `${lead ? ' ' : ''}${failed} did not restart; ${failed === 1 ? 'it uses' : 'they use'} the folder on the next message.` : ''}`;
  }

  // Options (all optional): read, save (body -> view), toast.
  function mount(container, options = {}) {
    const read = options.read || (async () => coreData(await api(ROUTE)));
    const save = options.save || (async body => coreData(await post(ROUTE, body)));
    let view = null, failed = false, missing = false, busy = false, note = '';
    const root = document.createElement('div');
    container.replaceChildren(root);
    function paint() {
      container.style.display = missing ? 'none' : '';
      if (missing) { root.innerHTML = ''; root.dispatchEvent(new Event('claude-folder-painted')); return; }
      const help = failed ? 'Could not read this setting.' : rowHelp(view);
      const value = view && view.path ? view.path : '';
      root.innerHTML = `<div class="st-row" data-cf-row data-search="${esc(('claude code folder account config claude_config_dir personal restart ' + help).toLowerCase())}">
        <div class="st-row-text"><div class="st-label">Claude Code folder</div><div class="st-help" data-cf-help>${esc(help)}</div>
          ${note ? `<div class="st-help" data-cf-note>${esc(note)}</div>` : ''}</div>
        <div class="st-control">
          <input type="text" data-cf-input spellcheck="false" autocomplete="off" placeholder="${esc(view ? str(view.default) : '~/.claude')}" value="${esc(value)}" ${view && !busy ? '' : 'disabled'} aria-label="Claude Code folder">
          <button type="button" class="quiet" data-cf-action="save" ${view && !busy ? '' : 'disabled'}>Save</button>
          <button type="button" class="quiet" data-cf-action="restart" ${view && !busy ? '' : 'disabled'}>Restart Claude agents</button>
          ${failed ? '<button type="button" class="quiet" data-cf-action="retry">Try again</button>' : ''}
        </div></div>`;
      root.dispatchEvent(new Event('claude-folder-painted'));
    }
    async function refresh() {
      try { view = await read(); failed = false; }
      catch (error) { if (routeMissing(error)) missing = true; else failed = true; }
      if (container.isConnected) paint();
    }
    async function act(body, busyNote) {
      busy = true; note = busyNote; paint();
      try {
        const next = await save(body);
        view = {path: next.path, effective: next.effective, default: next.default};
        note = 'restart' in next ? restartNote(next.restart) : 'Saved. Restart Claude agents to use it now; others use it when they next start.';
      } catch (error) {
        // The server says why (a folder that does not exist names the command that creates it); shown on the row, not in a toast.
        note = (error && (error.serverMessage || error.message)) || 'The Claude Code folder could not be saved.';
      }
      busy = false;
      if (container.isConnected) paint();
    }
    root.addEventListener('click', event => {
      const button = event.target.closest('[data-cf-action]');
      if (!button || busy) return;
      const id = button.dataset.cfAction;
      if (id === 'retry') { failed = false; view = null; paint(); void refresh(); return; }
      if (id === 'save') {
        const text = root.querySelector('[data-cf-input]').value.trim();
        void act({path: text || null}, 'Saving…');
      }
      if (id === 'restart') {
        // A folder typed but not saved yet is saved first, in the same request.
        const text = root.querySelector('[data-cf-input]').value.trim();
        const body = text !== str(view && view.path) ? {path: text || null, restart: true} : {restart: true};
        void act(body, 'Restarting Claude agents…');
      }
    });
    root.addEventListener('keydown', event => {
      if (event.key === 'Enter' && event.target.matches('[data-cf-input]')) root.querySelector('[data-cf-action="save"]').click();
    });
    paint();
    void refresh();
    return {root, refresh};
  }

  return {rowHelp, restartNote, mount, ROUTE};
})();
if (typeof module !== 'undefined') module.exports = claudeFolder;
