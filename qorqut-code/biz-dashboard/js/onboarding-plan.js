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
  return {chapters, chapterAt, nextChapter, resumeIndex, saveId, launchPhase, handleKey};
})();
if (typeof module !== 'undefined') module.exports = onboardingPlan;
