// Global context base readers. Root scope is explicit in every operation path;
// this view never invents a business slug or changes the selected business.
let globalContextBaseGeneration = 0;
let globalContextPendingEndpointSnapshot = null;
const globalContextObjectRoute = ref => `#/global/context/object/${encodeURIComponent(ref)}`;
const globalContextBaseRoute = page => `#/global/context/${page}`;

function globalContextBaseCurrent(generation, host, route, signal) {
  return generation === globalContextBaseGeneration && host.isConnected && !signal.aborted &&
    state.scope === 'global' && state.globalTab === 'context' && location.hash === route;
}

function globalContextBaseRead(operation, args, signal) {
  return pageReadPost(`/root/operations/${operation}`, args, signal).then(coreData);
}

function renderGlobalContextBase(host, page) {
  const route = location.hash.split('/')[3] || 'graph';
  if (route !== 'object') globalContextPendingEndpointSnapshot = null;
  if (route === 'people') return renderGlobalContextPeople(host);
  if (route === 'object') return renderGlobalContextObject(host);
  return renderGlobalContextGraph(host);
}

function renderGlobalContextGraph(host) {
  const generation = ++globalContextBaseGeneration, route = location.hash, signal = pageReads.signal;
  host.innerHTML = `<section class="global-context-base"><header class="global-context-base-head"><h2>Graph</h2><p>Objects and their stored relationships across Global context.</p></header>
    <div class="global-context-base-status" role="status">Loading the Global context graph…</div>
    <button type="button" class="bt global-context-base-reload" hidden>Reload graph</button><div class="global-context-graph-host"></div></section>`;
  const status = host.querySelector('.global-context-base-status'), reload = host.querySelector('.global-context-base-reload');
  const graphHost = host.querySelector('.global-context-graph-host');
  const current = () => globalContextBaseCurrent(generation, host, route, signal);
  async function load() {
    reload.disabled = true; reload.hidden = true; status.textContent = 'Loading the complete Global context graph…'; graphHost.replaceChildren();
    try {
      const page = await ctxReadFindAll({kind:'root'}, '', signal);
      if (!current()) return;
      const graph = ctxFindGraphData(page.items);
      if (!graph.nodes.length) {
        status.textContent = 'No objects in Global context yet.';
        reload.hidden = false;
        return;
      }
      status.textContent = `${graph.counts.objects} objects · ${graph.counts.relationships} relationships · ${graph.counts.endpointLinks} endpoint links`;
      ctxClearGraph();
      const knownEndpointRefs = new Set(graph.nodes.map(node => node.item?.ref).filter(ref => typeof ref === 'string'));
      _ctxGraph = contextGraph(graphHost, graph, {storageKey:'ctxgraph:global-context:v1',
        onOpen:node => {
          const ref = node.item?.ref || node.item?.id;
          if (ref) {
            globalContextPendingEndpointSnapshot = {ref, refs:knownEndpointRefs};
            location.hash = globalContextObjectRoute(ref);
          }
        }});
    } catch (error) {
      if (!current()) return;
      status.textContent = `The complete Global graph could not be loaded. ${error.message || 'Reload to try again.'}`;
      reload.hidden = false;
    } finally {
      if (generation === globalContextBaseGeneration) reload.disabled = false;
    }
  }
  reload.onclick = load;
  load();
}

function renderGlobalContextPeople(host) {
  const generation = ++globalContextBaseGeneration, route = location.hash, signal = pageReads.signal;
  host.innerHTML = `<section class="global-context-base global-context-people"><header class="global-context-base-head"><h2>People</h2><p>People are canonical objects in Global context.</p></header>
    <div class="global-context-people-tools"><span class="global-context-base-status" role="status">Loading people…</span><input type="search" aria-label="Search Global people" placeholder="Search people…" autocomplete="off"></div>
    <button type="button" class="bt global-context-base-reload" hidden>Reload people</button><div class="global-context-people-list"></div></section>`;
  const status = host.querySelector('.global-context-base-status'), search = host.querySelector('input[type=search]');
  const reload = host.querySelector('.global-context-base-reload'), list = host.querySelector('.global-context-people-list');
  const current = () => globalContextBaseCurrent(generation, host, route, signal);
  let people = [];
  const paint = () => {
    if (!current()) return;
    const query = search.value.trim().toLocaleLowerCase();
    const matches = people.filter(item => `${item.name || ''} ${item.summary || ''} ${item.definition || ''} ${item.attributes?.role || ''} ${item.attributes?.org || ''}`.toLocaleLowerCase().includes(query));
    status.textContent = query ? `${matches.length} of ${people.length} people` : `${people.length} people`;
    list.innerHTML = matches.length ? `<div class="global-context-people-cards">${matches.map(item => `<button type="button" class="global-context-person" data-ref="${esc(item.ref)}">
      <span class="global-context-person-icon" aria-hidden="true">${SVG_PEOPLE}</span><span class="global-context-person-text"><strong>${esc(item.name || item.ref)}</strong>
      ${item.attributes?.role || item.attributes?.org ? `<small>${esc([item.attributes?.role,item.attributes?.org].filter(Boolean).join(' · '))}</small>` : ''}
      ${item.summary || item.definition ? `<span>${esc(cap(item.summary || item.definition, 160))}</span>` : ''}</span></button>`).join('')}</div>`
      : `<div class="empty">${people.length ? 'No people match this search.' : 'No people in Global context yet.'}</div>`;
    list.querySelectorAll('[data-ref]').forEach(button => button.onclick = () => { location.hash = globalContextObjectRoute(button.dataset.ref); });
  };
  async function load() {
    reload.disabled = true; reload.hidden = true; status.textContent = 'Loading every Global person…'; list.replaceChildren();
    try {
      const page = await ctxReadFindAll({kind:'root'}, '', signal);
      if (!current()) return;
      people = page.items.filter(item => item && !item.is_relationship && item.kind === 'person' && typeof item.ref === 'string');
      paint();
    } catch (error) {
      if (!current()) return;
      status.textContent = `People could not be loaded completely. ${error.message || 'Reload to try again.'}`;
      list.replaceChildren(); reload.hidden = false;
    } finally {
      if (generation === globalContextBaseGeneration) reload.disabled = false;
    }
  }
  search.oninput = paint;
  reload.onclick = load;
  load();
}

