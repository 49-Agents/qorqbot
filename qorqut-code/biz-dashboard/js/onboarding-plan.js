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
    const index = steps.findIndex(step => saveId(step) === saved.step);
    return index < 0 ? 0 : index;
  }
  // The launch step's reading of Ordubashy's Chat (global-chat.js): `state` is the status line's data-state, `startLabel` the
  // visible Start control's text (none when Chat offers no start), `error` the status text shown after a failed start.
  function launchPhase({state, startLabel = '', error = ''} = {}) {
    if (state === 'live' || state === 'working') return {phase: 'online'};
    if (state === 'starting') return {phase: 'starting'};
    if (!state || state === 'loading') return {phase: 'checking'};
    if (/^try again$/i.test(startLabel.trim())) return {phase: 'failed', error};
    if (startLabel.trim()) return {phase: 'offline'};
    return {phase: 'unavailable'};
  }
  return {chapters, chapterAt, nextChapter, resumeIndex, saveId, launchPhase};
})();
if (typeof module !== 'undefined') module.exports = onboardingPlan;
