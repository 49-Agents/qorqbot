// Desktop notifications for agent replies, Signoffs waiting on the owner, due reminders and failed routines, while the dashboard is in the background.
// The choice is per browser (localStorage); the operating system's permission is asked for only when the owner turns
// a kind on. In the dashboard itself, the existing in-page toasts are unchanged.
const desktopNotify = (() => {
  const KEY = 'qorqbot:notifications';
  const KINDS = ['replies', 'signoffs', 'reminders', 'routines'];
  const supported = () => typeof Notification === 'function';
  function prefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
      return Object.fromEntries(KINDS.map(kind => [kind, saved[kind] === true]));
    } catch (_) { return Object.fromEntries(KINDS.map(kind => [kind, false])); }
  }
  const permission = () => supported() ? Notification.permission : 'unsupported';
  async function enable(kind, on) {
    if (on && supported() && Notification.permission === 'default') await Notification.requestPermission();
    const next = {...prefs(), [kind]: !!on && permission() === 'granted'};
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch (_) { /* Not remembered on this browser. */ }
    return next;
  }
  // A choice made outside this browser (Ordubashy's settings_set, js/display-sync.js). The operating system's permission is
  // only asked for by the owner's own click, so a kind turned on here stays off until this browser is allowed to notify.
  function set(kind, on) {
    if (!KINDS.includes(kind)) return prefs();
    const next = {...prefs(), [kind]: !!on && permission() === 'granted'};
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch (_) { /* Not remembered on this browser. */ }
    return next;
  }
  // `force` (the Settings test button) shows it even while the dashboard is in front.
  function send({kind, title, body = '', tag, onClick, force = false}) {
    if (!supported() || Notification.permission !== 'granted') return null;
    if (!force && (!prefs()[kind] || (document.visibilityState === 'visible' && document.hasFocus()))) return null;
    try {
      const note = new Notification(title, {body: String(body).slice(0, 240), tag, silent: false});
      note.onclick = () => { try { window.focus(); } catch (_) { /* Focus is best effort. */ } note.close(); onClick?.(); };
      // Closed after the same 10 seconds as the in-page notes, also where the system would keep it on screen.
      setTimeout(() => { try { note.close(); } catch (_) { /* Already gone. */ } }, 10000);
      return note;
    } catch (_) { return null; }
  }
  return {prefs, permission, enable, set, send, supported, KINDS};
})();
if (typeof module !== 'undefined') module.exports = desktopNotify;
