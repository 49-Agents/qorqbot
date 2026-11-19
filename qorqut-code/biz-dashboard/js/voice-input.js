// Voice input for the Chat composer, and the owner's sign-in with OpenRouter that turns it on.
// `voiceInputModel` is pure (unit tested with `node --test biz-dashboard/tests/*.test.js`). `openrouterConnect` starts the
// sign-in and finishes it when OpenRouter sends the browser back. `voiceInput.attach` adds the microphone button to one
// composer; the recording goes to the OpenRouter extension's own route and its text is inserted where the cursor is.
// The hotkey is Ctrl+Shift+Space: it starts and stops a recording in the composer being typed in, or the one on screen.
const voiceInputModel = (() => {
  // What a browser can record, most compatible first; the service accepts each by its plain media type.
  const RECORDINGS = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
  const MAX_SECONDS = 900;
  // Speech needs little; this keeps a full 15-minute recording near 4 MB, far under what the service accepts.
  const BITS_PER_SECOND = 32000;
  const isHotkey = event => !!event && event.code === 'Space' && event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey
    && !event.repeat && !event.isComposing;
  const hotkeyLabel = platform => /mac|iphone|ipad/i.test(platform || '') ? '⌃⇧Space' : 'Ctrl+Shift+Space';
  // The plain media type of a recording, without its codec parameters.
  const mediaType = type => String(type || '').split(';', 1)[0].trim().toLowerCase();
  // The first recording format this browser supports, or '' to let the browser choose its own.
  const pickRecording = supports => RECORDINGS.find(type => { try { return supports(type); } catch (_) { return false; } }) || '';
  // Put spoken text where the selection is, with one space between it and the words around it.
  // Returns the new draft and where the cursor goes; text beyond `maxLength` is not inserted.
  function insert(draft, start, end, spoken, maxLength = Infinity) {
    const value = String(draft ?? ''), text = String(spoken ?? '').trim();
    const from = Math.max(0, Math.min(start ?? value.length, value.length)), to = Math.max(from, Math.min(end ?? from, value.length));
    if (!text) return {value, cursor: to, inserted: ''};
    const before = value.slice(0, from), after = value.slice(to);
    const lead = before && !/\s$/.test(before) ? ' ' : '', tail = after && !/^[\s.,;:!?)]/.test(after) ? ' ' : '';
    const room = Math.max(0, maxLength - before.length - after.length);
    const inserted = (lead + text + tail).slice(0, room);
    return {value: before + inserted + after, cursor: before.length + inserted.length, inserted};
  }
  // What to tell the owner when the microphone cannot be used.
  function microphoneProblem(error) {
    const name = error?.name || '';
    if (name === 'NotAllowedError' || name === 'SecurityError') return 'Allow the microphone for this page to use voice input.';
    if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No microphone was found.';
    if (name === 'NotReadableError') return 'The microphone is in use by another app.';
    return 'The microphone could not be started.';
  }
  return {RECORDINGS, MAX_SECONDS, BITS_PER_SECOND, isHotkey, hotkeyLabel, mediaType, pickRecording, insert, microphoneProblem};
})();
if (typeof module !== 'undefined') module.exports = voiceInputModel;

