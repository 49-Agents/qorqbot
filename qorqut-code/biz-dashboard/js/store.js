// ---------------------------------------------------------------- change store
// Stage 2 PR5: the tab-local record of what events.js's stream has said, and
// who wants to know. Not a normalised entity cache (Stage 3's job) -- just a
// per-(kind,scope) version counter and a subscription registry, so a view
// can react to "something I render changed" instead of polling on a timer.
// The ETag cache (js/core.js, PR4) still owns getting the actual fresh data;
// this only ever decides *when* to ask for it again.
//
// `scope` is a business id, or null for "every business". An aggregate view
// (a Global tab, the personal taskbox) subscribes with null and is matched
// by every event; a coarse or genuinely global-scoped write likewise carries
// a null business_id on the wire and matches every subscriber, business-
// scoped or not. Mirrors PR3's server-side ALSO_GLOBAL_KINDS/GLOBAL_ONLY_KINDS
// reasoning: over-matching costs an unnecessary refresh and is always safe;
// under-matching misses a real change and is not.
const storeVersions = new Map();
const storeVersionKey = (kind, scope) => kind + " " + (scope ?? "");
function storeBump(kind, scope) {
  const key = storeVersionKey(kind, scope);
  storeVersions.set(key, (storeVersions.get(key) || 0) + 1);
}
function storeVersion(kind, scope) { return storeVersions.get(storeVersionKey(kind, scope)) || 0; }

const storeSubs = new Set();
const storePending = new Set();
function storeMatches(sub, kind, scope) {
  return sub.kinds.has(kind) && (sub.scope == null || scope == null || sub.scope === scope);
}
function storeFire(sub) {
  if (document.hidden) { storePending.add(sub); return; }
  storePending.delete(sub);
  clearTimeout(sub.timer);
  sub.timer = setTimeout(() => { sub.timer = null; sub.handler(); }, 250);
}
// Views subscribe with the kinds they render and their scope (a business id,
// or null for every business); the store calls `handler` -- debounced 250ms
// per subscription, queued instead while document.hidden and flushed once on
// visibilitychange -- whenever a matching event arrives. Returns an
// unsubscribe function; call it from the view's teardown.
function onChange(kinds, scope, handler) {
  const sub = { kinds: new Set(kinds), scope, handler, timer: null };
  storeSubs.add(sub);
  return () => { storeSubs.delete(sub); storePending.delete(sub); clearTimeout(sub.timer); };
}
// events.js's own ingestion API; a view calls onChange, never these directly.
function storeNotify(kind, scope) {
  storeBump(kind, scope);
  for (const sub of storeSubs) if (storeMatches(sub, kind, scope)) storeFire(sub);
}
function storeInvalidateAll() {
  for (const sub of storeSubs) { for (const kind of sub.kinds) storeBump(kind, sub.scope); storeFire(sub); }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  const due = [...storePending]; storePending.clear();
  due.forEach(storeFire);
});
