// Slash commands in Chat: typing "/" at the start of the composer lists the running CLI's commands (GET …/chat/commands).
// `slashCommands.match` is pure (tested with node --test); `slashCommands.attach` adds the menu to a composer.
const slashCommands = (() => {
  const MAX_SHOWN = 8;
  // The command being typed: the composer starts with "/" and no space or line break yet.
  const typing = value => /^\/[^\s]*$/.test(value) ? value.toLowerCase() : null;
  // Commands for what is typed: names that start with it first, then names that contain it, each in catalog order.
  function match(commands, value) {
    const query = typing(value);
    if (query === null) return [];
    const starts = [], contains = [];
    for (const item of commands) {
      const name = item.name.toLowerCase();
      if (name.startsWith(query)) starts.push(item);
      else if (query.length > 2 && name.includes(query.slice(1))) contains.push(item);
    }
    return [...starts, ...contains].slice(0, MAX_SHOWN);
  }
  // The catalog entry a sent text runs, or null for an ordinary message.
  function command(commands, text) {
    const name = /^(\/[^\s]+)/.exec(String(text).trim())?.[1];
    return name ? commands.find(item => item.name.toLowerCase() === name.toLowerCase()) || null : null;
  }

  // Whether a sent command leaves a menu or panel to finish in the agent's Terminal. `/model <name>` on Claude Code switches the
  // model at once; only a bare `/model` opens its menu. Every other "terminal" command opens one.
  function opensTerminal(item, text, provider) {
    if (!item || item.where !== 'terminal') return false;
    if (provider === 'claude' && item.name.toLowerCase() === '/model') return !/^\/\S+\s+\S/.test(String(text).trim());
    return true;
  }
  // The shared Codex server takes plain text, so it cannot run a Codex command: the message to show instead of sending one
  // (null when the session is a Terminal or the text is not a known command). The backend refuses it too.
  function sharedServerRefusal(session, item, label) {
    if (!item || session?.provider !== 'codex' || session?.transport !== 'codex_queue') return null;
    return `${item.name} runs only in ${label}’s Terminal. Open Terminal and run it there.`;
  }

  // `options.commands()` returns the current catalog (or null while unknown).
  function attach({input, form, commands}) {
    const menu = document.createElement('div');
    menu.className = 'slash-menu'; menu.hidden = true; menu.setAttribute('role', 'listbox'); menu.id = 'slash-' + Math.random().toString(36).slice(2);
    menu.setAttribute('aria-label', 'Commands');
    form.prepend(menu);
    input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-controls', menu.id);
    let shown = [], active = 0, dismissed = null;
    const esc = value => String(value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    function close() { menu.hidden = true; shown = []; input.removeAttribute('aria-activedescendant'); input.setAttribute('aria-expanded', 'false'); }
    function paint() {
      const list = commands() || [];
      shown = input.value === dismissed ? [] : match(list, input.value);
      if (!shown.length) { close(); return; }
      active = Math.min(active, shown.length - 1);
      menu.innerHTML = shown.map((item, i) => `<div class="slash-item" role="option" id="${menu.id}-${i}" aria-selected="${i === active}" data-index="${i}">
        <span class="slash-name">${esc(item.name)}</span><span class="slash-desc">${esc(item.description)}</span>
        ${item.where === 'terminal' ? '<span class="slash-tag" title="Opens a menu you finish in the agent’s Terminal">Terminal</span>' : item.source !== 'built-in' ? `<span class="slash-tag">${item.source === 'skill' ? 'Skill' : 'Yours'}</span>` : ''}</div>`).join('');
      menu.hidden = false;
      input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-activedescendant', `${menu.id}-${active}`);
      menu.querySelector('[aria-selected="true"]')?.scrollIntoView({block: 'nearest'});
    }
    function choose(index) {
      const item = shown[index]; if (!item) return;
      input.value = item.name + ' ';
      input.dispatchEvent(new Event('input', {bubbles: true}));
      dismissed = input.value; close(); input.focus();
    }
    const onInput = () => { if (input.value !== dismissed) dismissed = null; active = 0; paint(); };
    // Capture: runs before the composer's own Enter-to-send, so Enter picks the highlighted command.
    const onKey = event => {
      if (menu.hidden || event.isComposing) return;
      const move = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
      if (move) { event.preventDefault(); active = (active + move + shown.length) % shown.length; paint(); return; }
      const exact = shown[active] && input.value.trim().toLowerCase() === shown[active].name.toLowerCase();
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey && !exact)) {
        event.preventDefault(); event.stopImmediatePropagation(); choose(active); return;
      }
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismissed = input.value; close(); }
    };
    const onPick = event => { const row = event.target.closest('.slash-item'); if (row) { event.preventDefault(); choose(Number(row.dataset.index)); } };
    const onBlur = () => setTimeout(() => { if (document.activeElement !== input) close(); }, 120);
    input.addEventListener('input', onInput);
    input.addEventListener('keydown', onKey, true);
    input.addEventListener('blur', onBlur);
    menu.addEventListener('mousedown', onPick);
    return {
      // A catalog that arrives after typing started: show it if the owner is still typing a command.
      refresh() { if (document.activeElement === input) paint(); },
      dispose() {
        input.removeEventListener('input', onInput); input.removeEventListener('keydown', onKey, true);
        input.removeEventListener('blur', onBlur); menu.remove();
      },
    };
  }
  return {match, command, opensTerminal, sharedServerRefusal, attach, MAX_SHOWN};
})();
if (typeof module !== 'undefined') module.exports = slashCommands;