// The owner's sign-in with OpenRouter. The service keeps the secret half of the sign-in; this page only opens the address
// it returns and hands back the one-time code OpenRouter adds to the dashboard's address.
const openrouterConnect = (() => {
  const PENDING = 'openrouter-connect';
  const route = '/root/extensions/openrouter/connect';
  const remember = value => { try { value ? sessionStorage.setItem(PENDING, '1') : sessionStorage.removeItem(PENDING); } catch (_) { /* Private mode. */ } };
  const pending = () => { try { return sessionStorage.getItem(PENDING) === '1'; } catch (_) { return false; } };
  // Opens OpenRouter in this tab; it sends the browser back here when the owner approves.
  async function start() {
    const data = coreData(await post(route, {action: 'start'}));
    remember(true);
    window.location.assign(data.url);
  }
  // On the way back from OpenRouter the address carries `?code=`. Only the tab that started a sign-in finishes one.
  async function finishFromAddress() {
    const address = new URL(window.location.href), code = address.searchParams.get('code');
    if (!code || !pending()) return false;
    remember(false);
    address.searchParams.delete('code'); address.searchParams.delete('state');
    window.history.replaceState(window.history.state, '', address.pathname + address.search + address.hash);
    try {
      await post(route, {action: 'finish', code});
      voiceInput.refresh();
      toast('OpenRouter is connected. Voice input is on.');
      return true;
    } catch (failure) {
      toast(failure.message || 'OpenRouter could not be connected. Try again in Settings, Connections.');
      return false;
    }
  }
  return {start, finishFromAddress};
})();

