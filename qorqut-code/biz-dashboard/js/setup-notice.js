// A notice at the top of the dashboard when something Qorqbot cannot install itself is missing: Google Chrome, which the
// agents' own browsers run on. Everything else Qorqbot installs by itself. Facts come from /root/about (owner only).
// `setupNotice.message` is pure (tested with node --test); `setupNotice.show` paints it.
const setupNotice = (() => {
  const CHROME_URL = 'https://www.google.com/chrome/';
  const DISMISSED = 'qorqbot:setup-notice-dismissed';
  // What to tell the owner, or null when nothing is missing.
  function message(about) {
    if (!about || about.chrome_installed !== false) return null;
    return {text: 'Qorqbot needs Google Chrome so your agents can work on the web.', action: 'Get Chrome', url: CHROME_URL};
  }
  function dismissed() { try { return sessionStorage.getItem(DISMISSED) === '1'; } catch (_) { return false; } }
  function show(about) {
    document.querySelector('.setup-notice')?.remove();
    const notice = message(about);
    if (!notice || dismissed()) return;
    const bar = document.createElement('div');
    bar.className = 'setup-notice'; bar.setAttribute('role', 'alert');
    bar.innerHTML = `<span>${esc(notice.text)}</span>
      <a href="${esc(notice.url)}" target="_blank" rel="noopener noreferrer">${esc(notice.action)}</a>
      <button type="button" class="setup-notice-close" aria-label="Dismiss">×</button>`;
    bar.querySelector('.setup-notice-close').onclick = () => {
      try { sessionStorage.setItem(DISMISSED, '1'); } catch (_) { /* Still closes for this page. */ }
      bar.remove();
    };
    document.body.prepend(bar);
  }
  // Checked once per page load; a non-owner simply sees nothing.
  async function check() {
    if (typeof api !== 'function') return;
    try { show(coreData(await api('/root/about'))); } catch (_) { /* Not the owner, or the server is starting. */ }
  }
  return {message, show, check, CHROME_URL};
})();
if (typeof module !== 'undefined') module.exports = setupNotice;
else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setupNotice.check());
else setupNotice.check();
