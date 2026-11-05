// Local mock host for the CRM browser tests: serves this repository's dashboard and answers /api with the synthetic CRM
// Operations (crm_api.js), a minimal Focus board, private owner conversations and the shell reads the app boots with.
// No external request is ever made; callers also block every non-local origin in the browser.
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {createCrmApi, SCOPES} = require('./crm_api');

const root = path.resolve(__dirname, '../..');
const agent = '11111111-1111-4111-8111-111111111111';
const businesses = [{id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', business_id: '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa', slug: 'alpha', name: 'Alpha Studio', agents: [], owner: 'owner'}];
const profiles = [{agent_id: 'ordu', name: 'ordu', display_name: 'Ordubashy', status: 'active', role: {id: 'ordubashy'}, presentation: {department: 'operations'},
  scope: {kind: 'ecosystem', allowed_business_ids: ['alpha'], global_capabilities: []}}];

function startCrmServer({api = createCrmApi(), htmlTransform = null} = {}) {
  const board = {revision: 2, status: '', items: [], browser: null, activity: null};
  const state = {api, board, privateSends: [], privateReads: [], privateThreads: new Map(), focusPresents: [], archives: [], boardReads: 0,
    session: {principal_id: 'owner', principal_kind: 'human', display_name: 'Owner One', is_owner: true, is_installation_owner: true},
    flags: {holdCrmList: null, failCrmList: false, denyScope: null, repeatFirst: false}, requests: [], holds: new Map(), denied: new Set(), failing: new Set(), edges: [], lastFirst: new Map(),
    // Optional native transcript supplied by composed previews; ordinary CRM tests keep the default empty response.
    agentChats: new Map()};
  const sourceKey = source => `${source.type}:${source.business_id ?? 'global'}:${source.id}`;
  const privateThread = source => {
    const key = sourceKey(source);
    if (!state.privateThreads.has(key)) state.privateThreads.set(key, {messages: [{id: key + '-m1', sequence: 1, body: 'Private note on ' + source.type, created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent', is_owner: false, created_at: '2026-10-28T10:00:00Z'}], read: 0});
    return state.privateThreads.get(key);
  };
  const cardFor = source => {
    if (source.type === 'table') {
      const table = api.tables.get(source.id);
      if (!table) return null;
      const scope = table.business_id == null ? 'global' : 'alpha';
      return {id: 'card-' + table.id, revision: 1, label: table.title, state: null, reason: '', archived: false, created_by: agent, unavailable: false, references: [],
        source: {type: 'table', id: table.id, business_id: table.business_id}, source_revision: table.revision,
        resolved_source: {type: 'table', id: table.id, business_id: table.business_id, business_slug: SCOPES[scope].slug, business_name: scope === 'global' ? 'Global' : 'Alpha Studio',
          label: table.title, table_kind: table.table_kind, archived: table.archived, resource_type: 'data_tables', revision: table.revision},
        conversation: {type: 'table', id: table.id, business_id: table.business_id}};
    }
    const task = api.task(source.business_id ?? null, source.id);
    if (!task) return null;
    const slug = task.business_id == null ? null : 'alpha';
    return {id: 'card-' + (slug || 'g') + '-' + task.id, revision: 1, label: task.title, state: task.status, reason: '', archived: false, created_by: agent, unavailable: false, references: [],
      source: {type: 'task', id: task.id, business_id: task.business_id ?? null}, source_revision: task.revision,
      resolved_source: {type: 'task', id: task.id, business_id: task.business_id ?? null, business_slug: slug, business_name: slug ? 'Alpha Studio' : 'Global', label: task.title,
        state: task.status, task_type: task.task_type, resource_type: 'tasks', revision: task.revision},
      conversation: {type: 'task', id: task.id, business_id: task.business_id ?? null}};
  };
  // Acting as the installation owner (default) or as an authorized non-owner member; the mock Operations enforce the difference.
  state.actAs = kind => {
    const owner = kind === 'owner';
    state.session = owner ? {principal_id: 'owner', principal_kind: 'human', display_name: 'Owner One', is_owner: true, is_installation_owner: true}
      : {principal_id: 'member', principal_kind: 'human', display_name: 'Member Two', is_owner: false, is_installation_owner: false};
    api.actor = owner ? {id: 'owner', installationOwner: true} : {id: 'member', installationOwner: false, commentAuthority: true};
  };
  state.hold = op => {
    const entry = {}; entry.gate = new Promise(resolve => { entry.release = resolve; });
    entry.startedPromise = new Promise(resolve => { entry.started = resolve; });
    state.holds.set(op, entry); return entry;
  };
  state.pin = source => { const card = cardFor(source); if (card && !board.items.some(item => item.id === card.id)) board.items.push(card); return card; };

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const readBody = async () => { let raw = ''; for await (const chunk of req) raw += chunk; return raw ? JSON.parse(raw) : {}; };
    const send = (status, body) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(body)); };
    const reply = data => send(200, {status: 'ok', data});
    const fail = (status, code, message = code) => send(status, {status: 'error', error: {code, message}});
    if (!url.pathname.startsWith('/api/')) {
      let rel = url.pathname === '/' ? '/index.html' : url.pathname;
      if (rel === '/tables/view') rel = '/tables-view.html';
      const file = path.resolve(root, '.' + rel);
      if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : file.endsWith('.png') ? 'image/png' : file.endsWith('.woff2') ? 'font/woff2' : 'text/html');
      const contents = fs.readFileSync(file);
      res.end(htmlTransform && rel === '/index.html' ? htmlTransform(contents.toString()) : contents); return;
    }
    const body = ['POST', 'PUT'].includes(req.method) ? await readBody() : {};
    state.requests.push({path: url.pathname, body});
    if (url.pathname === '/api/biz') return send(200, businesses);
    if (url.pathname === '/api/session') return reply(state.session);
    if (url.pathname === '/api/root/agents') return reply({agents: profiles});
    const chatRoute = /^\/api\/root\/agents\/([^/]+)\/chat$/.exec(url.pathname);
    if (chatRoute) {
      const transcript = state.agentChats.get(decodeURIComponent(chatRoute[1]));
      if (transcript) return reply(transcript);
    }
    if (/^\/api\/root\/agents\/[^/]+\/chat\/events$/.test(url.pathname)) {
      res.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive'});
      res.write(': synthetic preview stream\n\n');
      return;
    }
    if (url.pathname === '/api/root/agent-pins') return reply({initialized: true, pinned_agents: []});
    if (/\/biz\/[^/]+\/agents/.test(url.pathname)) return reply([]);
    if (url.pathname.endsWith('/workspace_types')) return reply({types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy'], renderer: 'focus_board'}, {key: 'threads', display_name: 'Threads', supported_roles: ['ordubashy'], renderer: 'threads'}]});
    if (url.pathname.endsWith('/calendar_calendars_list')) return reply({configured: false, calendars: [], errors: []});
    if (url.pathname.endsWith('/calendar_today')) return reply({calendar: null, events: []});
    if (url.pathname.endsWith('/focus_notes_list')) return reply({notes: [], revision: 1});
    if (url.pathname.endsWith('/reply-notifications')) return reply({items: []});
    if (url.pathname.endsWith('/browser-surfaces')) return reply({browsers: [], desktop: null});
    if (url.pathname.endsWith('/focus-layout')) return reply({revision: 1, blocks: [], card_ids: board.items.map(item => item.id)});
    if (url.pathname.endsWith('/operations/focus_get')) {
      if (body.item_id) { const item = board.items.find(entry => entry.id === body.item_id); return item ? reply({item, revision: board.revision}) : fail(404, 'not_found'); }
      state.boardReads++;
      return reply({...board, items: board.items.filter(item => body.include_archived || !item.archived), next_offset: null});
    }
    if (url.pathname.endsWith('/operations/focus_present')) {
      if (state.failing.has('focus_present')) return fail(503, 'temporarily_unavailable');
      state.focusPresents.push(body);
      const card = state.pin(body.source);
      return card ? reply({item: card, outcome: 'presented'}) : fail(404, 'not_found');
    }
    if (url.pathname.endsWith('/operations/focus_set_archived')) {
      state.archives.push(body);
      const item = board.items.find(entry => entry.id === body.item_id);
      if (!item) return fail(404, 'not_found');
      if (item.revision !== body.expected_card_revision) return fail(409, 'revision_conflict');
      item.archived = body.archived; item.revision++; board.revision++;
      return reply({item, revision: board.revision});
    }
    if (url.pathname.endsWith('/operations/owner_conversations_list')) return reply({items: [], next_before: null});
    if (url.pathname.endsWith('/operations/owner_conversation_related')) return reply({items: [], next_offset: null});
    if (url.pathname.includes('/operations/owner_conversation_')) {
      const thread = privateThread(body.source), key = sourceKey(body.source);
      if (url.pathname.endsWith('owner_conversation_send')) {
        state.privateSends.push({source: body.source, text: body.text, key: body.idempotency_key});
        const sequence = thread.messages.at(-1).sequence + 1;
        thread.messages.push({id: key + '-m' + sequence, sequence, body: body.text, created_by: 'owner', created_by_name: 'Owner', author_kind: 'human', is_owner: true, created_at: new Date().toISOString()});
        return reply({message: thread.messages.at(-1), conversation: {id: key, revision: sequence, latest_sequence: sequence, message_count: thread.messages.length}});
      }
      if (url.pathname.endsWith('owner_conversation_mark_read')) { state.privateReads.push(body); thread.read = body.sequence; return reply({read_state: {last_read_sequence: thread.read, unread: false}}); }
      return reply({conversation: {id: key, source: body.source, audience: 'owner_ordubashy', revision: 1, latest_sequence: thread.messages.at(-1).sequence, message_count: thread.messages.length},
        messages: thread.messages.slice(-20), next_before: null,
        participants: [{created_by: 'owner', created_by_name: 'Owner', author_kind: 'human', is_owner: true}, {created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent'}],
        read_state: {last_read_sequence: thread.read, unread: false}});
    }
    const match = /^\/api\/(?:root|biz\/([^/]+))\/operations\/([a-z_]+)$/.exec(url.pathname);
    if (match) {
      const scope = match[1] || 'global';
      if (!SCOPES[scope]) return fail(404, 'not_found', 'Project not found');
      if (state.flags.denyScope === scope) return fail(403, 'forbidden', 'No access');
      if (state.denied.has(match[2])) return fail(403, 'forbidden', 'No access');
      if (state.failing.has(match[2])) return fail(503, 'temporarily_unavailable');
      // state.hold(op) delays the next matching Operation until release() so tests can act while a write is pending.
      const held = state.holds.get(match[2]);
      if (held) { state.holds.delete(match[2]); held.started?.(); await held.gate; if (held.failWith) return fail(held.failWith, 'temporarily_unavailable'); }
      if (match[2] === 'crm_entries_list') {
        if (state.flags.failCrmList) return fail(503, 'temporarily_unavailable');
        const hold = state.flags.holdCrmList;
        if (hold && hold.match(body)) await new Promise(resolve => { hold.release = resolve; hold.started?.(); });
      }
      const result = api.handle(scope, match[2], body);
      // Simulates the list shifting between pages: the first entry of the previous page comes back once more.
      if (result && match[2] === 'crm_entries_list' && result.status === 200) {
        const items = result.body.data.items;
        if (state.flags.repeatFirst && body.cursor && state.lastFirst.has(body.table_id)) items.unshift(state.lastFirst.get(body.table_id));
        if (!body.cursor && items.length) state.lastFirst.set(body.table_id, items[0]);
      }
      if (result) return send(result.status, result.body);
    }
    if (url.pathname.endsWith('/operations/relationships_list')) {
      const self = body.item?.id, scopeId = body.scope === 'global' || url.pathname.includes('/root/') ? null : '11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
      const edge = ([from, to]) => { const label = id => api.task(scopeId, id)?.title || id; const item = id => ({type: 'task', id, label: label(id), status: 'open'});
        return {id: from + '>' + to, role: 'concerns', source: {type: 'task', id: from}, target: {type: 'task', id: to}, source_item: item(from), target_item: item(to)}; };
      return reply({items: state.edges.filter(([from, to]) => from === self || to === self).map(edge), next_cursor: null});
    }
    return reply({items: [], messages: [], tasks: [], purposes: [], tags: [], agents: [], totals: {}});
  });
  state.listen = async () => { await new Promise(resolve => server.listen(0, '127.0.0.1', resolve)); state.origin = `http://127.0.0.1:${server.address().port}`; return state.origin; };
  state.close = () => new Promise(resolve => { server.closeAllConnections?.(); server.close(resolve); });
  return state;
}

module.exports = {startCrmServer, businesses};