const voiceInput = (() => {
  const ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/></svg>';
  const controllers = new Set();
  let status = null, reading = null;
  // Whether the OpenRouter extension is part of this Qorqbot and connected: 'connected', 'not_set_up' or 'not_installed'.
  function readStatus() {
    reading ||= api('/root/connectors').then(result => {
      const item = (coreData(result).connectors || []).find(entry => entry.id === 'openrouter');
      status = item ? item.status : 'not_installed';
    }).catch(() => { status = status || 'not_installed'; }).finally(() => { reading = null; controllers.forEach(c => c.paint()); });
    return reading;
  }
  // Reads again without hiding the button meanwhile; the last known answer stands until the new one arrives.
  const refresh = () => readStatus();
  const say = text => { if (typeof toast === 'function') toast(text); };

  // options: {form, input, before, maxLength}. The button is inserted before `before` inside `form`.
  function attach(options) {
    const {form, input} = options;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'gchat-mic quiet'; button.hidden = true; button.innerHTML = ICON;
    form.insertBefore(button, options.before || null);
    let state = 'idle', recorder = null, stream = null, chunks = [], timer = 0, locked = false, lockedWhy = '', cancelled = false, disposed = false;
    const label = () => state === 'recording' ? 'Stop and insert what you said' : state === 'working' ? 'Turning your words into text…'
      : locked && state === 'idle' && lockedWhy ? `Voice input is off. ${lockedWhy}` : `Voice input (${voiceInputModel.hotkeyLabel(navigator.platform)})`;
    function paint() {
      if (disposed) return;
      button.hidden = !status || status === 'not_installed';
      button.dataset.state = state;
      button.disabled = state === 'working' || (state === 'idle' && (locked || input.disabled));
      button.setAttribute('aria-pressed', state === 'recording' ? 'true' : 'false');
      button.setAttribute('aria-label', label()); button.title = label();
    }
    function release() {
      clearTimeout(timer); timer = 0;
      stream?.getTracks().forEach(track => track.stop());
      stream = recorder = null; chunks = [];
    }
    async function finish(type) {
      const blob = new Blob(chunks, {type});
      release();
      if (cancelled || !blob.size) { state = 'idle'; paint(); return; }
      state = 'working'; paint();
      try {
        const data = coreData(await api('/root/extensions/openrouter/transcribe',
          {method: 'POST', headers: {'Content-Type': voiceInputModel.mediaType(type) || 'audio/webm'}, body: blob}));
        if (disposed) return;
        if (!String(data.text || '').trim()) say('Nothing was heard. Try again a little closer to the microphone.');
        else {
          const next = voiceInputModel.insert(input.value, input.selectionStart, input.selectionEnd, data.text, options.maxLength);
          input.value = next.value; input.setSelectionRange(next.cursor, next.cursor);
          input.dispatchEvent(new Event('input', {bubbles: true}));
          if (!next.inserted.trim()) say('The message is full. Send it or shorten it first.');
        }
      } catch (failure) { if (!disposed) say(failure.message || 'Your words could not be turned into text. Try again.'); }
      state = 'idle'; paint();
      if (!disposed && !input.disabled) input.focus();
    }
    async function start() {
      if (state !== 'idle' || locked || input.disabled) return;
      if (status !== 'connected') await refresh();
      if (disposed) return;
      if (status !== 'connected') { say('Voice input needs OpenRouter. Connect it in Settings, Connections.'); return; }
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
        say(window.isSecureContext ? 'This browser cannot record audio.' : 'Voice input needs this page opened on this computer or over https.');
        return;
      }
      state = 'working'; paint();
      try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
      catch (failure) { state = 'idle'; paint(); say(voiceInputModel.microphoneProblem(failure)); return; }
      if (disposed) { release(); return; }
      const wanted = voiceInputModel.pickRecording(type => MediaRecorder.isTypeSupported(type));
      try { recorder = new MediaRecorder(stream, {audioBitsPerSecond: voiceInputModel.BITS_PER_SECOND, ...(wanted ? {mimeType: wanted} : {})}); }
      catch (failure) { release(); state = 'idle'; paint(); say('This browser cannot record audio.'); return; }
      chunks = []; cancelled = false;
      const active = recorder;
      active.addEventListener('dataavailable', event => { if (event.data?.size) chunks.push(event.data); });
      active.addEventListener('stop', () => { void finish(active.mimeType || wanted || 'audio/webm'); });
      active.addEventListener('error', () => { cancelled = true; say('The recording stopped unexpectedly.'); });
      active.start();
      timer = setTimeout(() => stop(), voiceInputModel.MAX_SECONDS * 1000);
      state = 'recording'; paint();
    }
    function stop(discard = false) {
      if (state !== 'recording' || !recorder) return;
      cancelled = discard;
      clearTimeout(timer); timer = 0;
      if (recorder.state !== 'inactive') recorder.stop();
    }
    const toggle = () => { if (state === 'recording') stop(); else void start(); };
    const onClick = () => toggle();
    // Escape discards a recording in progress without touching the draft.
    const onKey = event => { if (event.key === 'Escape' && state === 'recording') { event.preventDefault(); event.stopPropagation(); stop(true); } };
    button.addEventListener('click', onClick);
    form.addEventListener('keydown', onKey);
    const controller = {
      paint, toggle,
      get input() { return input; },
      get usable() { return !disposed && !button.hidden && form.isConnected && form.getClientRects().length > 0; },
      get busy() { return state !== 'idle'; },
    };
    controllers.add(controller);
    if (status) paint(); else void readStatus();
    return {
      button,
      // The composer's own lock (no session to send to, a send in flight) also locks the microphone.
      setLocked(value, why = '') { locked = !!value; lockedWhy = locked ? String(why || '') : ''; paint(); },
      dispose() {
        disposed = true; controllers.delete(controller);
        stop(true); release();
        button.removeEventListener('click', onClick); form.removeEventListener('keydown', onKey);
        button.remove();
      },
    };
  }

  // The composer the hotkey acts on: one that is recording, else the one being typed in, else the last one on screen.
  function target() {
    const usable = [...controllers].filter(c => c.usable);
    return usable.find(c => c.busy) || usable.find(c => c.input === document.activeElement) || usable.at(-1) || null;
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('keydown', event => {
      if (!voiceInputModel.isHotkey(event)) return;
      // An open dialog is not a place to dictate into a composer behind it.
      if (event.target instanceof Element && event.target.closest('[role="dialog"]')) return;
      const controller = target();
      if (!controller) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!controller.busy && document.activeElement !== controller.input && !controller.input.disabled) controller.input.focus();
      controller.toggle();
    }, true);
    // Connecting or removing OpenRouter in Settings changes whether the button is shown.
    document.addEventListener('connectors-painted', () => { void refresh(); });
  }
  return {attach, refresh};
})();

if (typeof window !== 'undefined' && typeof post === 'function') void openrouterConnect.finishFromAddress();
