// Pure walkthrough navigation: chapters, resume position and progress labels. No DOM access, so it is unit tested
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
  return {chapters, chapterAt, nextChapter, resumeIndex, saveId, launchPhase};
})();
if (typeof module !== 'undefined') module.exports = onboardingPlan;
