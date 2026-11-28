// A notice on the dashboard when the owner has to install Google Chrome: the agents' browser has no Chrome it may run on
// (/root/about `chrome_installed`, owner only). While it shows, /root/about is read again every few seconds and the notice
// goes by itself once Chrome is there. It stands above the welcome tour and the first-run screens, which leave it usable.
// `setupNotice.message` is pure (tested with node --test); `setupNotice.show` paints it.
const setupNotice = (() => {
  const CHROME_URL = 'https://www.google.com/chrome/';
  const DISMISSED = 'qorqbot:setup-notice-dismissed';
  const RECHECK_MS = 8000;
  let timer = null;
  // What to tell the owner, or null when nothing is missing.
  function message(about) {
    if (!about || about.chrome_installed !== false) return null;
    return {text: 'Qorqbot needs Google Chrome so your agents can work on the web.', action: 'Get Chrome', url: CHROME_URL};
  }
  // Top notices (this one and the update notice, js/account-gate.js) share one corner stack so they never cover each other or the
  // page's controls; any `.setup-notice` added straight to the body is moved into it.
  function stack() {
    let host = document.querySelector('.setup-notices');
    if (!host) { host = document.createElement('div'); host.className = 'setup-notices'; document.body.append(host); watchHeight(host); }
    return host;
  }
  // The app's toast messages sit in the same corner (css/app.css `.toasts`): they stand above the notices by this much.
  function watchHeight(host) {
    const root = document.documentElement;
    const paint = () => root.style.setProperty('--notices-h', host.offsetHeight ? `${host.offsetHeight + 8}px` : '0px');
    if (typeof ResizeObserver === 'function') new ResizeObserver(paint).observe(host);
    paint();
  }
  function gather() {
    if (typeof MutationObserver !== 'function' || !document.body) return;
    const move = () => document.querySelectorAll('body > .setup-notice').forEach(bar => stack().append(bar));
    new MutationObserver(move).observe(document.body, {childList: true});
    move();
  }
  function dismissed() { try { return sessionStorage.getItem(DISMISSED) === '1'; } catch (_) { return false; } }
  const current = () => document.querySelector('.chrome-notice');
  function show(about) {
    const notice = message(about);
    if (!notice || dismissed()) { current()?.remove(); return; }
    if (current()) return;                     // already up: not painted (and announced) again on every check
    const bar = document.createElement('div');
    bar.className = 'setup-notice chrome-notice'; bar.setAttribute('role', 'alert');
    bar.innerHTML = `<span>${esc(notice.text)}</span>
      <a href="${esc(notice.url)}" target="_blank" rel="noopener noreferrer">${esc(notice.action)}</a>
      <button type="button" class="setup-notice-close" aria-label="Dismiss">×</button>`;
    bar.querySelector('.setup-notice-close').onclick = () => {
      try { sessionStorage.setItem(DISMISSED, '1'); } catch (_) { /* Still closes for this page. */ }
      bar.remove();
    };
    stack().append(bar);
  }
  // Checked on page load, and again while the notice is up; a non-owner simply sees nothing.
  async function check() {
    if (typeof api !== 'function') return;
    clearTimeout(timer);
    try { show(coreData(await api('/root/about'))); } catch (_) { /* Not the owner, or the server is restarting. */ }
    if (current()) timer = setTimeout(check, RECHECK_MS);
  }
  return {message, show, check, gather, CHROME_URL, RECHECK_MS};
})();
if (typeof module !== 'undefined') module.exports = setupNotice;
else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => { setupNotice.gather(); setupNotice.check(); });
else { setupNotice.gather(); setupNotice.check(); }
