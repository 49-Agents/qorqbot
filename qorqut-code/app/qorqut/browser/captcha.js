// What the captcha command runs inside a page (captcha.py says when and why). One expression: an object of functions.
//
// scan and control only read the document, so they run in a JavaScript world of their own that the page cannot see.
// state, deliver and v3 have to reach what the page itself defined (its callbacks, the widget's own objects), so they run
// in the page's world; there `this` is the widget's frame element, or for v3 any element of the document.
(() => {
  // The form fields each widget leaves its answer in. The first is its own; the others it adds for compatibility.
  const FIELDS = {
    recaptcha: ['g-recaptcha-response'],
    hcaptcha: ['h-captcha-response', 'g-recaptcha-response'],
    turnstile: ['cf-turnstile-response', 'g-recaptcha-response'],
  };
  const TOKEN_LIFE = 110000;   // Milliseconds. An answer is valid for about two minutes; an older one is not handed out.

  // The element the page put the widget in. Cloudflare keeps its frame in a closed shadow tree, whose host is in the page.
  function holder(frame) {
    let node = frame;
    while (node.getRootNode() instanceof ShadowRoot) node = node.getRootNode().host;
    return node.closest('[data-sitekey]') || node;
  }

  // The answer fields nearest to the widget, so that a second widget in another form keeps its own.
  function fields(from, names) {
    const selector = names.map((name) => `[name="${name}"]`).join(',');
    for (let node = from; node; node = node.parentElement) {
      const found = node.querySelectorAll(selector);
      if (found.length) return [...found];
    }
    return [];
  }

  // The callbacks a page passed to grecaptcha.render() for this site key. reCAPTCHA keeps them in its client records,
  // a few levels down under names that change with every release, so they are found by shape.
  function registered(clients, sitekey) {
    const found = [], seen = new Set(), queue = [[clients, 0]];
    while (queue.length) {
      const [value, depth] = queue.shift();
      if (!value || typeof value !== 'object' || depth > 6 || seen.has(value) || seen.size > 5000) continue;
      seen.add(value);
      try {
        if (value instanceof Node || value === window) continue;
        if (value.sitekey === sitekey && value.callback) found.push(value.callback);
        for (const key of Object.keys(value)) queue.push([value[key], depth + 1]);
      } catch (error) { /* an object of another site: not ours to read */ }
    }
    return found;
  }

  return {
    // Any document: the site keys of an invisible reCAPTCHA v3 it loads as [key, enterprise], and whether the document
    // is Cloudflare's own "checking your browser" page.
    scan() {
      const v3 = [];
      for (const script of document.querySelectorAll('script[src*="recaptcha/"][src*="render="]')) {
        let address;
        try { address = new URL(script.src); } catch (error) { continue; }
        const key = address.searchParams.get('render');
        if (key && key !== 'explicit' && key !== 'onload') v3.push([key, /\/enterprise\.js$/.test(address.pathname)]);
      }
      const challenge = !!document.querySelector('script[src*="/cdn-cgi/challenge-platform/"][src*="/orchestrate/"], #challenge-form')
        || [...document.scripts].some((script) => !script.src && script.textContent.includes('_cf_chl_opt'));
      return {v3, challenge};
    },

    // Inside a widget's own frame: where its checkbox is, and whether it is ticked. Null when it shows none (Cloudflare
    // draws its checkbox in a closed shadow tree; the caller then clicks where that widget always has it).
    control() {
      const box = document.querySelector('#recaptcha-anchor, #checkbox, [role="checkbox"], input[type="checkbox"]');
      if (!box) return null;
      const rect = box.getBoundingClientRect();
      return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, drawn: rect.width > 0 && rect.height > 0,
              checked: box.getAttribute('aria-checked') === 'true' || box.checked === true};
    },

    // Whether the widget has its answer, and what the page declared for it.
    state(kind) {
      const box = holder(this);
      return {answered: fields(box, FIELDS[kind].slice(0, 1)).some((field) => field.value) || !!this.getAttribute('data-hcaptcha-response'),
              sitekey: box.getAttribute('data-sitekey'), action: box.getAttribute('data-action'), cdata: box.getAttribute('data-cdata')};
    },

    // Give the page a solver's answer the three ways pages take one: the form field, the callback, the widget's getResponse().
    deliver(kind, token, sitekey) {
      const box = holder(this), targets = fields(box, FIELDS[kind]);
      for (const field of targets) {
        field.value = token;
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
      }
      if (kind === 'hcaptcha') this.setAttribute('data-hcaptcha-response', token);
      const callbacks = new Set(box.getAttribute('data-callback') ? [box.getAttribute('data-callback')] : []);
      if (kind === 'recaptcha') for (const callback of registered(window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients, sitekey)) callbacks.add(callback);
      let called = 0;
      for (const callback of callbacks) {
        const run = typeof callback === 'function' ? callback : window[callback];
        if (typeof run !== 'function') continue;
        called += 1;
        try { run(token); } catch (error) { /* the page's own fault; the answer is still in its fields */ }
      }
      const widgets = {recaptcha: [window.grecaptcha, window.grecaptcha && window.grecaptcha.enterprise],
                       hcaptcha: [window.hcaptcha], turnstile: [window.turnstile]}[kind];
      let patched = 0;
      for (const widget of widgets) {
        if (!widget || typeof widget.getResponse !== 'function') continue;
        widget.getResponse = () => token;
        patched += 1;
      }
      return {fields: targets.length, called, patched};
    },

    // reCAPTCHA v3 shows nothing: the page asks grecaptcha.execute(key, {action}) for a score token when a form is sent.
    // A solver needs that action, which only the page's own call reveals. So execute is wrapped: it notes the action of
    // every call, and once a solver's answer for that action is here, hands it out instead, once.
    // Without `token`: wrap, and report the action seen so far. With one: keep it for the next call with `action`.
    // Null when the page has no reCAPTCHA to wrap.
    v3(token, action) {
      const apis = [window.grecaptcha, window.grecaptcha && window.grecaptcha.enterprise]
        .filter((api) => api && typeof api.execute === 'function');
      if (!apis.length) return null;
      const memo = apis.map((api) => api.execute.qorqut).find(Boolean) || {seen: false, action: ''};
      for (const api of apis) {
        if (api.execute.qorqut) continue;
        const real = api.execute;
        api.execute = function (key, options) {
          memo.seen = true;
          memo.action = (options && options.action) || '';
          if (memo.token && memo.action === memo.answers && Date.now() < memo.until) {
            const answer = memo.token;
            memo.token = null;
            return Promise.resolve(answer);
          }
          return real.apply(this, arguments);
        };
        api.execute.qorqut = memo;
      }
      if (token) Object.assign(memo, {token, answers: action, until: Date.now() + TOKEN_LIFE});
      return {seen: memo.seen, action: memo.action};
    },
  };
})()
