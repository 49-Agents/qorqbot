// Synthetic Operations for the record-panel browser tests (Story, Customer and Email Tasks, their links, activity,
// email chain and Context). Shapes follow the approved Operations DTOs: Customer states resolve to the first spelling
// of an equal label, an update answers with the whole canonical Task, and Global reads name their scope.
const assert = require('node:assert/strict');
const scopeOf = url => url.pathname.startsWith('/api/biz/alpha/') ? 'alpha' : 'global';
const db = {alpha: {}, global: {}};
const states = {alpha: ['Prospect', 'Active'], global: ['Founder lead']};
const stateKey = value => value.split(/\s+/).filter(Boolean).join(' ').normalize('NFKC').toUpperCase().toLowerCase().normalize('NFKC');
const canonicalState = (scope, label) => states[scope].find(known => stateKey(known) === stateKey(label)) || label.split(/\s+/).filter(Boolean).join(' ');
const UPLOAD_ID = '11111111-2222-4333-8444-555555555555', GLOBAL_UPLOAD_ID = '66666666-7777-4888-8999-000000000000';
// Linkable Context: documents (upload and note readers), a person, and an item that vanishes before it is opened.
// A registered Context document longer than one 6000-character page, with distinct content on every page.
const LONG = '99999999-aaaa-4bbb-8ccc-000000000001', GLONG = '99999999-aaaa-4bbb-8ccc-000000000002';
const longBody = tag => ['A', 'B', 'C'].map(page => `${tag} page ${page} ` + (page + ' ').repeat(3500)).join('');
const contextItems = {
  alpha: {[UPLOAD_ID]: {type: 'material', id: UPLOAD_ID, label: 'Clinic intake upload', resource_type: 'documents', kind: 'context_upload'},
    'note-1': {type: 'material', id: 'note-1', label: 'Founder note', resource_type: 'documents', kind: 'business_note'},
    [LONG]: {type: 'material', id: LONG, label: 'Business long note', resource_type: 'documents', kind: 'context_note'},
    'person-1': {type: 'person', id: 'person-1', label: 'Rosa contact', resource_type: 'principals', role: 'Practice owner', org: 'Alvarez Dental'},
    'gone-1': {type: 'material', id: 'gone-1', label: 'Removed material', resource_type: 'documents', kind: 'business_note'}},
  global: {[GLOBAL_UPLOAD_ID]: {type: 'material', id: GLOBAL_UPLOAD_ID, label: 'Shared intake upload', resource_type: 'documents', kind: 'context_upload'},
    'gnote-1': {type: 'material', id: 'gnote-1', label: 'Global founder note', resource_type: 'documents', kind: 'global_note'},
    [GLONG]: {type: 'material', id: GLONG, label: 'Global long topic', resource_type: 'documents', kind: 'context_topic'},
    'gperson-1': {type: 'person', id: 'gperson-1', label: 'Sam contact', resource_type: 'principals', role: 'Advisor'},
    'ggone-1': {type: 'material', id: 'ggone-1', label: 'Removed Global material', resource_type: 'documents', kind: 'global_note'}}};
const documentBodies = {[UPLOAD_ID]: 'Business upload body text', [GLOBAL_UPLOAD_ID]: 'Global upload body text', 'note-1': 'Business note body text', 'gnote-1': 'Global note body text', [LONG]: longBody('Business'), [GLONG]: longBody('Global')};
const NOTE_IDS = new Set(['note-1', 'gnote-1']);
const version = {bump: false};
const log = [];            // every operation: {scope, op, body}
const compat = [];
const privateSends = [], privateReads = [], privateMarks = [];
const longConversations = new Set(['story-1', 'story-g', 'cust-g']);
const flags = {holdTaskGet: null, releaseTaskGet: null, conflictNext: false, loseUpdateOnce: false, failThreadOnce: false, holdItemGet: null, releaseItemGet: null, holdDocGet: false, releaseDocGet: null,
  failAssociatedOnce: false, holdAssociated: null, releaseAssociated: null};
