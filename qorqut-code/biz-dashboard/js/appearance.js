// Theme and text size for this device. Loaded in <head> so the first paint already uses them. Stored per browser
// (localStorage): a choice made on one computer or browser does not change another.
const appearance = (() => {
  const KEY = 'qorqbot:appearance';
  const THEMES = ['dark', 'light', 'system'], SIZES = {small: 0.92, default: 1, large: 1.1};
  const DEFAULTS = {theme: 'dark', textSize: 'default'};
  function read() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
      return {theme: THEMES.includes(saved.theme) ? saved.theme : DEFAULTS.theme,
        textSize: Object.hasOwn(SIZES, saved.textSize) ? saved.textSize : DEFAULTS.textSize};
    } catch (_) { return {...DEFAULTS}; }
  }
  const media = typeof matchMedia === 'function' ? matchMedia('(prefers-color-scheme: light)') : null;
  function apply(prefs = read()) {
    const light = prefs.theme === 'light' || (prefs.theme === 'system' && !!media?.matches);
    document.documentElement.dataset.theme = light ? 'light' : 'dark';
    document.documentElement.style.colorScheme = light ? 'light' : 'dark';
    document.documentElement.style.zoom = SIZES[prefs.textSize] === 1 ? '' : String(SIZES[prefs.textSize]);
  }
  function set(change) {
    const next = {...read(), ...change};
    try { localStorage.setItem(KEY, JSON.stringify(next)); } catch (_) { /* Applies for this page only. */ }
    apply(next);
    return next;
  }
  media?.addEventListener?.('change', () => { if (read().theme === 'system') apply(); });
  apply();
  return {get: read, set, THEMES, SIZES};
})();
if (typeof module !== 'undefined') module.exports = appearance;
