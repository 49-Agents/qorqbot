// Threads the owner pinned to Focus's top row, after the open side chats. A pin is owner presentation
// saved on the server (/root/thread-pins); opening one reads the record through its ordinary access.
const threadPins = (() => {
  let items = [], loaded = false, loading = null, error = '', writes = Promise.resolve();
  const listeners = new Set();
  const canonical = s => ({type: String(s.type), id: String(s.id), business_id: s.business_id ?? null});
  const key = s => `${s.type}:${s.business_id ?? 'global'}:${s.id}`;
  const notify = () => { for (const fn of [...listeners]) { try { fn(); } catch (_) { /* one view cannot stop the others */ } } };
  function accept(data) {
    if (!Array.isArray(data?.threads)) throw new Error('Invalid pinned threads');
    items = data.threads; loaded = true; error = '';
  }
  function load() {
    if (loading) return loading;
    loading = (async () => {
      try { accept(coreData(await api('/root/thread-pins'))); }
      catch (e) { error = e.message || 'Pinned threads could not be read.'; }
      finally { loading = null; notify(); }
    })();
    return loading;
  }
  // Writes are serial and set a desired state, so a retry or a double click never flips a pin back.
  function set(source, title, pinned) {
    const s = canonical(source), name = String(title || '').trim().slice(0, 200);
    items = items.filter(item => key(item.source) !== key(s));
    if (pinned) items = [...items, {source: s, title: name}];
    notify();
    const run = writes.then(async () => {
      try { accept(coreData(await post('/root/thread-pins', {source: s, title: name, pinned}))); notify(); return true; }
      catch (e) { toast(e.message || (pinned ? 'Could not pin this thread.' : 'Could not unpin this thread.')); await load(); return false; }
    });
    writes = run.catch(() => {});
    return run;
  }
  // A pinned thread opens only in Chat; the record dashboard keeps its own selection.
  async function open(item) {
    const pane = typeof _agpad === 'undefined' ? null : _agpad;
    if (!pane || !pane.focusMount?.isConnected) return;
    try {
      const opened = await agfocusPairedSource({source:item.source,label:item.title || null},null);
      if (opened === false) toast('This thread cannot open yet. Finish the current conversation action and try again.');
      return opened;
    } catch (e) { toast(e.message || 'This thread is unavailable.'); return false; }
  }
  return {load, set, open, key, list: () => items, isLoaded: () => loaded, error: () => error,
    isPinned: source => items.some(item => key(item.source) === key(canonical(source))),
    find: k => items.find(item => key(item.source) === k) || null,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }};
})();
