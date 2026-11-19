// Pure walkthrough navigation: chapters, resume position, progress labels and key handling. No DOM access, so it is unit tested
// with `node --test biz-dashboard/tests/*.test.js`. Each step has an `id`, a `chapter` and optionally a `progressStep`, the id
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
  // Where an interrupted walkthrough continues: the first step saved under that id, else the beginning.
  function resumeIndex(steps, saved) {
    if (!saved || saved.status !== 'in_progress') return 0;
    const find = id => steps.findIndex(step => saveId(step) === id);
    let index = find(saved.step);
    // The retired Activity/Inbox step resumes at Calendar, the step that now follows the attention board.
    if (index < 0 && saved.step === 'activity') index = find('calendar');
    // The Learning Applier steps are retired while it is unavailable; their chapter continues at Settings.
    if (index < 0 && ['learning', 'context'].includes(saved.step)) index = find('settings');
    // The consent step exists only in the terminal variant; a walkthrough saved there continues at the launch step of this one.
    if (index < 0 && saved.step === 'start') index = find('terminal');
    // The retired connector steps resume at the browser step, now the end of the tour.
    if (index < 0 && ['connectors', 'connect-more'].includes(saved.step)) index = find('browser-setup');
    return index < 0 ? 0 : index;
  }
  // The launch step's reading of Ordubashy's Chat session (GET .../chat?limit=1): `session` is that response's session,
  // or null before the first read. Online means Chat can send to him.
  function launchPhase(session) {
    if (!session) return 'checking';
    if (session.state === 'live' && session.can_send) return 'online';
    if (session.state === 'live') return 'connecting';
    return 'waiting';
  }
  // ---- the two variants of the tour: `terminal` (the owner starts Ordubashy in his terminal) and `managed` (Qorqbot runs him
  // headless and the owner only uses Chat). One list of steps is the source; a step with `variant` belongs to that variant only,
  // and `managed: {...}` holds the fields that replace the step's own in the managed variant.
  const managedSession = session => session?.execution === 'managed';
  const variantSteps = (steps, variant) => steps.filter(step => !step.variant || step.variant === variant)
    .map(({managed, ...step}) => variant === 'managed' && managed ? {...step, ...managed} : step);
  // Which variant a tour takes, from plain inputs: `replay` (the tour taken again), `profile` (Ordubashy's agent profile),
  // `session` (his Chat session, null when it could not be read) and `clients` (GET /root/agent-clients, null when unread).
  // A replay never changes a setting, so it only follows the mode he is in. A first walkthrough switches him to managed only when
  // Claude Code is ready and he is not already starting or running in a terminal. Anything unreadable gives the terminal tour.
  function chooseVariant({replay = false, profile = null, session = null, clients = null} = {}) {
    if (!session) return 'terminal';
    if (managedSession(session)) return 'managed';
    if (replay || !clients) return 'terminal';
    const runtime = profile?.runtime ?? profile?.profile?.runtime;
    if (runtime && runtime !== 'claude-code') return 'terminal';
    if (!(clients.claude?.installed && clients.claude?.signed_in)) return 'terminal';
    return ['connecting', 'online'].includes(launchPhase(session)) ? 'terminal' : 'managed';
  }
  // The managed start step's reading: checking until his session is read, switching until it says managed, then ready.
  const managedStartPhase = session => !session ? 'checking' : managedSession(session) ? 'ready' : 'switching';
  // A refusal of the switch that says a session is already open in his terminal (codes as in agentRuntime.runtimeRefusal).
  const TERMINAL_OPEN = new Set(['session_open', 'session_live', 'live_session', 'terminal_present', 'terminal_running', 'tmux_session']);
  const terminalOpenRefusal = error => TERMINAL_OPEN.has(String(error?.code || '')) || TERMINAL_OPEN.has(String(error?.details?.reason || ''));
  // Whether the hello can be sent on the last step. `runtime` is agentRuntime. A terminal tour needs him online; a managed one
  // only needs a message to be able to open his session (or the session to be open). 'switch' is a managed tour whose switch
  // never happened (a skipped chapter); 'blocked' is a managed session that cannot start, for example Claude Code disconnected.
  function helloReadiness(session, variant, runtime) {
    if (variant !== 'managed') return launchPhase(session) === 'online' ? 'ready' : 'offline';
    if (!session || !managedSession(session)) return 'switch';
    return !runtime.closed(session) || runtime.canOpen(session) ? 'ready' : 'blocked';
  }
  // Applies a key press to the tour. Only the terminal step hands Esc and Tab to the highlighted area, because they are the
  // CLI's own keys there; on every other step Esc closes the tour and Tab stays within it, wherever the focus is.
  // `tour` is what is on screen: `step`, the highlighted `target` element (or null), `active` (the focused element),
  // `focusables()` (the elements Tab cycles through, in order) and `exit()`. Returns 'pass' when the key was left alone.
  function handleKey(event, tour) {
    if (tour.step?.terminal && tour.target?.contains(event.target)) return 'pass';
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); tour.exit(); return 'exit'; }
    if (event.key !== 'Tab') return 'pass';
    const list = tour.focusables(); if (!list.length) return 'pass';
    const at = list.indexOf(tour.active), next = (at + (event.shiftKey ? -1 : 1) + list.length) % list.length;
    event.preventDefault(); list[next].focus();
    return 'tab';
  }
  return {chapters, chapterAt, nextChapter, resumeIndex, saveId, launchPhase, variantSteps, chooseVariant, managedStartPhase, terminalOpenRefusal, helloReadiness, handleKey};
})();
if (typeof module !== 'undefined') module.exports = onboardingPlan;
