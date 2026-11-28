// Claude Code and Codex, set up from the dashboard: one row per product, one button that moves Download, Connect,
// Ready. Qorqbot downloads each product and opens its sign-in page in the owner's browser.
// Shown in Settings (Model/Provider) and, when neither product is both installed and signed in, as a screen after the Qorqbot
// sign-in and before the walkthrough (`preflight`, called from main.js).
// Server: GET /root/agent-clients -> {claude, codex}; POST /root/agent-clients {provider, action} -> the same view,
// where action is download, cancel_download, connect, switch (sign in as another account), cancel_connect or submit_code
// (`code`: what the Claude sign-in page shows) (qorqut/owner_settings.py). The sign-in part (page link, code field, device code)
// is `signIn`, shared with the notice in Chat (js/agent-runtime.js); `accountNotice` words that notice.
// `rowState`, `hasReady`, `needsPoll` and `controller` are pure or DOM-free (tested with node --test, which also runs
// `mount` and `preflight` against a small fake DOM).
const agentClients = (() => {
  const PROVIDERS = ['claude', 'codex'];
  const NAMES = {claude: 'Claude Code', codex: 'Codex'};
  // Where the owner signs in, said the way the sign-in page says it.
  const ACCOUNTS = {claude: 'your Claude account', codex: 'your ChatGPT account'};
  // Where to get it by hand, offered only when Qorqbot cannot download it for this computer.
  const SITES = {claude: 'claude.com/code', codex: 'github.com/openai/codex'};
  // Read every 2 s for the first 15 minutes of a download or sign-in, then every 15 s for as long as it goes on (a big
  // download on a slow link): the row never freezes. Coming back to the tab reads at the fast rate again.
  const POLL_MS = 2000, SLOW_MS = 15000, POLL_FAILURES = 5, POLL_LIMIT = 450;
  // The first read before the walkthrough waits this long; the dashboard never waits longer for this screen.
  const FIRST_READ_MS = 5000;
  const MANAGED_NOTE = 'Agents use this copy from their next session on.';
  const LATER_KEY = 'qorqbot:agent-setup-later';
  // What a button says while its request is on the way, so it never sits disabled without a reason.
  const BUSY = {download: 'Starting…', connect: 'Opening your browser…', switch: 'Opening sign-in…', cancel_download: 'Cancelling…',
    cancel_connect: 'Cancelling…', use_other: 'Switching…'};
  // Claude's sign-in, when no page opened on this screen: the code the page shows can be pasted here.
  const SIGNIN_FALLBACK = 'Page did not open, or Qorqbot runs on another computer? Open the sign-in page yourself and paste the code it shows.';
  // How long "Switched just now" stays on the row.
  const SWITCHED_MS = 10 * 60 * 1000;
  // The products' marks, drawn before their names (simple-icons, CC0). Claude in its own colour, the OpenAI mark in the text colour.
  const LOGO_PATHS = {
    claude: 'm4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z',
    codex: 'M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z',
  };
  const logoHtml = provider => `<span class="ac-logo" data-provider="${esc(provider)}" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="${LOGO_PATHS[provider] || ''}"/></svg></span>`;

  const item = (view, provider) => (view && view[provider]) || {};
  const phase = it => (it.install && it.install.phase) || 'not_downloaded';
  const signingIn = it => !!(it.installed && it.sign_in && it.sign_in.state === 'connecting');
  const ready = it => !!(it.installed && it.signed_in);
  // One sentence from server text: no stray trailing dots, one full stop.
  // Server text is shown only when it is text: a missing or odd value never prints as "undefined" or "null".
  const str = value => (typeof value === 'string' || typeof value === 'number') ? String(value).trim() : '';
  const sentence = text => { const clean = str(text).replace(/[.\s]+$/, ''); return clean ? clean + '.' : ''; };
  const percent = it => {
    const value = it.install && it.install.progress;
    return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : null;
  };

  // "owner@example.com (Max)": who an account is, or '' when the server named nobody.
  const whoIs = account => {
    const email = str(account && account.email), plan = str(account && account.plan);
    return email ? `${email}${plan ? ` (${plan})` : ''}` : '';
  };
  // When a limit ends, in the client's own words when it sent some ("Nov 28, 7pm (America/New_York)", Codex: "at 3pm"), else from the
  // time. Returns '' when neither is known, so the sentence leaves the time out.
  function resetsPhrase(text, at, now = Date.now()) {
    const own = str(text).replace(/[.\s]+$/, '');
    if (own) return own;
    const when = new Date(str(at));
    if (!str(at) || Number.isNaN(when.getTime())) return '';
    const time = when.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    if (when.toDateString() === new Date(now).toDateString()) return `at ${time}`;
    return `${when.toLocaleDateString([], {month: 'short', day: 'numeric'})}, ${time}`;
  }
  // The sentence about agents after a completed switch (`switched.restarted` and the like are counts). A first sign-in (no account
  // was signed in before: `previous_account` is null) switched nothing, so the row shows only its ready text.
  function switchedNote(switched, now) {
    if (!switched || typeof switched !== 'object') return '';
    if (!switched.previous_account || typeof switched.previous_account !== 'object') return '';
    const at = Date.parse(str(switched.at));
    if (Number.isNaN(at) || now - at > SWITCHED_MS) return '';
    const count = key => Math.max(0, Math.floor(Number(switched[key]) || 0));
    const restarted = count('restarted'), continued = count('continued'), failed = count('failed');
    const lead = switched.changed === false ? 'Signed in again as the same account.' : 'Switched just now.';
    if (switched.state === 'restarting') return `${lead} Restarting agents…`;
    const parts = [];
    if (restarted) parts.push(`${restarted} agent${restarted === 1 ? '' : 's'} restarted`);
    if (continued) parts.push(`${continued} continued where the limit stopped ${continued === 1 ? 'it' : 'them'}`);
    const done = parts.length ? ` ${parts.join(', ')}.` : failed ? '' : switched.changed === false ? '' : ' No agents needed restarting.';
    return `${lead}${done}${failed ? ` ${failed} did not restart.` : ''}`;
  }

  // ---- the sign-in part, shared with the notice in Chat
  // What the running sign-in shows: the page to open, the code to type there (Codex device code) or to paste here (Claude), and
  // whether a pasted code is on its way. Null when no sign-in runs. `pending` is the action sent and not yet answered.
  function signInState(provider, it, pending = null) {
    if (!signingIn(it || {})) return null;
    const info = it.sign_in, url = str(info.page_url);
    const device = info.method === 'device_code' && !!str(info.user_code);
    return {provider, name: NAMES[provider], pageUrl: /^https?:\/\//i.test(url) ? url : '', userCode: device ? str(info.user_code) : '',
      acceptsCode: info.accepts_code === true && !device, codeSubmitted: info.code_submitted === true, busy: pending === 'submit_code',
      switching: info.switching === true, previous: whoIs(info.previous_account) || (it.signed_in ? whoIs(it.account) : '')};
  }
  // The line about the old sign-in, said while a switch is under way.
  const stillSignedIn = s => !s.switching ? '' : s.previous ? `Still signed in as ${s.previous} until you finish.` : 'Your current sign-in stays until you finish.';
  // Only what changes the shape: the code being submitted is updated in place, so the field keeps its text and focus.
  const signInShape = s => JSON.stringify([s.provider, s.pageUrl, s.userCode, s.acceptsCode]);
  // `actions` is the caller's own buttons for this step (Cancel): they sit at the bottom right, before Connect, the way a Signoff
  // puts its quiet button before the filled one.
  function signInHtml(s, actions = '') {
    const link = s.pageUrl ? `<a class="ac-open" href="${esc(s.pageUrl)}" target="_blank" rel="noopener noreferrer" data-ac-open
      aria-label="Open the ${esc(s.name)} sign-in page in a new tab">Open sign-in page</a>` : '';
    const device = s.userCode ? `<span class="ac-usercode"><span class="ac-usercode-label">Code to type on the page</span><code data-ac-user-code>${esc(s.userCode)}</code></span>
      <button type="button" class="ac-copy" data-ac-copy aria-label="Copy the ${esc(s.name)} sign-in code">Copy</button>` : '';
    const field = s.acceptsCode ? `<input type="text" data-ac-code maxlength="512" placeholder="Paste the code from the page" aria-label="Code from the ${esc(s.name)} sign-in page" autocomplete="off" spellcheck="false">` : '';
    const connect = s.acceptsCode ? `<button type="button" class="ac-connect" data-ac-finish aria-label="Connect ${esc(s.name)} with this code">Connect</button>` : '';
    return `${device}${field}<p class="ac-checking" data-ac-checking role="status" hidden>Checking the code…</p>
      <div class="ac-signin-actions">${link}<span class="ac-signin-gap"></span>${actions}${connect}</div>`;
  }
  // The typed text, and whether the field has focus, survive a repaint of whatever holds the field.
  const captureCode = root => {
    const input = root.querySelector('[data-ac-code]');
    return input ? {value: input.value || '', focused: document.activeElement === input, start: input.selectionStart, end: input.selectionEnd} : null;
  };
  function restoreCode(root, kept) {
    const input = kept && root.querySelector('[data-ac-code]');
    if (!input) return;
    input.value = kept.value;
    if (kept.focused && !input.disabled) {
      input.focus({preventScroll: true});
      try { input.setSelectionRange(kept.start, kept.end); } catch (_) { /* The caret goes to the end. */ }
    }
  }
  // Brings the host up to date. The markup is rebuilt only when its shape changes; a code that is being checked only disables the field.
  function paintSignIn(host, s, actions = '') {
    const shape = signInShape(s) + actions;
    if (host.dataset.shape !== shape) {
      const kept = captureCode(host);
      host.dataset.provider = s.provider; host.dataset.shape = shape;
      host.innerHTML = signInHtml(s, actions);
      restoreCode(host, kept);
    }
    const checking = s.codeSubmitted || s.busy, input = host.querySelector('[data-ac-code]'), finish = host.querySelector('[data-ac-finish]');
    if (input) input.disabled = checking;
    if (finish) finish.disabled = checking;
    const note = host.querySelector('[data-ac-checking]'); if (note) note.hidden = !checking;
  }
  // The buttons inside the sign-in part. `submit(provider, code)` sends the code; Copy and the field's Enter key are handled here.
  // Returns true when the event was one of these.
  function signInEvent(event, submit) {
    const host = event.target.closest && event.target.closest('[data-ac-signin]');
    if (!host) return false;
    const finish = () => {
      const input = host.querySelector('[data-ac-code]'), code = input ? String(input.value || '').trim() : '';
      if (!code) { if (input) input.focus(); return; }
      submit(host.dataset.provider, code);
    };
    if (event.type === 'keydown') {
      if (event.key !== 'Enter' || event.isComposing || !event.target.matches('[data-ac-code]')) return false;
      event.preventDefault(); finish(); return true;
    }
    if (event.target.closest('[data-ac-finish]')) { if (!event.target.closest('[data-ac-finish]').disabled) finish(); return true; }
    const copy = event.target.closest('[data-ac-copy]');
    if (copy) {
      const code = host.querySelector('[data-ac-user-code]')?.textContent || '';
      try { void globalThis.navigator?.clipboard?.writeText(code); } catch (_) { /* The code stays on screen to copy by hand. */ }
      copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 2000);
      return true;
    }
    return false;
  }

  // What one row shows. `pending` is the action already sent for this row and not yet answered; `error` the last
  // failed request. Returns {provider, name, key, tone, pill, detail, note, progress, indeterminate, actions, error}.
  function rowState(provider, it, {pending = null, error = '', now = Date.now()} = {}) {
    it = it || {};
    const name = NAMES[provider], account = ACCOUNTS[provider];
    const row = {provider, name, key: '', tone: 'warn', pill: '', detail: '', note: '', notes: [], signin: null, progress: null, indeterminate: false, actions: [], error: error || ''};
    // `label` is what the button says; `aria` names the product too, for a screen reader.
    const ARIA = {download: `Download ${name}`, connect: `Connect ${name}`, cancel_download: `Cancel the ${name} download`,
      cancel_connect: `Cancel signing in to ${name}`, refresh: `Check again for ${name}`, switch: `Switch the ${name} account`};
    const act = (id, label, kind = 'primary') => row.actions.push({id, label, kind, aria: id === 'download' && row.key === 'error' ? `Try again to download ${name}` : ARIA[id]});
    if (signingIn(it)) {
      const s = row.signin = signInState(provider, it, pending);
      // A code to type on the page replaces the browser window; a code to paste here is for when no window opened. The view does
      // not say whether a window opened (Qorqbot may run on another computer), so the words hold either way.
      const help = s.userCode ? 'Open the sign-in page on any device and type the code below. This page updates when you are done.'
        : 'Finish signing in on the sign-in page. This page updates when you are done.';
      const fallback = s.acceptsCode ? SIGNIN_FALLBACK : s.pageUrl && !s.userCode ? 'Page did not open? Open the sign-in page yourself.' : '';
      Object.assign(row, {key: 'connecting', pill: 'Signing in', tone: 'info', detail: help, notes: [fallback, stillSignedIn(s)].filter(Boolean)});
      act('cancel_connect', 'Cancel', 'quiet');
    } else if (phase(it) === 'downloading' && !it.installed) {
      const progress = percent(it);
      Object.assign(row, {key: 'downloading', pill: 'Downloading', tone: 'info', progress, indeterminate: progress === null,
        detail: `Downloading ${name}. Keep Qorqbot open until it finishes.`});
      act('cancel_download', 'Cancel', 'quiet');
    } else if (ready(it)) {
      const info = it.account || {}, email = str(info.email), plan = str(info.plan), version = str(it.version);
      // Signed in with an API key there is no email to show.
      const who = email ? ` as ${email}${plan ? ` (${plan})` : ''}` : info.method === 'api_key' ? ' with an API key' : '';
      Object.assign(row, {key: 'ready', pill: 'Ready', tone: 'ok', detail: `Signed in${who}.${version ? ` Version ${version}.` : ''}`});
      // A session opened before the download finished keeps the copy it started with.
      if (it.source === 'managed') row.notes.push(MANAGED_NOTE);
      const limit = it.limit && it.limit.reached === true;
      if (limit) {
        const resets = resetsPhrase(it.limit.resets_text, it.limit.resets_at, now);
        Object.assign(row, {key: 'limit', pill: 'Limit reached', tone: 'warn',
          detail: `Signed in${who}. Usage limit reached${resets ? `, resets ${resets}` : ''}.`});
        row.notes.unshift(`Agents on ${name} wait until then, or until you switch.`);
      }
      // A sign-in that failed while the old one kept working changes nothing: it is said once, beside the row's own state.
      if (it.sign_in && it.sign_in.state === 'failed') {
        const why = str(it.sign_in.reason).replace(/[.\s]+$/, '');
        row.notes.unshift(`Signing in to another account did not finish${why ? ` (${why})` : ''}. Nothing changed.`);
      }
      const switched = switchedNote(it.switched, now);
      if (switched) row.notes.unshift(switched);
      act('switch', 'Switch account', limit ? 'primary' : 'quiet');
    } else if (it.installed) {
      const failed = it.sign_in && it.sign_in.state === 'failed';
      const why = failed ? str(it.sign_in.reason).replace(/[.\s]+$/, '') : '';
      Object.assign(row, {key: 'connect', pill: 'Sign in needed', tone: 'warn',
        detail: failed ? `Signing in did not finish${why ? ` (${why})` : ''}. Press Connect to try again.`
          : `${name} is installed. Sign in with ${account} to finish.`});
      act('connect', 'Connect');
    } else if (phase(it) === 'unsupported') {
      Object.assign(row, {key: 'unsupported', pill: 'Not available', tone: 'warn',
        detail: `${sentence(it.install.message) || `Qorqbot cannot download ${name} for this computer.`} Install it yourself from ${SITES[provider]}, then press Check again.`});
      act('refresh', 'Check again', 'quiet');
    } else if (phase(it) === 'error') {
      Object.assign(row, {key: 'error', pill: 'Download failed', tone: 'warn',
        detail: `${sentence(it.install.message) || 'The download did not finish.'} Check your internet connection, then try again.`});
      act('download', 'Try again');
    } else if (phase(it) === 'ready') {
      // Downloaded, and the server has not said so yet: the next read does.
      Object.assign(row, {key: 'finishing', pill: 'Finishing', tone: 'info', indeterminate: true, detail: `Almost done. Getting ${name} ready…`});
    } else {
      // The server says why when the copy on this computer cannot be used (too old): that comes before the Download action.
      Object.assign(row, {key: 'download', pill: 'Not installed', tone: 'warn',
        detail: sentence(it.install && it.install.message) || `Qorqbot downloads ${name} for you.`});
      act('download', 'Download');
    }
    row.note = row.notes.join(' ');
    // The code being checked is shown in the sign-in part itself; the row's buttons stay as they are.
    if (pending && pending !== 'submit_code') {
      row.actions = row.actions.map(action => ({...action, busy: action.id === pending, disabled: true,
        label: action.id === pending ? (BUSY[pending] || '…') : action.label}));
      if (!row.actions.some(action => action.busy)) row.actions.push({id: pending, label: BUSY[pending] || '…', kind: 'quiet', busy: true, disabled: true});
    }
    return row;
  }
  const rows = (view, state = {}) => PROVIDERS.map(provider =>
    rowState(provider, item(view, provider), {pending: state.pending && state.pending[provider], error: state.errors && state.errors[provider], now: state.now}));
  const hasReady = view => PROVIDERS.some(provider => ready(item(view, provider)));
  // A download, a sign-in or a finishing download is under way: the view is read again until it settles.
  const needsPoll = view => PROVIDERS.some(provider => {
    const it = item(view, provider);
    // Agents still being restarted after a switch: their counts rise until it is done.
    return signingIn(it) || (it.switched && it.switched.state === 'restarting') || (!it.installed && ['downloading', 'ready'].includes(phase(it)));
  });

  const defaultRead = async () => coreData(await api('/root/agent-clients'));
  // `extra` is what an action carries besides its name: the code of `submit_code`.
  const defaultAct = async (provider, action, extra = {}) => coreData(await post('/root/agent-clients', {provider, action, ...extra}));

  // Holds the view, sends actions and keeps reading while something is under way. No DOM: `onChange(state)` repaints.
  // Answers that arrive after a newer request are dropped, so an old read never undoes a newer answer.
  function controller({read = defaultRead, act = defaultAct, onChange = () => {}, live = () => true, view = null,
    delay = POLL_MS, slow = SLOW_MS, schedule = setTimeout, cancel = clearTimeout} = {}) {
    const state = {view, loading: !view, loadError: '', errors: {}, pending: {}};
    let timer = null, seq = 0, failures = 0, polls = 0, stopped = false;
    const emit = () => { if (!stopped && live()) onChange(state); };
    const settle = () => {
      cancel(timer); timer = null;
      if (stopped || !live() || !state.view || !needsPoll(state.view)) return;
      timer = schedule(() => refresh({quiet: true}), polls++ >= POLL_LIMIT ? slow : delay);
    };
    async function refresh({quiet = false} = {}) {
      // Rows that have left the page (or were stopped) read nothing more.
      if (stopped || !live()) return state.view;
      const mine = ++seq;
      if (!quiet) { state.loading = !state.view; emit(); }
      try {
        const next = await read();
        if (mine !== seq || stopped) return state.view;
        failures = 0; state.view = next; state.loading = false; state.loadError = '';
      } catch (error) {
        if (mine !== seq || stopped) return state.view;
        state.loading = false;
        // A read that fails while something is under way is tried again a few times before the owner is told.
        if (!quiet || ++failures >= POLL_FAILURES) state.loadError = (error && error.message) || 'Could not check Claude Code and Codex.';
        else { emit(); cancel(timer); timer = schedule(() => refresh({quiet: true}), (polls >= POLL_LIMIT ? slow : delay) * 2); return state.view; }
      }
      emit(); settle();
      return state.view;
    }
    async function run(provider, action, extra) {
      if (action === 'refresh') return refresh();
      polls = 0; failures = 0;
      state.pending[provider] = action; state.errors[provider] = ''; emit();
      try {
        const next = await act(provider, action, extra);
        // Reads started before this answer are older than it.
        seq++;
        if (!stopped) { state.view = next; state.loadError = ''; }
      } catch (error) {
        state.errors[provider] = (error && error.message) || `That did not work. Try again.`;
      } finally { delete state.pending[provider]; }
      emit(); settle();
      return state.view;
    }
    // The tab is visible again: read now and go back to the fast rate while something is still under way.
    const resume = () => {
      if (stopped || !live() || !state.view || !needsPoll(state.view)) return Promise.resolve(state.view);
      polls = 0; failures = 0;
      return refresh({quiet: true});
    };
    const stop = () => { stopped = true; cancel(timer); timer = null; };
    return {state, refresh, run, resume, stop};
  }

  const buttonHtml = (row, action) => `<button type="button" class="${action.kind === 'quiet' ? 'quiet' : ''}" data-ac-action="${esc(action.id)}" data-provider="${esc(row.provider)}"${action.disabled ? ' disabled aria-busy="true"' : ''}
    aria-label="${esc(action.busy ? action.label : action.aria || action.label)}">${esc(action.label)}</button>`;
  // Everything that changes the row's shape; the progress bar alone is updated in place so a button keeps focus.
  // The sign-in part counts only by its shape (see signInShape): a code being checked is updated in place.
  const signature = row => JSON.stringify([row.key, row.tone, row.pill, row.detail, row.notes, row.indeterminate, row.error, row.actions, row.signin && signInShape(row.signin)]);
  function rowHtml(row) {
    const bar = row.key === 'downloading' || row.key === 'finishing'
      ? `<div class="ac-progress${row.indeterminate ? ' is-indeterminate' : ''}" role="progressbar" aria-label="${esc(row.name)} download" aria-valuemin="0" aria-valuemax="100"${row.indeterminate ? '' : ` aria-valuenow="${row.progress}"`}><i${row.indeterminate ? '' : ` style="width:${row.progress}%"`}></i></div>
        <p class="ac-percent" data-ac-percent>${row.indeterminate ? '' : row.progress + '%'}</p>` : '';
    return `<div class="ac-main"><div class="ac-head">${logoHtml(row.provider)}<span class="ac-name">${esc(row.name)}</span><span class="ac-pill" data-tone="${esc(row.tone)}">${esc(row.pill)}</span></div>
      <p class="ac-detail">${esc(row.detail)}</p>${row.notes.map(note => `<p class="ac-note">${esc(note)}</p>`).join('')}${row.signin ? `<div class="ac-code" data-ac-signin data-provider="${esc(row.provider)}"></div>` : ''}${bar}${row.error ? `<p class="ac-error" role="alert">${esc(row.error)}</p>` : ''}</div>
      <div class="ac-actions">${row.signin ? '' : rowActionsHtml(row)}</div>`;
  }
  // While a sign-in waits, the row's own buttons (Cancel) belong to the sign-in step, at its bottom right.
  const rowActionsHtml = row => row.actions.map(action => buttonHtml(row, action)).join('');

  // Draws the two rows into `container` and drives them. `options.view` is a read the caller already has,
  // `options.onChange(view)` is called after every paint, `options.search` makes the rows findable by Settings search.
  function mount(container, options = {}) {
    const root = document.createElement('div');
    root.className = 'ac-list';
    root.innerHTML = '<p class="ac-load" data-ac-load role="status">Checking Claude Code and Codex…</p><div class="ac-rows" data-ac-rows></div><p class="ac-sr" data-ac-sr role="status"></p>';
    container.replaceChildren(root);
    const live = () => root.isConnected;
    const load = root.querySelector('[data-ac-load]'), list = root.querySelector('[data-ac-rows]'), announce = root.querySelector('[data-ac-sr]');
    const seen = new Map(), refocus = new Set();
    function paint(state) {
      const loadFailed = !state.view && state.loadError;
      load.hidden = !!state.view;
      load.innerHTML = loadFailed ? `${esc(state.loadError)} <button type="button" class="quiet" data-ac-reload>Try again</button>` : 'Checking Claude Code and Codex…';
      if (!state.view) return;
      const stale = state.loadError ? `<p class="ac-error" role="alert" data-ac-stale>${esc(state.loadError)} <button type="button" class="quiet" data-ac-reload>Try again</button></p>` : '';
      root.querySelector('[data-ac-stale]')?.remove();
      for (const row of rows(state.view, state)) {
        let el = list.querySelector(`[data-ac-row="${row.provider}"]`);
        if (!el) {
          el = document.createElement('div'); el.className = 'ac-row'; el.dataset.acRow = row.provider;
          if (options.search !== false) el.dataset.search = `${row.name} claude codex agents model provider install download sign in connect account switch limit usage ready`.toLowerCase();
          list.append(el);
        }
        const sig = signature(row);
        if (seen.get(row.provider) !== sig) {
          // Focus stays in the row: a button that is replaced by a busy one hands it to the next enabled button.
          if (el.contains(document.activeElement) && document.activeElement.closest('button')) refocus.add(row.provider);
          const first = !seen.has(row.provider);
          const kept = captureCode(el);
          el.dataset.state = row.key; el.dataset.tone = row.tone;
          el.innerHTML = rowHtml(row);
          const slot = el.querySelector('[data-ac-signin]');
          if (slot) { paintSignIn(slot, row.signin, rowActionsHtml(row)); restoreCode(el, kept); }
          const target = refocus.has(row.provider) && el.querySelector('[data-ac-action]:not([disabled])');
          if (target) { target.focus({preventScroll: true}); refocus.delete(row.provider); }
          // The change is spoken once; the first paint is not an announcement.
          if (!first) announce.textContent = `${row.name}: ${row.pill}. ${row.detail}`;
          seen.set(row.provider, sig);
        } else if (row.signin) {
          // The same row again: only the code being checked can have changed.
          paintSignIn(el.querySelector('[data-ac-signin]'), row.signin, rowActionsHtml(row));
        } else if (row.progress !== null) {
          const bar = el.querySelector('.ac-progress');
          if (bar) { bar.setAttribute('aria-valuenow', row.progress); bar.firstElementChild.style.width = row.progress + '%'; }
          const text = el.querySelector('[data-ac-percent]'); if (text) text.textContent = row.progress + '%';
        }
      }
      if (stale) list.insertAdjacentHTML('afterend', stale);
      options.onChange?.(state.view);
      root.dispatchEvent(new CustomEvent('agent-clients-painted', {bubbles: true}));
    }
    const ctl = controller({view: options.view || null, live, onChange: paint, read: options.read, act: options.act,
      delay: options.delay, slow: options.slow, schedule: options.schedule, cancel: options.cancel});
    // Back on this tab: read at once and at the fast rate. The listener goes when the rows leave the page or stop.
    const onVisible = () => {
      if (!live()) { document.removeEventListener('visibilitychange', onVisible); return; }
      if (document.visibilityState === 'visible') void ctl.resume();
    };
    document.addEventListener('visibilitychange', onVisible);
    const stop = () => { ctl.stop(); document.removeEventListener('visibilitychange', onVisible); };
    const submitCode = (provider, code) => { void ctl.run(provider, 'submit_code', {code}); };
    root.addEventListener('keydown', event => { signInEvent(event, submitCode); });
    root.addEventListener('click', event => {
      if (signInEvent(event, submitCode)) return;
      const button = event.target.closest('[data-ac-action]');
      if (button && !button.disabled) { void ctl.run(button.dataset.provider, button.dataset.acAction); return; }
      if (event.target.closest('[data-ac-reload]')) void ctl.refresh();
    });
    if (options.view) paint(ctl.state);
    // The first read decides whether the rows appear; a poll starts by itself when a download is already under way.
    if (!options.view) void ctl.refresh(); else if (needsPoll(options.view)) void ctl.refresh({quiet: true});
    return {root, refresh: ctl.refresh, stop, state: ctl.state};
  }

  // ---- the notice in Chat when an agent's last answer failed for its account (a usage limit, or a sign-in that is no longer accepted)
  // `problem` is `session.managed.account_problem` as agentRuntime.managedOf shapes it, `it` the product's row of the view (or
  // undefined before it is read). `ctx.attempted`: the owner pressed a sign-in button in this notice (an old failed sign-in is not
  // shown to someone who has not tried), `ctx.busy`: the action on its way, `ctx.error`. Returns
  // {key, text, small, error, actions: [{id, label, kind, aria, busy, disabled}], signin}.
  const SWITCH_WORD = {claude: 'Claude', codex: 'ChatGPT'};
  const SHORT_NAMES = {claude: 'Claude', codex: 'Codex'};
  function accountNotice(problem, it, {agent = 'this agent', attempted = false, busy = '', error = '', now = Date.now()} = {}) {
    it = it || {};
    const provider = problem.provider, name = NAMES[provider], limit = problem.kind === 'usage_limit';
    const who = whoIs(problem.account) || whoIs(it.account);
    const resets = limit ? resetsPhrase(problem.resetsText || (it.limit && it.limit.resets_text), problem.resetsAt || (it.limit && it.limit.resets_at), now) : '';
    const other = problem.other && problem.other.ready && NAMES[problem.other.id] ? problem.other : null;
    const notice = {key: '', text: '', small: '', error: error || '', actions: [], signin: signInState(provider, it, busy)};
    const act = (id, label, kind = 'primary', aria = label) => notice.actions.push({id, label, kind, aria});
    const useOther = () => { if (limit && other) act('use_other', `Use ${NAMES[other.id]} for ${agent}`, 'quiet', `Use ${NAMES[other.id]} for ${agent} instead of ${name}`); };
    const start = limit ? 'switch' : 'connect';
    if (notice.signin) {
      const s = notice.signin;
      Object.assign(notice, {key: 'signing', text: s.userCode ? 'Open the sign-in page and type the code shown here.' : 'Finish signing in on the sign-in page.',
        small: [s.acceptsCode ? SIGNIN_FALLBACK : '', stillSignedIn(s)].filter(Boolean).join(' ')});
      act('cancel_connect', 'Cancel', 'quiet', `Cancel signing in to ${name}`);
    } else if (attempted && it.switched && it.switched.state === 'restarting') {
      // The new sign-in is in: the session restarts and this notice goes with the problem.
      const now_ = whoIs(it.switched.account);
      Object.assign(notice, {key: 'switching', text: 'Signed in. Restarting agents…', small: now_ ? `Now signed in as ${now_}.` : ''});
    } else if (attempted && it.sign_in && it.sign_in.state === 'failed') {
      const why = sentence(it.sign_in.reason);
      Object.assign(notice, {key: 'failed', text: 'Signing in did not finish. Nothing changed.',
        small: [who ? `Still signed in as ${who}.` : '', resets ? `The limit resets ${resets}.` : '', why].filter(Boolean).join(' ')});
      act(start, 'Try again', 'primary', `Try signing in to ${name} again`); useOther();
    } else if (limit) {
      Object.assign(notice, {key: 'limit', text: `${SHORT_NAMES[provider]} usage limit reached.${resets ? ` It resets ${resets}.` : ''}`,
        small: `${who ? `Signed in as ${who}. ` : ''}Every agent on ${name} is waiting.`});
      act('switch', `Switch ${SWITCH_WORD[provider]} account`, 'primary', `Switch the ${name} account`); useOther();
    } else {
      Object.assign(notice, {key: 'signed_out', text: `${name} is signed out.`,
        small: who ? `The sign-in for ${who} is no longer accepted.` : 'The sign-in is no longer accepted.'});
      act('connect', 'Sign in again', 'primary', `Sign in to ${name} again`);
    }
    // While a request is on its way every button waits and the one pressed says what it is doing.
    if (busy && busy !== 'submit_code') {
      notice.actions = notice.actions.map(a => ({...a, disabled: true, busy: a.id === busy, label: a.id === busy ? BUSY[busy] || '…' : a.label}));
    }
    return notice;
  }

  const skippedBefore = () => { try { return sessionStorage.getItem(LATER_KEY) === '1'; } catch (_) { return false; } };
  const rememberSkip = () => { try { sessionStorage.setItem(LATER_KEY, '1'); } catch (_) { /* Asked again next time. */ } };

  // The first read, given at most `ms`: the server can take a while to answer (it may check each product's version).
  async function firstRead(read, ms) {
    let timer;
    const late = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('The check took too long.')), ms); });
    try { return await Promise.race([read(), late]); } finally { clearTimeout(timer); }
  }

  // The screen between the Qorqbot sign-in and the walkthrough. Resolves at once when a product is already Ready, and
  // also, without a screen, when the server cannot be read in time or at all or anything here goes wrong: this never
  // keeps the dashboard from loading, and never rejects. Otherwise resolves when the owner continues or chooses
  // "Set up later". Settings always has the same rows. `options` is for tests: read, act, timeout, schedule, cancel.
  async function preflight({timeout = FIRST_READ_MS, ...options} = {}) {
    try {
      if (skippedBefore()) return null;
      let first;
      try { first = await firstRead(options.read || defaultRead, timeout); } catch (_) { return null; }
      if (!first || typeof first !== 'object') return null;
      if (hasReady(first)) return first;
      return await gateScreen(first, options);
    } catch (_) { return null; }
  }

  function gateScreen(first, options) {
    return new Promise(resolve => {
      let gate = null, panel = null, blocked = [], done = false;
      // Whatever ends the screen (a button, or an error while building it) puts the page back the way it was.
      const finish = view => {
        if (done) return; done = true;
        try { if (panel) panel.stop(); } catch (_) { /* Gone anyway. */ }
        try { if (gate) gate.remove(); } catch (_) { /* Gone anyway. */ }
        blocked.forEach(el => { el.inert = false; });
        resolve(view);
      };
      try {
        gate = document.createElement('div');
        gate.className = 'ac-gate';
        gate.innerHTML = `<section class="onboarding-card ac-gate-card" role="dialog" aria-modal="true" aria-labelledby="ac-gate-title" aria-describedby="ac-gate-lead">
          <h2 id="ac-gate-title">get your agents ready</h2>
          <p id="ac-gate-lead">Your agents work through Claude Code or Codex. Set up one of them below. You only do this once.</p>
          <div class="ac-gate-rows" data-ac-gate-rows></div>
          <p class="ac-gate-hint" id="ac-gate-hint" data-ac-gate-hint role="status"></p>
          <div class="ac-gate-actions"><button type="button" class="ghost ac-later" data-ac-later>Set up later</button>
            <button type="button" data-ac-continue aria-describedby="ac-gate-hint" disabled>Continue</button></div>
        </section>`;
        document.body.append(gate);
        // Nothing behind the screen can be reached, by mouse or keyboard, until it is gone. The dashboard notices
        // (js/setup-notice.js) stand above it and stay usable.
        blocked = [...document.body.children].filter(el => el !== gate && !el.inert && el.className !== 'setup-notices');
        blocked.forEach(el => { el.inert = true; });
        const next = gate.querySelector('[data-ac-continue]'), later = gate.querySelector('[data-ac-later]'), hint = gate.querySelector('[data-ac-gate-hint]');
        let last = first;
        const paintGate = view => {
          last = view;
          const ok = hasReady(view), wasOk = !next.disabled;
          next.disabled = !ok; later.hidden = ok;
          hint.textContent = ok ? 'You are set. Continue to your workspace.' : 'Continue unlocks when Claude Code or Codex says Ready.';
          if (ok && !wasOk) next.focus();
        };
        panel = mount(gate.querySelector('[data-ac-gate-rows]'), {...options, view: first, search: false, onChange: paintGate});
        next.onclick = () => finish(last);
        later.onclick = () => { rememberSkip(); finish(last); };
        (gate.querySelector('[data-ac-action]:not([disabled])') || later).focus();
      } catch (_) { finish(null); }
    });
  }

  return {rowState, rows, hasReady, needsPoll, controller, mount, preflight, accountNotice, resetsPhrase, signInState, signInHtml, signInShape, paintSignIn,
    signInEvent, captureCode, restoreCode, logoHtml, PROVIDERS, NAMES, SWITCHED_MS, POLL_MS, SLOW_MS, POLL_LIMIT, FIRST_READ_MS};
})();
if (typeof module !== 'undefined') module.exports = agentClients;