function renderGlobalContextObject(host) {
  const generation = ++globalContextBaseGeneration, route = location.hash, signal = pageReads.signal;
  const encoded = location.hash.split('/')[4] || '';
  let ref;
  try { ref = decodeURIComponent(encoded); }
  catch (_) { return globalContextBaseRouteError(host, 'The object link has malformed encoding.'); }
  if (!/^<object>[^<>]+<\/object>$/.test(ref)) return globalContextBaseRouteError(host, 'The object link is not a canonical context reference.');
  const endpointSnapshot = globalContextPendingEndpointSnapshot?.ref === ref ? globalContextPendingEndpointSnapshot.refs : null;
  globalContextPendingEndpointSnapshot = null;
  const current = () => globalContextBaseCurrent(generation, host, route, signal);
  host.innerHTML = `<section class="global-context-base global-context-object"><p class="global-context-base-status" role="status">Loading the canonical object…</p><button type="button" class="bt global-context-object-retry" hidden>Reload object</button></section>`;
  const status = host.querySelector('.global-context-base-status'), retry = host.querySelector('.global-context-object-retry');
  let surface = null, expand = null, endpointNames = null, expandError = '', timelineError = '', attrError = '';
  const historyLoading = new Set();
  function mergeEndpointNames(nodes) {
    const found = Object.fromEntries((Array.isArray(nodes) ? nodes : []).filter(node => typeof node?.ref === 'string')
      .map(node => [node.ref, {name:node.name || ''}]));
    endpointNames = {...(endpointNames || {}), ...found};
  }
  function paint() {
    if (!current() || !surface) return;
    host.innerHTML = `<section class="global-context-base global-context-object">${ctxObjectHtml(surface, expand, [], {global:true,timelineError,attrError,endpointNames,knownEndpointRefs:endpointSnapshot})}
      <div class="global-context-expand-status" role="status">${expandError ? esc(`Neighbourhood could not be loaded. ${expandError}`) : ''}</div>
      <button type="button" class="bt global-context-expand-retry" ${expandError ? '' : 'hidden'}>Reload neighbourhood</button></section>`;
    wireGlobalContextObjectLinks(host);
    host.querySelector('.global-context-expand-retry').onclick = loadExpand;
    host.querySelectorAll('[data-history-more]').forEach(button => button.onclick = () => loadHistory(button.dataset.historyMore, false));
    host.querySelectorAll('[data-history-reload]').forEach(button => button.onclick = () => loadHistory(button.dataset.historyReload, true));
  }
  function wireGlobalContextObjectLinks(root) {
    root.querySelectorAll('[data-global-context-route]').forEach(link => link.onclick = () => { location.hash = globalContextBaseRoute(link.dataset.globalContextRoute); });
    root.querySelectorAll('[data-ref]').forEach(link => link.onclick = event => {
      event.stopPropagation();
      if (link.dataset.ref) location.hash = globalContextObjectRoute(link.dataset.ref);
    });
  }
  async function loadExpand() {
    if (!current()) return;
    expandError = '';
    const button = host.querySelector('.global-context-expand-retry'); if (button) button.disabled = true;
    const expandStatus = host.querySelector('.global-context-expand-status'); if (expandStatus) expandStatus.textContent = 'Loading the neighbourhood…';
    try {
      const next = await globalContextBaseRead('context_expand', {ref, depth:1, limit:200}, signal);
      if (!current()) return;
      expand = next;
      mergeEndpointNames(next.nodes);
    }
    catch (error) { expandError = error.message || 'Reload to try again.'; }
    if (current()) paint();
  }
  async function loadHistory(kind, restart) {
    if (!current() || !surface || historyLoading.has(kind)) return;
    const isTimeline = kind === 'timeline', key = isTimeline ? 'timeline' : 'attr_history';
    const cursorKey = isTimeline ? 'timeline_next_cursor' : 'attr_history_next_cursor';
    const cursor = restart ? null : surface[cursorKey];
    const args = {ref, timeline_limit:50, attr_history_limit:20};
    if (cursor) args[isTimeline ? 'timeline_cursor' : 'attr_history_cursor'] = cursor;
    const previousRows = restart ? [] : (surface[key] || []), previousTotal = restart ? null : surface[`${key}_total`];
    if (isTimeline) timelineError = ''; else attrError = '';
    historyLoading.add(kind);
    const button = host.querySelector(`[data-history-more="${kind}"], [data-history-reload="${kind}"]`);
    if (button) { button.disabled = true; button.textContent = 'Loading…'; }
    try {
      const next = await globalContextBaseRead('context_object', args, signal);
      if (!current()) return;
      const nextTotal = next[`${key}_total`];
      if (!ctxHistoryPageValid(next, key, !!cursor)) throw new Error('The history response was incomplete or malformed; reload to try again.');
      if (!restart && previousTotal != null && nextTotal !== previousTotal) throw new Error('The history snapshot changed; reload to try again.');
      if (!restart && cursor && next[cursorKey] === cursor) throw new Error('The history continuation did not advance; reload to try again.');
      const pageRows = Array.isArray(next[key]) ? next[key] : [];
      const mergedRows = [...previousRows, ...pageRows];
      const ids = mergedRows.map(row => row && row.id).filter(Boolean);
      if (!restart && new Set(ids).size !== ids.length) throw new Error('The history page repeated an earlier item; reload to try again.');
      if (nextTotal != null && mergedRows.length > nextTotal) throw new Error('The history page did not match its snapshot; reload to try again.');
      if (next[`${key}_complete`] && mergedRows.length !== nextTotal || !next[`${key}_complete`] && mergedRows.length >= nextTotal)
        throw new Error('The history continuation did not match its snapshot; reload to try again.');
      const otherKey = isTimeline ? 'attr_history' : 'timeline';
      const otherPage = Object.fromEntries([otherKey, `${otherKey}_total`, `${otherKey}_complete`, `${otherKey}_next_cursor`]
        .filter(field => Object.hasOwn(surface, field)).map(field => [field, surface[field]]));
      surface = {...surface, ...next, ...otherPage, [key]: mergedRows};
    } catch (error) {
      if (!current()) return;
      if (isTimeline) timelineError = error.message || 'Reload impacts to try again.';
      else attrError = error.message || 'Reload attribute history to try again.';
    } finally {
      historyLoading.delete(kind);
    }
    if (current()) paint();
  }
  async function load() {
    retry.disabled = true; retry.hidden = true; status.textContent = 'Loading the canonical object…';
    try {
      surface = await globalContextBaseRead('context_object', {ref, timeline_limit:50, attr_history_limit:20}, signal);
      if (!current()) return;
      if (!ctxHistoryPageValid(surface, 'timeline')) timelineError = 'Timeline is incomplete or malformed; reload the object to try again.';
      if (!ctxHistoryPageValid(surface, 'attr_history')) attrError = 'Attribute history is incomplete or malformed; reload the object to try again.';
      try {
        expand = await globalContextBaseRead('context_expand', {ref, depth:1, limit:200}, signal);
        mergeEndpointNames(expand.nodes);
      }
      catch (error) { expandError = error.message || 'Reload to try again.'; }
      const endpoints = surface.object?.endpoints;
      if ((surface.object?.is_relationship || surface.object?.kind === 'relationship') && Array.isArray(endpoints)) {
        const firstEndpoint = endpoints.find(endpoint => typeof endpoint === 'string' && /^<object>[^<>]+<\/object>$/.test(endpoint));
        if (firstEndpoint) {
          try {
            const adjacent = await globalContextBaseRead('context_expand', {ref:firstEndpoint, depth:1, limit:200}, signal);
            mergeEndpointNames(adjacent.nodes);
          } catch (_) { /* Keep names resolved by the object's bounded expansion. */ }
        }
      }
      if (current()) paint();
    } catch (error) {
      if (!current()) return;
      status.textContent = `The canonical object could not be loaded. ${error.message || 'Reload to try again.'}`;
      retry.hidden = false;
    } finally { if (generation === globalContextBaseGeneration) retry.disabled = false; }
  }
  retry.onclick = load;
  load();
}

function globalContextBaseRouteError(host, message) {
  host.innerHTML = `<section class="global-context-base"><div class="empty global-context-route-error" role="alert">${esc(message)}</div><button type="button" class="bt">Back to Graph</button></section>`;
  host.querySelector('button').onclick = () => { location.hash = globalContextBaseRoute('graph'); };
}
