// Shared, bounded snapshots for explicitly read-only UI queries. Never cache writes.
// The server is authoritative; a snapshot can paint immediately while refresh runs.
const uiReads = (() => {
  const entries = new Map(), max = 200;
  const copy = value => value == null ? value : structuredClone(value);
  const entry = key => {
    let item = entries.get(key);
    if (!item) { item = {data:null, at:0, generation:0, pending:null, listeners:new Set()}; entries.set(key,item); }
    entries.delete(key); entries.set(key,item);
    if (entries.size > max) for (const [id,candidate] of entries) {
      if (!candidate.pending && !candidate.listeners.size && id !== key) { entries.delete(id); break; }
    }
    return item;
  };
  const publish = item => { for (const listener of item.listeners) listener(copy(item.data)); };
  return {
    peek(key) { return copy(entries.get(key)?.data); },
    subscribe(key, listener) { const item=entry(key); item.listeners.add(listener); return ()=>item.listeners.delete(listener); },
    read(key, loader, {maxAge=12000, force=false}={}) {
      const item=entry(key);
      if (item.pending) return item.pending.then(copy);
      if (!force && item.data !== null && Date.now()-item.at < maxAge) return Promise.resolve(copy(item.data));
      const generation=item.generation;
      const run=Promise.resolve().then(loader).then(data=>{
        if (generation !== item.generation) throw Object.assign(new Error('Read superseded'),{name:'AbortError'});
        item.data=copy(data); item.at=Date.now(); publish(item);
        return data;
      }).catch(error=>{
        if (generation === item.generation && (accessLost(error) || error.code === 'not_found')) {
          item.data=null; item.at=0; publish(item);
        }
        throw error;
      }).finally(()=>{if(item.pending===run)item.pending=null;});
      item.pending=run;
      return run.then(copy);
    },
    invalidate(prefix='', {drop=false}={}) {
      for (const [key,item] of entries) if (key.startsWith(prefix)) {
        item.generation++; item.at=0; item.pending=null;
        if (drop) { item.data=null; publish(item); }
      }
    },
  };
})();
const agentFocusKey = id => 'agent-focus:'+id;
function agentFocusRead(id, options) {
  return uiReads.read(agentFocusKey(id), async()=>{
    const data=coreData(await post('/root/operations/todo_get',{scope:'global',agent_id:id,projection:'focus'}));
    if (data?.agent_id !== id || !data.attention) throw Error('Focus unavailable');
    return data;
  }, options);
}
// Change events invalidate only read snapshots. Components keep their normal refresh lifecycle.
onChange(['agents'], null, ()=>uiReads.invalidate('agent-roster'));
onChange(['attention','tasks','workflows'], null, ()=>uiReads.invalidate('agent-focus:'));