const seed = (scope, task) => { db[scope][task.id] = {archived: false, status: 'open', tags: [], assignee_ids: [], owners: [], children: [], related_tasks: [], revision: 1, created: '2026-10-01T00:00:00Z', updated: '2026-10-01T00:00:00Z', ...task}; };
seed('alpha', {id: 'story-1', title: 'Spring campaign story', task_type: 'story', overview: 'Original overview', business_id: 'biz-1'});
seed('alpha', {id: 'child-1', title: 'Draft the launch note', parent_id: 'story-1', business_id: 'biz-1'});
seed('alpha', {id: 'child-story', title: 'Plan the launch party', task_type: 'story', parent_id: 'story-1', business_id: 'biz-1'});
seed('alpha', {id: 'child-done', title: 'Archive the old brief', status: 'done', parent_id: 'story-1', business_id: 'biz-1'});
seed('alpha', {id: 'child-quiet', title: 'Quiet archived child', archived: true, parent_id: 'story-1', business_id: 'biz-1'});
seed('alpha', {id: 'grand-1', title: 'Collect venue quotes', parent_id: 'child-1', business_id: 'biz-1'});
seed('alpha', {id: 'grand-2', title: 'Shortlist the caterers', parent_id: 'child-story', business_id: 'biz-1'});
seed('alpha', {id: 'story-empty', title: 'Empty story', task_type: 'story', business_id: 'biz-1'});
seed('alpha', {id: 'story-dates', title: 'Dated activity story', task_type: 'story', overview: 'Boundary dates', business_id: 'biz-1'});
seed('alpha', {id: 'story-old', title: 'Older activity story', task_type: 'story', overview: 'A past year', business_id: 'biz-1'});
seed('alpha', {id: 'task-linked', title: 'Linked ordinary task', business_id: 'biz-1'});
seed('alpha', {id: 'cust-1', title: 'Rosa Alvarez', task_type: 'customer', customer_kind: 'person', customer_state: 'Prospect', customer_profile: {company: 'Alvarez Dental', contact_name: '', email: 'rosa@example.test', phone: '', website: '', notes: 'Met at the fair'}, business_id: 'biz-1', status: 'open'});
seed('alpha', {id: 'cust-2', title: 'Marta Ruiz', task_type: 'customer', customer_kind: 'person', customer_state: 'Active', customer_profile: {company: '', contact_name: '', email: '', phone: '', website: '', notes: ''}, business_id: 'biz-1'});
seed('alpha', {id: 'mail-1', title: 'Pricing question thread', task_type: 'email', email_conversation_id: 'conv-1', business_id: 'biz-1'});
seed('alpha', {id: 'plain-1', title: 'An ordinary task', business_id: 'biz-1'});
seed('global', {id: 'cust-g', title: 'Global Customer Co', task_type: 'customer', customer_kind: 'company', customer_state: 'Founder lead', customer_profile: {company: '', contact_name: 'Sam', email: '', phone: '', website: '', notes: ''}, business_id: null});
seed('global', {id: 'story-g', title: 'Global story', task_type: 'story', overview: 'Global overview', business_id: null});
seed('global', {id: 'gchild-1', title: 'Global child one', parent_id: 'story-g', business_id: null});
seed('global', {id: 'mail-g', title: 'Global mail task', task_type: 'email', email_conversation_id: 'conv-g', business_id: null});
// Canonical associated records for the recursive read. Sources are the pre-resolved Task IDs in this owning
// scope; the fixture applies the contract's task set, deduplication, visibility and pagination to them.
const associatedRecords = {
  alpha: [
    {type: 'action', id: 'business/biz-1/act-venue', business_id: 'biz-1', label: 'Confirm the spring venue', state: 'done', sources: ['story-1']},
    {type: 'action', id: 'business/biz-1/act-photographer', business_id: 'biz-1', label: 'Book the launch photographer', state: 'withdrawn', sources: ['child-1']},
    {type: 'action', id: 'business/biz-1/act-checklist', business_id: 'biz-1', label: 'Draft the venue checklist', state: 'open', sources: ['grand-1']},
    {type: 'action', id: 'business/biz-1/act-catering', business_id: 'biz-1', label: 'Compare caterers', state: 'open', sources: ['child-story', 'grand-2']},
    {type: 'signoff', id: 'sg-budget', business_id: 'biz-1', label: 'Approve the spring budget', state: 'approved', sources: ['story-1']},
    {type: 'signoff', id: 'sg-brief', business_id: 'biz-1', label: 'Approve the photographer brief', state: 'pending', sources: ['child-1']},
    {type: 'signoff', id: 'sg-date', business_id: 'biz-1', label: 'Approve the launch date', state: 'rejected', sources: ['grand-1']},
    // A linked Task is not a descendant: its associated history must never join this Story.
    {type: 'signoff', id: 'sg-linked', business_id: 'biz-1', label: 'Approve the linked task', state: 'pending', sources: ['task-linked']},
  ],
  global: [
    {type: 'action', id: 'global/act-g', business_id: null, label: 'Global action', state: 'open', sources: ['story-g']},
    {type: 'signoff', id: 'sg-g', business_id: null, label: 'Global signoff', state: 'pending', sources: ['gchild-1']},
  ],
};
const associated = {records: associatedRecords, calls: []};
const relationships = {alpha: [], global: []};
const threadNotes = Array.from({length: 45}, (_, i) => ({id: 'act-' + (i + 1), sequence: i + 1, body: 'Activity entry ' + (i + 1), created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: '2026-10-02T10:00:00Z'}));
// Activity boundary dates, relative to the app's current San Juan year, so the expected labels never depend on
// the wall clock: a same-day repeat, a calendar change, a year change, a current-year first row and an older
// first row. Instants are UTC; America/Puerto_Rico is UTC-4 year-round.
const activityYear = Number(new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Puerto_Rico', year: 'numeric'}).format(new Date()));
const sjInstant = (year, month, day, hour, minute = 0) => new Date(Date.UTC(year, month - 1, day, hour + 4, minute)).toISOString();
const datedNotes = [
  {id: 'dn-6', sequence: 6, entry_kind: 'system', title: 'Created', body: 'Story created', created_at: sjInstant(activityYear, 10, 29, 8)},
  {id: 'dn-5', sequence: 5, body: 'Newest agent entry', created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: sjInstant(activityYear, 10, 28, 10, 5)},
  {id: 'dn-4', sequence: 4, body: 'Same day, earlier', created_by: 'agent-2', created_by_name: 'Writer', author_kind: 'agent', created_at: sjInstant(activityYear, 10, 28, 5)},
  {id: 'dn-3', sequence: 3, body: 'Previous calendar day', created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: sjInstant(activityYear, 10, 27, 19, 59)},
  {id: 'dn-2', sequence: 2, body: 'Last year', created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: sjInstant(activityYear - 1, 12, 31, 8)},
  {id: 'dn-1', sequence: 1, body: 'Two years ago', created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: sjInstant(activityYear - 2, 1, 2, 8)},
];
const oldNotes = [
  {id: 'dd-2', sequence: 2, body: 'Newest entry of an older year', created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: sjInstant(activityYear - 4, 6, 1, 8)},
  {id: 'dd-1', sequence: 1, body: 'Same older year, earlier', created_by: 'agent-1', created_by_name: 'Planner', author_kind: 'agent', created_at: sjInstant(activityYear - 4, 5, 1, 8)},
];
const threadNotesByTask = {'story-dates': datedNotes, 'story-old': oldNotes};
const emailMessages = Array.from({length: 60}, (_, i) => ({id: 'msg-' + (i + 1), from: {address: 'client@example.test'}, subject: 'Pricing question', body: 'Mail body ' + (i + 1), received_at: '2026-10-01T10:' + String(i % 60).padStart(2, '0') + ':00Z', has_attachments: i === 59}));
const emailOps = {reads: [], comments: 0};


// Answers a request when it is one of the record Operations (or the business Task compat route); returns whether it did.
async function dispatch(req, res, url) {
  const json = (data, status = 200) => { res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); };
  const ok = data => json({status: 'ok', data});
  const fail = (code, message, status = 400) => json({status: 'error', error: {code, message}}, status);
  const view = (scope, task) => ({...task, children: Object.values(db[scope]).filter(other => other.parent_id === task.id).map(({id, title, status, task_type}) => ({id, title, status, task_type}))});
  if (url.pathname.includes('/operations/') && req.method === 'POST') {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || '{}'), op = url.pathname.split('/').pop(), scope = scopeOf(url);
    log.push({scope, op, body});
    if (op.startsWith('owner_conversation_')) {
      assert.ok(body.source && body.source.id, 'a canonical record source is always named');
      assert.equal(body.agent_id, undefined, 'record conversations never name an agent');
      assert.equal(body.peer_role, undefined, 'record conversations never name a peer role');
      if (op === 'owner_conversation_peers') return fail('unknown_operation', 'Peer discovery is retired', 404);
      if (op === 'owner_conversation_send') { privateSends.push({scope, ...body}); return ok({message: {id: 'sent-' + privateSends.length}, conversation: {latest_sequence: 2}}); }
      if (op === 'owner_conversation_mark_read') { privateMarks.push(body); return ok({read_state: {last_read_sequence: body.sequence, unread: false}}); }
      privateReads.push({scope, ...body});
      // Some records have a long history so the conversation has a scroll position to keep.
      const count = longConversations.has(body.source.id) ? 40 : 1;
      return ok({conversation: {id: 'c-' + body.source.id, latest_sequence: count, message_count: count, revision: 1},
        messages: Array.from({length: count}, (_, i) => ({id: 'm' + i + '-' + body.source.id, sequence: i + 1, body: i === count - 1 ? 'Ordubashy line for ' + body.source.id : 'Earlier line ' + i + ' for ' + body.source.id, created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent', is_owner: false, created_at: '2026-10-02T09:00:00Z'})),
        next_before: null, participants: [], read_state: {last_read_sequence: 0, unread: true}});
    }
    if (op === 'tasks_get') {
      if (flags.holdTaskGet === body.task_id) { flags.holdTaskGet = null; await new Promise(resolve => { flags.releaseTaskGet = resolve; }); }
      const task = db[scope][body.task_id]; if (!task) return fail('not_found', 'Task is no longer available', 404);
      return ok({task: view(scope, task)});
    }
    if (op === 'tasks_list') {
      const rows = Object.values(db[scope]).filter(task => body.parent_id ? task.parent_id === body.parent_id : true);
      // A deliberately small page proves the section's own More control without a long fixture.
      const size = Math.min(Number(body.limit) || 50, 2), offset = body.cursor ? Number(body.cursor) : 0;
      return ok({tasks: rows.slice(offset, offset + size).map(task => view(scope, task)), next_cursor: offset + size < rows.length ? String(offset + size) : null});
    }
    if (op === 'tasks_associated_records') {
      // The shared read contract: Task operation (route supplies the scope), recursive descendant task set,
      // canonical deduplication, combined task_ids, stable order and a cursor bound to this exact query.
      associated.calls.push({namespace: scope, ...body});
      if (flags.holdAssociated === body.task_id) { flags.holdAssociated = null; await new Promise(resolve => { flags.releaseAssociated = resolve; }); }
      if (flags.failAssociatedOnce) { flags.failAssociatedOnce = false; return fail('internal', 'Associated records failed', 500); }
      const extra = Object.keys(body).filter(key => !['task_id', 'record_type', 'include_descendants', 'limit', 'cursor'].includes(key) && key !== 'idempotency_key');
      if (extra.length) return fail('validation_error', `Additional properties are not allowed (${extra.join(', ')} unexpected)`, 400);
      if (!db[scope][body.task_id]) return fail('not_found', 'Task is no longer available', 404);
      if (!['action', 'signoff'].includes(body.record_type)) return fail('validation_error', 'record_type must be action or signoff', 400);
      const includeDescendants = body.include_descendants !== false;
      const tasks = new Set();
      const walk = id => {
        if (tasks.has(id) || !db[scope][id]) return;
        tasks.add(id);
        if (!includeDescendants) return;
        for (const candidate of Object.values(db[scope])) if (candidate.parent_id === id) walk(candidate.id);
      };
      walk(body.task_id);
      const grouped = new Map();
      for (const record of associatedRecords[scope]) {
        if (record.type !== body.record_type) continue;
        const taskIds = record.sources.filter(id => tasks.has(id));
        if (!taskIds.length) continue;
        const key = [record.type, record.business_id ?? 'global', record.id].join(':');
        if (grouped.has(key)) grouped.get(key).task_ids.push(...taskIds);
        else grouped.set(key, {type: record.type, id: record.id, business_id: record.business_id, label: record.label, state: record.state, task_ids: [...new Set(taskIds)]});
      }
      const items = [...grouped.values()].sort((a, b) => [a.business_id ?? '', a.id].join(':').localeCompare([b.business_id ?? '', b.id].join(':')));
      const binding = {root: body.task_id, type: body.record_type, descendants: includeDescendants, scope};
      let offset = 0;
      if (body.cursor) {
        const bound = JSON.parse(Buffer.from(body.cursor, 'base64').toString());
        if (['root', 'type', 'descendants', 'scope'].some(key => bound[key] !== binding[key])) return fail('validation_error', 'Cursor belongs to a different Story, record type or scope', 400);
        offset = bound.offset;
      }
      const size = Math.min(Number(body.limit) || 50, 2), page = items.slice(offset, offset + size);
      return ok({items: page, next_cursor: offset + size < items.length ? Buffer.from(JSON.stringify({...binding, offset: offset + size})).toString('base64') : null});
    }
    if (op === 'tasks_update') {
      const task = db[scope][body.task_id];
      if (flags.loseUpdateOnce && body.changes.customer_state) { flags.loseUpdateOnce = false; res.statusCode = 503; return res.end('lost'); }
      if (flags.conflictNext || body.expected_revision !== task.revision) { flags.conflictNext = false; return fail('revision_conflict', 'The task changed', 409); }
      const changes = {...body.changes};
      // An equal existing label keeps its first spelling; a new label joins the vocabulary as written (whitespace collapsed).
      if (changes.customer_state) { changes.customer_state = canonicalState(scope, changes.customer_state); if (!states[scope].includes(changes.customer_state)) states[scope].push(changes.customer_state); }
      // Task-to-Task links are the existing related_task change; linking twice keeps one chip.
      const link = changes.related_task;
      if (link) {
        delete changes.related_task;
        task.related_tasks = (task.related_tasks || []).filter(item => String(item.id) !== String(link.task_id));
        if (link.linked !== false && String(link.task_id) !== String(task.id)) {
          const other = (link.business_id ? db.alpha[link.task_id] : db.global[link.task_id]) || {};
          task.related_tasks.push({id: link.task_id, task_id: link.task_id, title: other.title || link.task_id, task_type: other.task_type || 'task',
            status: other.status || 'open', business_id: link.business_id ?? null,
            link_source: {task_id: task.id, business_id: task.business_id ?? null, revision: task.revision + 1}});
        }
      }
      for (const [key, value] of Object.entries(changes)) task[key] = value;
      task.revision++;
      return ok({task: view(scope, task)});
    }
    if (op === 'tasks_create') {
      const id = 'new-' + Object.keys(db[scope]).length;
      seed(scope, {id, title: body.title, task_type: body.task_type || 'task', customer_kind: body.customer_kind ?? null, customer_state: body.customer_state || '', customer_profile: body.customer_profile || {}, overview: body.overview || ''});
      if (body.customer_state) states[scope].push(body.customer_state);
      return ok({task: view(scope, db[scope][id])});
    }
    if (op === 'tasks_config_get') return ok({config: {states: []}, revision: 1, customer_states: [...states[scope]]});
    if (op === 'tasks_thread_get') {
      if (flags.failThreadOnce) { flags.failThreadOnce = false; return fail('internal', 'Boom', 500); }
      const rows = threadNotesByTask[body.task_id] || threadNotes;
      const before = body.cursor ? Number(body.cursor) : rows.length + 1;
      const page = rows.filter(note => note.sequence < before).slice(-20);
      return ok({thread_id: 't', participants: [], notes: page, next_cursor: page[0].sequence > 1 ? String(page[0].sequence) : null});
    }
    if (op === 'relationships_list') {
      const edges = relationships[scope].filter(edge => edge.target.id === body.item.id || edge.source.id === body.item.id);
      return ok({items: edges, next_cursor: null});
    }
    if (op === 'relationships_create') {
      const known = contextItems[scope][body.source.id], email = body.source.id.startsWith('conv');
      const edge = {id: 'rel-' + (relationships[scope].length + 1), revision: 1, role: body.role, source: body.source, target: body.target,
        source_item: email ? {...body.source, label: 'Pricing question', resource_type: 'email_conversations'} : {...body.source, label: known?.label || 'Unknown', resource_type: known?.resource_type || 'documents'},
        target_item: {...body.target, label: 'Record', resource_type: 'tasks'}};
      relationships[scope].push(edge);
      return ok({relationship: edge});
    }
    if (op === 'relationships_remove') { relationships[scope] = relationships[scope].filter(edge => edge.id !== body.relationship_id); return ok({removed: true}); }
    if (op === 'context_items_list') {
      if (scope === 'global') assert.equal(body.scope, 'global', 'Global Context lookups name Global explicitly');
      return ok({items: Object.values(contextItems[scope]).filter(item => item.type === body.type), next_cursor: null});
    }
    if (op === 'context_items_get') {
      if (scope === 'global') assert.equal(body.scope, 'global', 'Global Context reads name Global explicitly');
      if (flags.holdItemGet === body.item.id) { flags.holdItemGet = null; await new Promise(resolve => { flags.releaseItemGet = resolve; }); }
      const item = contextItems[scope][body.item.id];
      if (!item || item.id.startsWith('gone') || item.id.startsWith('ggone')) return fail('not_found', 'Item not found', 404);
      return ok({item});
    }
    // The real strict argument schemas: unknown properties are validation errors, never silently accepted.
    const strict = allowed => { const extra = Object.keys(body).filter(key => !allowed.includes(key) && key !== 'idempotency_key'); return extra.length ? fail('validation_error', `Additional properties are not allowed (${extra.map(key => `'${key}'`).join(', ')} unexpected)`, 400) : null; };
    if (op === 'context_get') {
      const bad = strict(['document_id', 'scope', 'cursor', 'heading', 'start_line', 'end_line', 'max_chars']); if (bad) return bad;
      assert.equal(body.scope, scope === 'global' ? 'shared' : 'business', 'a raw document read names its one owning namespace');
      const text = documentBodies[body.document_id];
      // A Global note is not in the registered Context library, and the registered library never serves notes.
      if (!text || NOTE_IDS.has(body.document_id) || !body.document_id.includes('-') || body.document_id.length < 30) return fail('not_found', 'Document is not in the selected context library', 404);
      if (flags.holdDocGet && body.cursor) { flags.holdDocGet = false; await new Promise(resolve => { flags.releaseDocGet = resolve; }); }
      const hash = 'sha256:' + (version.bump ? 'v2' : 'v1'), pageSize = body.max_chars || 6000;
      let offset = 0;
      if (body.cursor) {
        const bound = JSON.parse(Buffer.from(body.cursor, 'base64').toString());
        if (bound.id !== body.document_id || bound.scope !== scope || bound.hash !== hash) return fail('validation_error', 'Cursor belongs to a different query, scope or document version', 400);
        offset = bound.offset;
      }
      const part = text.slice(offset, offset + pageSize), following = offset + part.length;
      const next = following < text.length ? Buffer.from(JSON.stringify({id: body.document_id, scope, hash, offset: following})).toString('base64') : null;
      return ok({document: {id: body.document_id, title: 'Source ' + body.document_id.slice(0, 4), body: part, character_offset: offset, content_hash: hash}, next_cursor: next, complete: following === text.length});
    }
    if (op === 'notes_get') {
      // Exactly {document_id}: the route (root or business) supplies the scope, as on the real host.
      const bad = strict(['document_id']); if (bad) return bad;
      const text = documentBodies[body.document_id];
      if (!text || !NOTE_IDS.has(body.document_id)) return fail('not_found', 'Note not found', 404);
      if ((scope === 'global') !== body.document_id.startsWith('g')) return fail('not_found', 'Note is outside the selected scope', 404);
      return ok({document: {id: body.document_id, body: text}});
    }
    if (op === 'email_conversations_list') return ok({items: [{id: 'conv-1', subject: 'Pricing question', last_message_at: '2026-10-01T12:00:00Z'}, {id: 'conv-2', subject: 'Invoice follow up', last_message_at: '2026-10-02T12:00:00Z'}], next_cursor: null});
    if (op === 'email_conversation_get') {
      emailOps.reads.push({scope, ...body});
      const end = body.cursor ? Number(body.cursor) : emailMessages.length;
      const page = emailMessages.slice(Math.max(0, end - 50), end);
      return ok({conversation: {id: body.conversation_id, subject: 'Pricing question', thread_revision: 1, controls: {tags: [], owner_attention_priority: 'none', revision: 0}}, messages: page, next_cursor: end - 50 > 0 ? String(end - 50) : null});
    }
    if (op === 'email_comments_list' || op === 'email_comment') { emailOps.comments++; return ok({notes: [], next_before: null}); }
    if (op === 'assignees_list') return ok({assignees: [{id: 'owner-1', kind: 'human', display_name: 'Owner', name: null, is_owner: true}, {id: 'agent-1', kind: 'agent', display_name: 'Planner', name: 'planner', is_owner: false}]});
    if (op === 'context_reviews_list') return ok({items: [], next_cursor: null, item_revision: 1});
    if (op === 'tags_list' || op === 'tags_catalog') return ok({tags: [], items: []});
    return ok({});
  }
  const taskRoute = url.pathname.match(/^\/api\/biz\/alpha\/task\/([^/]+)$/);
  if (taskRoute && req.method === 'POST') {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || '{}'), task = db.alpha[taskRoute[1]];
    compat.push(body);
    Object.assign(task, body.set || {}); task.revision++;
    return json({task: view('alpha', task)});
  }
  if (taskRoute) { const task = db.alpha[taskRoute[1]]; return task ? json({task: view('alpha', task)}) : json({error: 'missing'}, 404); }
}
async function handle(req, res, url) { await dispatch(req, res, url); return res.writableEnded || res.headersSent; }
module.exports = {flags, db, states, stateKey, log, compat, privateSends, privateReads, privateMarks, longConversations, relationships, emailOps, emailMessages, contextItems, documentBodies, LONG, GLONG, version, UPLOAD_ID, GLOBAL_UPLOAD_ID, associated, associatedRecords, activityYear, handle};
