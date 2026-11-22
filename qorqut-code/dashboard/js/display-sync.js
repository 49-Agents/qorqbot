// Theme, text size and desktop alerts that Ordubashy set for the owner (the settings_set tool). These live in each browser, so
// the server keeps only what was asked: every field with its own revision (GET /root/display-preferences). This browser applies
// a field once, when it first sees that revision, and from then on the value is this browser's own choice like any other: a
// later change made here in Settings stays until Ordubashy sets that field again.
// `plan` is pure (unit tested with `node --test dashboard/tests/*.test.js`).
const displaySync = (() => {
  const KEY = 'qorqbot:display-applied';
  const ALERTS = {notify_replies: 'replies', notify_signoffs: 'signoffs', notify_reminders: 'reminders', notify_routines: 'routines'};
  const NAMES = {replies: 'agent replies', signoffs: 'Signoffs', reminders: 'reminders', routines: 'failed routines'};
  // What a browser that applied everything up to revision `applied` still has to apply: {appearance, alerts, revision}.
  function plan(view, applied) {
    const fields = view && typeof view.fields === 'object' && view.fields ? view.fields : {};
    const seen = Number.isInteger(applied) && applied > 0 ? applied : 0;
    const result = {appearance: {}, alerts: {}, revision: seen};
    for (const [name, item] of Object.entries(fields)) {
      if (!item || !Number.isInteger(item.revision) || item.revision <= seen) continue;
      result.revision = Math.max(result.revision, item.revision);
      if (name === 'theme' && typeof item.value === 'string') result.appearance.theme = item.value;
      else if (name === 'text_size' && typeof item.value === 'string') result.appearance.textSize = item.value;
      else if (Object.hasOwn(ALERTS, name) && typeof item.value === 'boolean') result.alerts[ALERTS[name]] = item.value;
    }
    return result;
  }
  // The sentence for alerts that were turned on but that this browser has not allowed yet, or '' when there are none.
  function blockedNote(kinds) {
    if (!kinds.length) return '';
    const named = kinds.map(kind => NAMES[kind] || kind);
    const list = named.length > 1 ? named.slice(0, -1).join(', ') + ' and ' + named.at(-1) : named[0];
    return `Ordubashy turned on desktop alerts for ${list}. This browser has not allowed notifications yet: turn them on in Settings, Notifications.`;
  }
  const applied = () => { try { return Number.parseInt(localStorage.getItem(KEY) || '0', 10) || 0; } catch (_) { return 0; } };
  let running = null, again = false;
  async function sync() {
    if (running) { again = true; return running; }
    running = (async () => {
      try {
        const next = plan(coreData(await api('/root/display-preferences', {cache: 'no-store'})), applied());
        if (Object.keys(next.appearance).length) {
          const look = appearance.set(next.appearance);
          // An open Settings page shows the new choice.
          document.querySelectorAll('[data-choice]').forEach(button =>
            button.setAttribute('aria-checked', String(look[button.dataset.choice] === button.dataset.value)));
        }
        const blocked = [];
        for (const [kind, on] of Object.entries(next.alerts)) {
          const now = desktopNotify.set(kind, on);
          if (on && !now[kind]) blocked.push(kind);
          document.querySelectorAll(`[data-switch="${kind}"]`).forEach(button => button.setAttribute('aria-checked', String(now[kind])));
        }
        if (blocked.length && typeof toast === 'function') toast(blockedNote(blocked));
        if (next.revision > applied()) { try { localStorage.setItem(KEY, String(next.revision)); } catch (_) { /* Applied again next time. */ } }
      } catch (_) { /* Tried again on the next change or page load. */ }
    })().finally(() => { running = null; if (again) { again = false; void sync(); } });
    return running;
  }
  return {plan, blockedNote, sync};
})();
if (typeof module !== 'undefined') module.exports = displaySync;

// Ordubashy changed one of the owner's settings: apply the display choices here, and reread what else the page shows of them.
if (typeof onChange === 'function') {
  onChange(['owner_settings'], null, () => {
    void displaySync.sync();
    if (typeof testingModeLoad === 'function') void testingModeLoad(true);
    if (typeof voiceInput !== 'undefined') voiceInput.refresh();
  });
  void displaySync.sync();
}
