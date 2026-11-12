// The owner's Qorqbot sign-in, before the walkthrough: email, then the emailed code and how they use Qorqbot.
// The local server talks to the account service and keeps the token (qorqut/account.py); this page never sees it.
// When the service cannot be reached the owner may continue for now; the screen returns on the next start.
// Also paints the one-line "new version" notice from the same /root/account read, checked again every six hours.
// `accountGate.updateMessage` is pure (tested with node --test).
const accountGate = (() => {
  const LICENSE_URL = 'https://qorqbot.com/license/';
  const PRIVACY_URL = 'https://qorqbot.com/privacy/';
  const UPDATE_DISMISSED = 'qorqbot:update-notice-dismissed';
  const RECHECK = 6 * 3600 * 1000, FIRST_RECHECK = 15000;
  let root = null, email = '', marketing = false, resolveGate = null;

  function updateMessage(view) {
    const update = view && view.update;
    if (!update || !update.version) return null;
    return {text: `Qorqbot ${update.version} is available.`, action: 'Download', url: update.url || 'https://qorqbot.com/downloads/',
      version: update.version};
  }
  function paintUpdate(view) {
    document.querySelector('.update-notice')?.remove();
    const notice = updateMessage(view);
    let dismissed = null;
    try { dismissed = sessionStorage.getItem(UPDATE_DISMISSED); } catch (_) { /* Shown again next time. */ }
    if (!notice || dismissed === notice.version) return;
    const bar = document.createElement('div');
    bar.className = 'setup-notice update-notice'; bar.setAttribute('role', 'status');
    bar.innerHTML = `<span>${esc(notice.text)}</span>
      <a href="${esc(notice.url)}" target="_blank" rel="noopener noreferrer">${esc(notice.action)}</a>
      <button type="button" class="setup-notice-close" aria-label="Dismiss">×</button>`;
    bar.querySelector('.setup-notice-close').onclick = () => {
      try { sessionStorage.setItem(UPDATE_DISMISSED, notice.version); } catch (_) { /* Still closes for this page. */ }
      bar.remove();
    };
    document.body.prepend(bar);
  }

  async function read() { return coreData(await api('/root/account')); }
  async function send(action, body) { return coreData(await post('/root/account/' + action, body)); }
  const unreachable = error => error && (error.code === 'provider_unavailable' || error.code === 'network_unavailable');

  function card(html) {
    if (!root) {
      root = document.createElement('div');
      root.className = 'account-gate';
      root.innerHTML = '<section class="onboarding-card account-gate-card" role="dialog" aria-modal="true" aria-labelledby="account-gate-title"></section>';
      document.body.append(root);
    }
    const section = root.querySelector('section');
    section.innerHTML = html;
    // A message about the last attempt goes away once the owner types again.
    section.oninput = () => { const line = section.querySelector('[data-account-error]'); if (line) line.textContent = ''; };
    return section;
  }
  const privacyNote = `<p class="onboarding-private"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.8 12.2l2.2 2.2 4.3-4.6"/></svg><span><b>Your work stays here.</b> Qorqbot only learns your email and that Qorqbot is in use. Never your conversations, files or what your agents do. <a href="${PRIVACY_URL}" target="_blank" rel="noopener noreferrer">Privacy</a></span></p>`;
  function showError(section, error) {
    const line = section.querySelector('[data-account-error]');
    if (line) line.textContent = error?.message || 'That did not work. Try again.';
  }
  function busy(button, on, label) { button.disabled = on; if (label) button.textContent = on ? '…' : label; }

  function emailStep() {
    const section = card(`<h2 id="account-gate-title">sign in to qorqbot</h2>
      <p>Enter your email and we’ll send you a sign-in code. You do this once on this computer.</p>
      <form class="account-gate-form" data-account-email novalidate>
        <input type="email" name="email" autocomplete="email" placeholder="you@example.com" required value="${esc(email)}" aria-label="email">
        <label class="account-gate-check"><input type="checkbox" name="marketing" ${marketing ? 'checked' : ''}> Send me occasional product news</label>
        <p class="account-gate-error" data-account-error role="alert"></p>
        <div class="onboarding-welcome-actions"><button type="submit">send code</button></div>
      </form>${privacyNote}`);
    const form = section.querySelector('form'), input = form.email, button = form.querySelector('button');
    input.focus();
    form.onsubmit = async event => {
      event.preventDefault();
      email = input.value.trim(); marketing = form.marketing.checked;
      busy(button, true, 'send code');
      try { await send('start', {email}); codeStep(); }
      catch (error) { unreachable(error) ? offlineStep() : showError(section, error); }
      finally { busy(button, false, 'send code'); }
    };
  }

  function codeStep() {
    const section = card(`<h2 id="account-gate-title">check your email</h2>
      <p>We sent a code to <b>${esc(email)}</b>. It expires in 10 minutes.</p>
      <form class="account-gate-form" data-account-code novalidate>
        <input name="code" autocomplete="one-time-code" inputmode="text" maxlength="9" placeholder="XXXX-XXXX" required aria-label="sign-in code" class="account-gate-code">
        <fieldset class="account-gate-use"><legend>How will you use Qorqbot?</legend>
          <label><input type="radio" name="use" value="personal" checked> Personal</label>
          <label><input type="radio" name="use" value="work"> For work</label>
        </fieldset>
        <input name="company" autocomplete="organization" placeholder="Company name" aria-label="company name" hidden>
        <p class="onboarding-hint" data-account-work hidden>Work use needs a commercial license, free until January 1, 2027. <a href="${LICENSE_URL}" target="_blank" rel="noopener noreferrer">License</a></p>
        <p class="account-gate-error" data-account-error role="alert"></p>
        <div class="onboarding-welcome-actions"><button type="button" class="ghost" data-account-back>back</button><button type="button" class="ghost" data-account-resend>send again</button><button type="submit">sign in</button></div>
      </form>`);
    const form = section.querySelector('form'), button = form.querySelector('button[type=submit]');
    const work = () => form.querySelector('input[name=use]:checked').value === 'work';
    form.querySelectorAll('input[name=use]').forEach(radio => radio.onchange = () => {
      form.company.hidden = !work(); section.querySelector('[data-account-work]').hidden = !work();
      if (work()) form.company.focus();
    });
    form.code.focus();
    section.querySelector('[data-account-back]').onclick = emailStep;
    section.querySelector('[data-account-resend]').onclick = async () => {
      try { await send('start', {email}); showError(section, {message: 'A new code is on its way.'}); }
      catch (error) { unreachable(error) ? offlineStep() : showError(section, error); }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      busy(button, true, 'sign in');
      try {
        const view = await send('verify', {email, code: form.code.value.trim(), use: work() ? 'work' : 'personal',
          company: work() ? form.company.value.trim() : null, marketing_ok: marketing});
        finish(view);
      } catch (error) { unreachable(error) ? offlineStep() : showError(section, error); }
      finally { busy(button, false, 'sign in'); }
    };
  }

  function offlineStep() {
    const section = card(`<h2 id="account-gate-title">can’t reach the sign-in service</h2>
      <p>Check your internet connection and try again. You can also continue now and sign in the next time you start Qorqbot.</p>
      <p class="account-gate-error" data-account-error role="alert"></p>
      <div class="onboarding-welcome-actions"><button type="button" class="ghost" data-account-later>continue for now</button><button type="button" data-account-retry>try again</button></div>`);
    section.querySelector('[data-account-retry]').onclick = emailStep;
    section.querySelector('[data-account-later]').onclick = async () => {
      try { finish(await send('defer', {})); } catch (error) { showError(section, error); }
    };
  }

  function finish(view) {
    root?.remove(); root = null;
    if (typeof settingsAccountRefresh === 'function') settingsAccountRefresh(view);
    const done = resolveGate; resolveGate = null;
    if (done) done(view);
  }

  // Resolves once the owner may use the dashboard: already signed in, signed in now, continuing for now, sign-in not
  // required, or not the owner (agents' embedded views never reach here).
  async function boot() {
    let view;
    try { view = await read(); } catch (_) { return null; }
    paintUpdate(view);
    // The first read after a start only begins the release check; its answer is ready moments later.
    const recheck = async () => { try { paintUpdate(await read()); } catch (_) { /* Checked again later. */ } };
    setTimeout(recheck, FIRST_RECHECK);
    setInterval(recheck, RECHECK);
    if (!view.required) return view;
    return new Promise(resolve => { resolveGate = resolve; emailStep(); });
  }
  // Settings' "Sign in" after the owner continued without signing in.
  function signIn() { resolveGate = null; emailStep(); }
  return {boot, read, send, signIn, updateMessage, paintUpdate, LICENSE_URL, PRIVACY_URL};
})();
if (typeof module !== 'undefined') module.exports = accountGate;
