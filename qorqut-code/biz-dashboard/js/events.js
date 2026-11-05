// ---------------------------------------------------------------- change events
// Stage 2 PR5: one EventSource to /api/root/events per tab. The browser
// handles reconnection and resending Last-Event-ID on its own (native
// EventSource behaviour) -- this only has to react to what arrives.
//
// A native reconnect (`onopen` fires again after any drop, by spec) and an
// explicit `reset` event (the server's ring buffer wrapped past what this
// tab last saw, or its per-connection queue overflowed) mean the same
// thing: assume anything may have changed and let every subscriber refresh,
// rather than try to reconstruct exactly what was missed -- consistent with
// this stage's rule that push is an optimisation over polling, never a
// correctness mechanism on its own. Heartbeats are SSE comment lines
// (": heartbeat"), which EventSource never dispatches as a message; there is
// nothing to handle for them here.
let changeEvents = null;
function receiveChanges(payload) {
  if (!payload || (payload.kinds || []).includes('*')) { storeInvalidateAll(); return; }
  // `context_queue` is scope-wide: a blocked head in one business changes every business's queue-waiting status.
  (payload.kinds || []).forEach(kind => storeNotify(kind, kind === 'context_queue' ? null : payload.business_id ?? null));
}
function connectEvents() {
  if (changeEvents) return changeEvents;
  if (typeof embeddedAgentPane !== 'undefined' && embeddedAgentPane && parent !== window) {
    const receive = event => receiveChanges(event.detail);
    parent.addEventListener('qorqut-data-change', receive);
    changeEvents = {close: () => parent.removeEventListener('qorqut-data-change', receive)};
    window.addEventListener('pagehide', () => changeEvents.close(), {once: true});
    return changeEvents;
  }
  const es = new EventSource(evUrl("/root/events"));
  // `onopen` also fires for the very first connect, not just a reconnect.
  // The initial page load already fetches everything it needs on its own;
  // invalidating again immediately after just doubles every subscriber's
  // first fetch for no reason. Only a *re*-open -- after a drop -- means
  // this tab may have missed something and needs the same treatment as an
  // explicit reset.
  let everOpened = false;
  const publish = payload => {
    receiveChanges(payload);
    window.dispatchEvent(new CustomEvent('qorqut-data-change', {detail: payload}));
  };
  es.onopen = () => { if (everOpened) publish(null); everOpened = true; };
  es.addEventListener("reset", () => publish(null));
  es.onmessage = event => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (e) { return; }
    // A wildcard (an external writer invalidated everything -- changes.py's
    // publish_wildcard) means the same thing as an explicit reset: this tab
    // cannot know exactly what changed, so assume everything may have.
    // storeNotify("*", ...) would otherwise match no real subscription's
    // kind set and silently do nothing.
    publish(payload);
  };
  changeEvents = es;
  return es;
}
