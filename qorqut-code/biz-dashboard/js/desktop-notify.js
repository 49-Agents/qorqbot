// Desktop notifications for agent replies and Signoffs waiting on the owner, while the dashboard is in the background.
// The choice is per browser (localStorage); the operating system's permission is asked for only when the owner turns
// a kind on. In the dashboard itself, the existing in-page toasts are unchanged.
const desktopNotify = (() => {
  const KEY = 'qorqbot:notifications';
  const KINDS = ['replies', 'signoffs'];
  const supported = () => typeof Notification === 'function';
  function prefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
      return Object.fromEntries(KINDS.map(kind => [kind, saved[kind] === true]));
    } catch (_) { return {replies: false, signoffs: false}; }
  }
  const permission = () => supported() ? Notification.permission : 'unsupported';
  async function enable(kind, on) {
    if (on && supported() && Notification.permission === 'default') await Notification.requestPermission();
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
      return note;
    } catch (_) { return null; }
  }
  return {prefs, permission, enable, send, supported, KINDS};
})();
if (typeof module !== 'undefined') module.exports = desktopNotify;
