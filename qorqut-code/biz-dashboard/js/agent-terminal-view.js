// An agent's Chat panel when agents run in terminals (the legacy option, one setting for the whole installation). Everything here is
// terminal-only: the Terminal view beside Chat and its Chat/Terminal toggle, the session wording ("live" and "offline", the start hint,
// "Open terminal"), the terminal refusal texts, the slash commands that finish in the Terminal. js/global-chat.js reaches it only
// behind `typeof agentTerminalView !== 'undefined'`, and every function that reads a Chat payload first asks `isTerminal(session)`:
// a payload counts as a terminal agent only when `session.execution === 'terminal'`; anything else is a managed session and keeps
// js/agent-runtime.js. The page-level mode (`agentMode.terminals()`, js/agent-mode.js) decides only whether a Terminal view is built.
// Not touched here: the managed words and widgets (asleep, off, Stop, End session, question cards); they never run for these payloads.
const agentTerminalView = (() => {
  const on = () => typeof agentMode !== 'undefined' && agentMode.terminals();
  const isTerminal = session => !!session && session.execution === 'terminal';

  // The two small buttons that switch an agent's panel between Chat and Terminal.
  function toggleHtml(terminal = false) {
    return `<div class="gchat-view-toggle" role="group" aria-label="Agent view">
    <button type="button" class="ttab" data-chat-return aria-label="Chat" aria-pressed="${!terminal}" ${terminal ? '' : 'disabled'}><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>
    <button type="button" class="ttab" data-terminal aria-label="Terminal" aria-pressed="${terminal}" ${terminal ? 'disabled' : ''}><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 17 6-6-6-6M12 19h8"/></svg></button>
  </div>`;
  }

  // Names the program that connects: the client started in this agent's own terminal (`launch_commands`, the agent's default first).
  function startHint(session, label) {
    const commands = Array.isArray(session?.launch_commands) ? session.launch_commands.filter(item => typeof item === 'string' && item) : [];
    return commands.length ? `Open the terminal (>_) and run ${commands.join(' or ')} to start ${label}` : `Start or resume ${label} in Terminal`;
  }
  // The dot and the line by the composer: the terminal product's two states, live and offline.
  function summary(session, label, viewing = true) {
    if (!session) return {key: 'loading', line: ''};
    if (session.state === 'live' && session.can_send && !viewing) return {key: 'starting', line: ''};
    if (session.state === 'live') {
      if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
      if (session.reason === 'binding_not_ready' || session.reason === 'capture_not_ready') return {key: 'starting', line: ''};
      return {key: session.working ? 'working' : 'live', line: ''};
    }
    if (session.reason === 'capture_disabled') return {key: 'disabled', line: ''};
    if (session.reason === 'unsupported_runtime') return {key: 'offline', line: ''};
    const direction = ['other_conversation', 'selection_required', 'registering'].includes(session.reason)
      ? 'Connecting to the conversation in Terminal…' : `${startHint(session, label)}.`;
    return {key: 'offline', line: direction};
  }
  // An empty conversation already shows the same direction, with its button, in the middle of the pane.
  const sessionLineText = (current, hint) => hint.text && current.key === 'offline' ? '' : current.line;
  function emptyState(session, label, key, openTerminal) {
    if (key === 'live' || key === 'working') return {text: 'Your agent conversation appears here.', action: null};
    if (key === 'starting') return {text: 'Connecting…', action: null};
    if (key === 'disabled') return {text: 'Chat unavailable.', action: null};
    return {text: `${startHint(session, label)}. Its conversation will appear here.`, action: openTerminal ? {key: 'terminal', label: 'Open terminal'} : null};
  }
  // Why the composer can't be used right now, in plain words (a message goes only to a live agent).
  function unavailableText(session, label, ctx = {}) {
    if (ctx.finished) return 'This chat is no longer available.';
    if (ctx.paired) return `Close the side conversation to message ${label}.`;
    if (!session) return 'Loading the conversation…';
    if (session.reason === 'capture_disabled') return 'Chat is unavailable.';
    if (session.state !== 'live') return `Not running. Start ${label} in its Terminal first.`;
    return `Connecting to ${label}…`;
  }

  // Refusal wording for the codes a terminal agent can give (null: the dashboard's other tables decide).
  const REFUSALS = {
    terminal_offline: 'Not running. Start or resume it in Terminal, then send again.',
    terminal_changed: 'Not delivered. Check the agent and try again.',
    terminal_required: 'This command runs only in the agent’s Terminal. Open Terminal and run it there.',
    terminal_present: 'The agent’s Terminal is still running. Close it, then send again.',
    terminal_mode_off: 'Agents run as managed sessions on this installation.',
    runtime_switching: 'Agents are being switched. Try again in a minute.',
    unsupported_runtime: 'This agent can’t take messages here.',
  };
  const refusalText = (session, code) => isTerminal(session) ? REFUSALS[code] || null : null;
  const REFUSAL_CODES = Object.keys(REFUSALS);

  // A terminal agent has the Terminal to finish a menu command in, so the catalog's "unavailable" entries (a menu only a terminal can
  // show) are offered again as `where: 'terminal'` and never refused (js/slash-commands.js refuses only "unavailable").
  const catalog = commands => commands.map(item => item.where === 'unavailable' ? {...item, where: 'terminal'} : item);
  // Whether a sent command leaves a menu or panel to finish in the Terminal. `/model <name>` on Claude Code switches the model at
  // once; only a bare `/model` opens its menu. Every other "terminal" command opens one.
  function opensTerminal(item, text, provider) {
    if (!item || item.where !== 'terminal') return false;
    if (provider === 'claude' && item.name.toLowerCase() === '/model') return !/^\/\S+\s+\S/.test(String(text).trim());
    return true;
  }
  // After such a command is sent the Terminal opens, with a short note.
  function finishInTerminal(slash, text, session, label, openTerminal, tell) {
    if (!isTerminal(session) || typeof openTerminal !== 'function' || !opensTerminal(slash, text, session.provider || 'claude')) return false;
    tell?.(`Finish ${slash.name} in ${label}’s Terminal.`);
    openTerminal();
    return true;
  }

  // The Chat/Terminal switch follows each payload: a payload that is not a terminal agent (the installation is being switched) has no
  // Terminal, so the switch goes and the host drops its Terminal view. `memo` remembers the last mode reported.
  function syncChat(root, session, options, memo) {
    if (!session) return;
    const managed = !isTerminal(session);
    const toggle = root.querySelector('.gchat-view-toggle');
    if (toggle) toggle.hidden = managed;
    if (memo.managed !== managed) { memo.managed = managed; options.onExecution?.(managed); }
  }

  // One Chat/Terminal lifecycle for an agent. Keeps the two views mounted across toggles; navigation/dispose releases both streams.
  // opts: {terminal: {key, base}, isCurrent(): bool, onTerminalStatus(status), view: 'chat'|'terminal'}; deps come from global-chat.js.
  function mountView(pane, agent, opts, deps) {
    let chat = null, closeTerminal = null, disposed = false, managed = false;
    const chatHost = document.createElement('div'), terminalHost = document.createElement('div');
    for (const host of [chatHost, terminalHost]) { host.className = 'gchat-view-host'; host.hidden = true; }
    pane.replaceChildren(chatHost, terminalHost);
    // Release a legacy viewer once on entry, not when returning to this agent's warm Chat.
    if (typeof teardownTerm === 'function') teardownTerm();
    const current = () => !disposed && (!opts.isCurrent || opts.isCurrent());
    const key = String(opts.terminal?.key ?? agent.agent_id ?? ''), base = opts.terminal?.base ?? `/root/terminal/${encodeURIComponent(key)}`;
    const showChat = () => {
      if (!current()) return;
      terminalHost.hidden = true; chatHost.hidden = false;
      if (chat) return;
      const originAgentId = String(agent.agent_id ?? '');
      chat = deps.mountChat(chatHost, agent, {openTerminal: showTerminal, onExecution: setManaged, openReference: ref => deps.openReference(ref, {
        agentId: originAgentId,
        isCurrent: () => current() && !chatHost.hidden,
      })});
    };
    // A payload that is not a terminal agent has no Terminal to show: Chat is the only view.
    const setManaged = on => {
      managed = on === true;
      if (!managed) return;
      closeTerminal?.(); closeTerminal = null; terminalHost.replaceChildren();
      if (!terminalHost.hidden) showChat();
    };
    // Refresh explicitly replaces the terminal viewer; ordinary toggles retain its renderer and stream.
    const showTerminal = (refresh = false) => {
      if (!current()) return;
      if (managed) { showChat(); return; }
      chatHost.hidden = true; terminalHost.hidden = false;
      if (closeTerminal && !refresh) return;
      closeTerminal?.();
      const label = agent.display_name || agent.name;
      terminalHost.innerHTML = `<div class="gchat-terminal"><div class="gchat-top"><span class="gchat-name">${esc(label)}</span><span class="gchat-status">Terminal</span></div><div class="gchat-session-switch">${toggleHtml(true)}</div><div class="gchat-terminal-host"></div></div>`;
      terminalHost.querySelector('[data-chat-return]').onclick = () => showChat();
      const shellStatus = terminalHost.querySelector('.gchat-status');
      closeTerminal = mountTerminal(key, terminalHost.querySelector('.gchat-terminal-host'), status => {
        shellStatus.textContent = status === 'ended' ? 'Terminal exited' : status === 'disconnected' ? 'Terminal disconnected' : 'Terminal';
        opts.onTerminalStatus?.(status);
      }, base);
    };
    // Also clean up if navigation removes/replaces the hosts without explicitly disposing the view.
    const watcher = setInterval(() => {
      if (!chatHost.isConnected || !terminalHost.isConnected || !current()) view.dispose();
    }, 2000);
    const view = {showChat, showTerminal,
      selectPairedSource(source, generation, onUnavailable, onReference, onBack) {
        if (disposed || !opts.isCurrent?.()) return false;
        if (chatHost.hidden) showChat();
        if (!chat || chatHost.hidden) return false;
        return chat.selectPairedSource?.(source, generation, onUnavailable, onReference, onBack) ?? false;
      },
      clearPairedSource(generation) {
        if (!chat) return false;
        return chat.clearPairedSource?.(generation) ?? false;
      },
      pairedCanLeave() { return chat?.pairedCanLeave?.() ?? true; },
      dispose() {
        if (disposed) return;
        disposed = true; clearInterval(watcher);
        chat?.dispose(); chat = null;
        closeTerminal?.(); closeTerminal = null;
        chatHost.remove(); terminalHost.remove();
        deps.release(view);
      }};
    deps.adopt(view);
    (opts.view === 'terminal' ? showTerminal : showChat)();
    return view;
  }

  return {on, isTerminal, toggleHtml, startHint, summary, sessionLineText, emptyState, unavailableText, refusalText, REFUSAL_CODES,
    catalog, opensTerminal, finishInTerminal, syncChat, mountView};
})();
if (typeof module !== 'undefined') module.exports = agentTerminalView;
