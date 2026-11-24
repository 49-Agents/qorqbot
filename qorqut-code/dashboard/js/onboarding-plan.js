// Pure walkthrough navigation: chapters, resume position, progress labels and key handling. No DOM access, so it is unit tested
// with `node --test dashboard/tests/*.test.js`. Each step has an `id`, a `chapter` and optionally a `progressStep`, the id
// saved for it when it shares progress with an earlier step.
const onboardingPlan = (() => {
  const saveId = step => step.progressStep || step.id;
  function chapters(steps) {
    const list = [];
    steps.forEach((step, index) => {
      if (!list.length || list[list.length - 1].id !== step.chapter) list.push({id: step.chapter, start: index, end: index});
      else list[list.length - 1].end = index;
    });
    return list;
  }
  function chapterAt(steps, index) {
    const list = chapters(steps);
    const position = list.findIndex(c => index >= c.start && index <= c.end);
    return {...list[position], number: position + 1, total: list.length, step: index - list[position].start + 1,
      count: list[position].end - list[position].start + 1};
  }
  // First step of the next chapter, or -1 when this is the last chapter.
  function nextChapter(steps, index) {
    const list = chapters(steps), current = list.findIndex(c => index >= c.start && index <= c.end);
    return current + 1 < list.length ? list[current + 1].start : -1;
  }
  const RETIRED_START = new Set(['start', 'terminal', 'managed-start']);
  // Where an interrupted walkthrough continues: the first step saved under that id, else the beginning.
  function resumeIndex(steps, saved) {
    if (!saved || saved.status !== 'in_progress') return 0;
    const find = id => steps.findIndex(step => saveId(step) === id);
    let index = find(saved.step);
    // The retired Todo board and Activity/Inbox steps resume at Calendar, the step that now follows Focus.
    if (index < 0 && ['attention', 'activity'].includes(saved.step)) index = find('calendar');
    // The Learning Applier steps are retired while it is unavailable; their chapter continues at Settings.
    if (index < 0 && ['learning', 'context'].includes(saved.step)) index = find('settings');
    // The retired start steps (the folder consent, the terminal launch and the managed start) continue at the step that
    // follows Ordubashy's introduction.
    if (index < 0 && RETIRED_START.has(saved.step)) index = find('focus');
    // The retired connector steps resume at the browser step, now the end of the tour.
    if (index < 0 && ['connectors', 'connect-more'].includes(saved.step)) index = find('browser-setup');
    return index < 0 ? 0 : index;
  }
  // Whether the hello on the last step can be answered. `clients` is GET /root/agent-clients ({claude, codex}, each with `installed` and
  // `signed_in`; null when it could not be read) and `options` is GET /root/agents/<id>/chat/options (null when it is not available).
  // 'ready': an answer can come, and the message may be typed and sent (it starts Ordubashy whether he is asleep or off).
  // 'none': neither Claude Code nor Codex is installed and signed in. 'chosen': one is, but the client this agent is set to use is not.
  // An unreadable clients status never blocks the hello: the owner can always write to him.
  const clientReady = item => !!(item && item.installed && item.signed_in);
  function helloReadiness(clients, options = null) {
    if (!clients || typeof clients !== 'object') return {state: 'ready', client: ''};
    const claude = clientReady(clients.claude), codex = clientReady(clients.codex);
    if (!claude && !codex) return {state: 'none', client: ''};
    const chosen = options && ['claude', 'codex'].includes(options.effective_client) ? options.effective_client : '';
    if (chosen && !clientReady(clients[chosen])) return {state: 'chosen', client: chosen};
    return {state: 'ready', client: chosen};
  }
  // What the last step says when nothing can answer yet: which client is missing or signed out, and where to fix it. `readiness` is
  // `helloReadiness(...)`; the empty text means the hello can be answered.
  const CLIENT_NAMES = {claude: 'Claude Code', codex: 'Codex'};
  const clientProblem = item => !item || !item.installed ? 'isn’t installed' : !item.signed_in ? 'isn’t signed in' : 'isn’t ready';
  function helloMissing(clients, readiness, agent = 'Ordubashy') {
    if (!readiness || !clients || typeof clients !== 'object') return '';
    if (readiness.state === 'none') {
      return `${agent} needs Claude Code or Codex to answer, and neither is ready: Claude Code ${clientProblem(clients.claude)}, and Codex ${clientProblem(clients.codex)}. Open Settings > Agents to fix one, then come back and say hello.`;
    }
    if (readiness.state === 'chosen' && CLIENT_NAMES[readiness.client]) {
      const name = CLIENT_NAMES[readiness.client], other = CLIENT_NAMES[readiness.client === 'claude' ? 'codex' : 'claude'];
      return `${agent} is set to use ${name}, and ${name} ${clientProblem(clients[readiness.client])}. Open Settings > Agents to fix it, or switch ${agent} to ${other} there, then say hello.`;
    }
    return '';
  }
  // The steps as shown: while agents run in terminals, a step's `terminals` fields replace its own. The list, its order and the saved
  // ids stay the same, and with `terminals` false the very same list comes back.
  const stepsFor = (steps, terminals) => terminals ? steps.map(step => step.terminals ? {...step, ...step.terminals} : step) : steps;
  // Whether the hello may be typed and sent when agents run in terminals: Ordubashy's session is live and can receive. `chat` is
  // GET /root/agents/<id>/chat (null when it could not be read).
  const helloLive = chat => !!chat && !!chat.session && chat.session.state === 'live' && chat.session.can_send === true;
  // Applies a key press to the tour: Esc closes it and Tab stays within it, wherever the focus is.
  // `tour` is what is on screen: the highlighted `target` element (or null), `active` (the focused element),
  // `focusables()` (the elements Tab cycles through, in order) and `exit()`. Returns 'pass' when the key was left alone.
  function handleKey(event, tour) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); tour.exit(); return 'exit'; }
    if (event.key !== 'Tab') return 'pass';
    const list = tour.focusables(); if (!list.length) return 'pass';
    const at = list.indexOf(tour.active), next = (at + (event.shiftKey ? -1 : 1) + list.length) % list.length;
    event.preventDefault(); list[next].focus();
    return 'tab';
  }
  return {chapters, chapterAt, nextChapter, resumeIndex, saveId, helloReadiness, helloMissing, clientReady, stepsFor, helloLive, handleKey};
})();
if (typeof module !== 'undefined') module.exports = onboardingPlan;
