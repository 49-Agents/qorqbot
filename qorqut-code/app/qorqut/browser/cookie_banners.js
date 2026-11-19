// Refuses a page's cookie banner: clicks its reject control, never an accept control. Finds none: does nothing.
//
// cookie_banners.py installs this file in every document of a managed browser, in a JavaScript world of its own, so the
// page cannot see or change it. It is our own small logic and copies no consent-blocking list or extension.
//
// What it clicks, and why it is safe to click:
//   * only a visible button or link whose WHOLE label is a refusal ("Reject all", "Nur notwendige", ...). Labels are
//     matched from start to end, so "Accept all", "Accept or reject" and "Reject changes to the draft" never match;
//   * only inside a cookie banner: a box named after a known consent tool (OneTrust, Didomi, Usercentrics, ...) and small,
//     or a box that floats over the page (fixed, sticky, dialog), is small and talks about cookies or consent. A name
//     like "cookie-settings" is not evidence on its own: an admin page's "cookie-settings" section with a "Reject"
//     button is a form, not a banner. A "Decline" button anywhere else is left alone, as is a page that merely links to
//     its cookie policy. A refusal that is a link leaving the page is never followed;
//   * when the banner has no refusal but offers settings, it opens them once and refuses there if a refusal shows up.
//     Only a floating banner's own settings button counts, never a link that leaves the page. It never saves or
//     confirms a selection: that could keep switches that are on.
//
// Known limits: a banner drawn in a closed shadow tree is out of reach; settings that open in another frame are not
// followed; a wall that only offers "accept or pay" is left as it is.
//
// It looks a few times in the first twenty seconds of a document, because banners arrive late, and then stops. One refusal
// a document.
(() => {
  // Once a document. The mark is on the document, not the window: a new tab's first page reuses the blank page's window.
  if (document.qorqutCookieBanner) return;
  document.qorqutCookieBanner = true;

  const LOOK_AFTER_MS = [0, 500, 1000, 2000, 3500, 5000, 8000, 12000, 20000];
  const LOOK_AFTER_SETTINGS_MS = [300, 800, 1500, 3000, 6000];
  const BANNER_TEXT_LIMIT = 3000;       // characters; a first-layer banner is a few hundred, a whole page is far more
  const BANNER_CONTROL_LIMIT = 25;      // buttons and links; a page that happens to float (an app shell) has many more
  const EXPLAINING_LENGTH = 40;         // "We use cookies to ..." is a sentence; a footer's "Cookie policy" link is not
  const LABEL_LIMIT = 60;

  const COOKIE = /cookie|çerez|куки|ciastecz|informasjonskaps|evästei|kakor\b|kolačić|sušenk|süti/i;
  // Names of consent tools: specific enough to count on their own. Names a site may give any section ("cookie-settings",
  // "gdpr") only say where to look, and a box with one must also float and talk about consent.
  const VENDOR = /onetrust|ot-sdk|didomi|usercentrics|cybot|truste(?![a-z])|trustarc|sp_message|qc-cmp|fc-consent|iubenda|klaro|osano|cmplz|complianz|borlabs|cky-/i;
  const NAMED = /cookie|gdpr|consent-(banner|bar|modal|popup|dialog|manager|layer)|privacy-(banner|bar|popup)|cc-(window|banner)/i;
  const CONSENT_WORDING = /cookie|consent|privacy|gdpr|tracking|çerez|куки|ciastecz|einwilligung|datenschutz/i;
  const CONTROLS = 'button, a, [role="button"], input[type="button"], input[type="submit"]';

  // Whole-label patterns. Labels arrive lower-cased with punctuation turned into single spaces.
  const REJECT = [
    // English
    /^(reject|decline|refuse|deny|disagree)( to)?( all)?( (optional|non essential|nonessential|additional|unnecessary|analytics|tracking|third party))?( cookies)?$/,
    /^(i )?(do not|don t) (accept|agree|consent)$/,
    /^(accept|allow|use|continue with)( only)? (the )?(strictly )?(necessary|essential|required)( cookies)?( only)?$/,
    /^(only )?(strictly )?(necessary|essential|required)( cookies)? only$|^only (strictly )?(necessary|essential|required)( cookies)?$/,
    /^continue without (accepting|agreeing|consent)$/,
    // German
    /^(alle |alles )?(ablehnen|verweigern)$|^(cookies |optionale cookies )ablehnen$|^nicht (akzeptieren|zustimmen)$/,
    /^nur (technisch )?(notwendige|erforderliche|essenzielle)( cookies)?( akzeptieren| zulassen| erlauben| verwenden)?$/,
    /^weiter ohne (zustimmung|einwilligung|zu akzeptieren)$/,
    // French
    /^(tout |tous )?(refuser|rejeter)( tout| tous)?( les cookies)?$|^je refuse$|^continuer sans accepter$/,
    /^(accepter )?(uniquement |seulement )?(les )?cookies (nécessaires|essentiels)( uniquement| seulement)?$/,
    // Spanish
    /^(rechazar|denegar)( todo| todas| todos)?( las cookies)?$|^no acepto$|^continuar sin aceptar$/,
    /^(solo|sólo|únicamente) (las )?(cookies )?(necesarias|esenciales)$/,
    // Italian
    /^(rifiuta|rifiuto|nega)( tutto| tutti)?( i cookie)?$|^continua senza accettare$|^solo (cookie )?(necessari|essenziali|tecnici)$/,
    // Dutch
    /^(alles |alle )?(weigeren|afwijzen)( alles| alle)?( cookies)?$|^alleen (noodzakelijke|functionele|essentiële)( cookies)?$/,
    // Portuguese
    /^(rejeitar|recusar)( tudo| todos| todas)?( os cookies)?$|^(apenas|somente|só) (os )?(cookies )?(necessários|essenciais)$/,
    // Polish
    /^(odrzuć|odrzucam|odmawiam|nie zgadzam się)( wszystko| wszystkie)?$|^tylko (niezbędne|wymagane|konieczne)( pliki cookie)?$/,
    // Turkish
    /^(tümünü |hepsini )?(reddet|reddediyorum)$|^kabul etmiyorum$|^(sadece|yalnızca) (gerekli|zorunlu)( çerezler| çerezleri)?( kabul et| kullan)?$/,
    // Russian
    /^(отклонить|отказаться)( все| всё| от всех)?$|^не принимать$|^только (необходимые|обязательные)( файлы cookie)?$/,
    // Swedish, Danish, Norwegian
    /^(avvisa|neka|afvis|afslå|avvis|avslå)( alla| alle)?$|^(endast|bara|kun) (nödvändiga|nødvendige)( cookies| kakor| informasjonskapsler)?$/,
  ];
  const SETTINGS = [
    /^(manage|customi[sz]e|configure|change|adjust|set|review|edit|show|view|more)( my| your| cookie| cookies| privacy| consent)* (options|preferences|settings|choices|purposes|cookies)$/,
    /^(cookie |cookies |privacy )?(options|preferences|settings)$|^customi[sz]e$|^let me choose$/,
    /^(cookie |datenschutz)?(einstellungen|optionen)( verwalten| anpassen)?$|^anpassen$|^mehr optionen$|^zwecke anzeigen$/,
    /^(paramétrer|personnaliser|gérer|configurer)( mes| les| vos)?( choix| cookies| préférences| options| paramètres)?$/,
    /^(paramètres|préférences|options)( des cookies| de confidentialité)?$/,
    /^(configurar|gestionar|personalizar|administrar)( las| mis)?( cookies| opciones| preferencias)?$|^(configuración|preferencias|opciones)( de cookies)?$/,
    /^(gestisci|personalizza|configura)( le)?( opzioni| preferenze| cookie)?$|^(impostazioni|preferenze)( dei cookie)?$/,
    /^(instellingen|voorkeuren|opties)( beheren| aanpassen)?$|^(beheer|wijzig) (instellingen|voorkeuren|opties)$|^aanpassen$/,
  ];

  const label = (control) => (control.innerText || control.value || control.getAttribute('aria-label') || '')
    .toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();

  const matches = (patterns, text) => text.length <= LABEL_LIMIT && patterns.some((pattern) => pattern.test(text));

  // Every control of the document, including those inside open shadow trees (several consent tools draw there).
  const controls = (root, found = []) => {
    found.push(...root.querySelectorAll(CONTROLS));
    for (const element of root.querySelectorAll('*')) {
      if (element.shadowRoot) controls(element.shadowRoot, found);
    }
    return found;
  };

  const shown = (control) => !control.disabled && control.getClientRects().length > 0
    && control.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });

  // The element above, stepping out of a shadow tree to the element that hosts it.
  const above = (node) => node.parentElement || (node.getRootNode() instanceof ShadowRoot ? node.getRootNode().host : null);

  const within = (box) => box.shadowRoot || box;

  const explainsCookies = (box) => {
    const walker = document.createTreeWalker(within(box), NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.nodeValue.trim();
      if (text.length >= EXPLAINING_LENGTH && COOKIE.test(text)) return true;
    }
    return false;
  };

  const small = (box) => within(box).textContent.length <= BANNER_TEXT_LIMIT
    && within(box).querySelectorAll(CONTROLS).length <= BANNER_CONTROL_LIMIT;

  const floats = (box) => {
    if (box.matches('dialog, [role="dialog"], [role="alertdialog"], [aria-modal="true"]')) return true;
    const position = getComputedStyle(box).position;
    return position === 'fixed' || position === 'sticky';
  };

  // Is this control part of a cookie banner? A named box counts only when ``named`` allows it. ``opened`` says we opened the
  // banner's settings ourselves, so the box may be a long list of purposes and partners.
  const inBanner = (control, named, opened) => {
    for (let box = above(control); box && box !== document.body && box !== document.documentElement; box = above(box)) {
      const name = box.id + ' ' + (box.getAttribute('class') || '');
      if (named && VENDOR.test(name) && (opened || small(box))) return true;
      if (named && NAMED.test(name) && floats(box) && (opened || small(box)) && CONSENT_WORDING.test(within(box).textContent)) return true;
      if (floats(box) && explainsCookies(box) && (opened || small(box))) return true;
    }
    // Some consent tools put the banner in a frame of its own: there the whole document is the banner.
    return window !== window.top && explainsCookies(document.body) && (opened || small(document.body));
  };

  // A link that leaves the page. A footer's "Cookie settings" is one, and following it would take the agent's page away;
  // so would a refusal drawn as a link to another address.
  const leaves = (control) => control.tagName === 'A' && control.hasAttribute('href')
    && !/^(#|javascript:)/i.test(control.getAttribute('href').trim());

  let opened = false;
  let finished = false;
  const look = () => {
    if (finished || !document.body) return;
    const candidates = controls(document).filter(shown).map((control) => [control, label(control)]);
    const reject = candidates.find(([control, text]) => matches(REJECT, text) && !leaves(control) && inBanner(control, true, opened));
    if (reject) {
      finished = true;
      reject[0].click();
      return;
    }
    // Opening settings is held to the stricter test: sites name plenty of ordinary links "cookie settings".
    const settings = opened ? null : candidates.find(([control, text]) => matches(SETTINGS, text) && !leaves(control)
      && inBanner(control, false, false));
    if (settings) {
      opened = true;
      settings[0].click();
      LOOK_AFTER_SETTINGS_MS.forEach((delay) => setTimeout(look, delay));
    }
  };

  const start = () => LOOK_AFTER_MS.forEach((delay) => setTimeout(look, delay));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
