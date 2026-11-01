// Global context base readers. Root scope is explicit in every operation path;
// this view never invents a business slug or changes the selected business.
let globalContextBaseGeneration = 0;
const globalContextObjectRoute = ref => `#/global/context/object/${encodeURIComponent(ref)}`;
const globalContextBaseRoute = page => `#/global/context/${page}`;

function globalContextBaseCurrent(generation, host, route, signal) {
  return generation === globalContextBaseGeneration && host.isConnected && !signal.aborted &&
    state.scope === 'global' && state.globalTab === 'context' && location.hash === route;
}

function renderGlobalContextBase(host, page) {
  pubMountGeneration++;
  const route = location.hash.split('/')[3] || 'graph';
  if (route === 'people') return renderGlobalContextPeople(host);
  if (route === 'object') return renderGlobalContextObject(host);
  return renderGlobalContextGraph(host);
}

function globalContextNav(route) {
  return {open: ref => { location.hash = globalContextObjectRoute(ref); }, go: token => { location.hash = globalContextBaseRoute(token); },
    current: () => state.scope === 'global' && state.globalTab === 'context' && location.hash === route,
    crumbs: [{label: 'Global Graph', go: 'graph'}], homeLabel: 'Global Context', backLabel: 'Back to Graph'};
}

// The Global Graph is a bounded window onto the shared published graph, read through the shared published-read helper.
function renderGlobalContextGraph(host) {
  globalContextBaseGeneration++;
  pubMountGraph(host, {kind: 'root'}, globalContextNav(location.hash), {storageKey: 'ctxgraph:published:v1:global', caption: 'Global context'});
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
  globalContextBaseGeneration++;
  const route = location.hash, encoded = location.hash.split('/')[4] || '';
  let ref;
  try { ref = decodeURIComponent(encoded); }
  catch (_) { return globalContextBaseRouteError(host, 'Unavailable'); }
  pubMountObject(host, {kind: 'root'}, ref, globalContextNav(route));
}

function globalContextBaseRouteError(host, message) {
  host.innerHTML = `<section class="global-context-base"><div class="empty global-context-route-error" role="alert">${esc(message)}</div><button type="button" class="bt">Back to Graph</button></section>`;
  host.querySelector('button').onclick = () => { location.hash = globalContextBaseRoute('graph'); };
}
