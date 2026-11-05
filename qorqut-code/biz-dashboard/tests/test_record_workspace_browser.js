// Shared Focus record workspace against synthetic DTOs only. No live records,
// services, browser sessions or agent models are touched by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const shots = process.env.RECORD_WORKSPACE_SHOTS || null;
const proposal = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/signoffs.json'), 'utf8')).data.items[0];
const agent = '11111111-1111-4111-8111-111111111111';

const profiles = [['ordu', 'Ordubashy', 'ordubashy']].map(([agent_id, display_name, role]) =>
  ({agent_id, name: agent_id, display_name, status: 'active', role: {id: role}, presentation: {department: 'operations'},
    scope: {kind: 'ecosystem', allowed_business_ids: ['alpha', 'beta'], global_capabilities: []}}));
const businesses = [
  {id: 'project-a', business_id: 'project-a', slug: 'alpha', name: 'Alpha Studio', agents: ['dev'], owner: 'owner'},
  {id: 'project-b', business_id: 'project-b', slug: 'beta', name: 'Beta Lab', agents: ['dev'], owner: 'owner'},
  {id: 'project-c', business_id: 'project-c', slug: 'gamma', name: 'Gamma Works', agents: ['dev'], owner: 'owner'},
  {id: 'project-d', business_id: 'project-d', slug: 'old', name: 'Old Project', agents: ['dev'], owner: 'owner', archived: true},
];
const story = {
  id: 'story-1', title: 'Spring campaign', task_type: 'story', status: 'open', revision: 3,
  overview: 'First paragraph of the campaign direction.\n\nSecond paragraph with the decision context.',
  business_id: 'project-a', business_slug: 'alpha', tags: ['campaign'], assignee_ids: [], created_by: 'owner',
  children: [{id: 'child-1', title: 'Draft the landing page', status: 'working', revision: 1, task_type: 'task'}],
  links: [], note_count: 0,
};
const storyCall = {reads: 0, saves: [], saveFails: 0, keyHistory: []};
let projectName = 'Alpha Studio';
const project = {id: 'project-a', business_id: 'project-a', name: projectName, slug: 'alpha', summary: 'A synthetic Project used by the workspace test.', revision: 4,
  archived: false, source: {type: 'project', id: 'project-a', business_id: 'project-a'}};
const projectB = {id: 'project-b', business_id: 'project-b', name: 'Beta Lab', slug: 'beta', summary: '', revision: 2, archived: false,
  source: {type: 'project', id: 'project-b', business_id: 'project-b'}};
const workItem = (over = {}) => ({
  type: 'task', id: 'dup', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio',
  label: 'Top-level Alpha work', revision: 1, state: 'open', task_type: 'task', parent_id: null, summary: '',
  resource_type: 'tasks', can_thread: true, association: {owned: true, linked: false}, ...over,
});
const inventory = {
  work: [
    workItem(),
    workItem({id: 'foreign-task', business_id: null, business_slug: null, business_name: 'Global', label: 'Global follow-up',
      association: {owned: false, linked: true, link_id: 'link-global', link_revision: 2}}),
    workItem({id: 'foreign-beta', business_id: 'project-b', business_slug: 'beta', business_name: 'Beta Lab', label: 'Beta foreign work',
      association: {owned: false, linked: true, link_id: 'link-beta', link_revision: 1}}),
    workItem({id: 'owned-linked', label: 'Owned and linked', association: {owned: true, linked: true, link_id: 'link-owned', link_revision: 1}}),
  ],
  signoffs: [{type: 'signoff', id: 'signoff-1', business_id: null, business_slug: null, business_name: 'Global', label: 'Send the launch announcement',
    revision: 2, state: 'pending', resource_type: 'signoffs', can_thread: true, association: {owned: false, linked: true, link_id: 'link-signoff', link_revision: 3}}],
  actions: [{type: 'action', id: 'action-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Published the first page',
    revision: 1, state: 'done', resource_type: 'action_logs', can_thread: false, association: {owned: true, linked: false}}],
  documents: [{type: 'material', id: 'doc-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Campaign brief',
    revision: 1, summary: 'The brief body in the authorized read model.', resource_type: 'documents', can_thread: false,
    association: {owned: true, linked: false}},
    {type: 'person', id: 'person-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Campaign owner',
      revision: 1, summary: 'The owner of the campaign.', resource_type: 'context_entities', can_thread: false,
      association: {owned: true, linked: false}}],
  tables: [{type: 'table', id: 'table-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Campaign calendar',
    revision: 1, resource_type: 'data_tables', can_thread: false, association: {owned: true, linked: false}}],
  automation: [
    {type: 'routine', id: 'routine-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Weekly digest',
      revision: 1, state: 'active', resource_type: 'routines', can_thread: false, association: {owned: true, linked: false}},
    {type: 'routine', id: 'routine-g', business_id: null, business_slug: null, business_name: 'Global', label: 'Global digest',
      revision: 3, state: 'paused', resource_type: 'routines', can_thread: false, association: {owned: false, linked: true, link_id: 'link-routine', link_revision: 1}},
  ],
  other: [{type: 'reminder', id: 'reminder-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Follow up Friday',
    summary: 'Follow up Friday', at: '2026-10-30T15:00:00Z', state: 'queued', revision: 4, resource_type: 'owner_reminders', can_thread: false,
    association: {owned: true, linked: false}},
    {type: 'dev_ticket', id: 'ticket-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Checkout retry loop',
      summary: 'kind: bug; status: closed', state: 'closed', revision: 3, resource_type: 'dev_tickets', can_thread: false,
      association: {owned: true, linked: false}}],
};
const projectTicket = {id: 'ticket-1', title: 'Checkout retry loop', kind: 'bug', status: 'closed',
  details: 'First paragraph of the owner report.\n\nSecond paragraph keeps the reproduction steps and context.',
  resolution: 'Replaced the retry guard and verified the checkout flow.', reporter_id: 'owner-1', business_id: 'project-a',
  created_at: '2026-10-26T09:00:00Z', updated_at: '2026-10-27T15:30:00Z', revision: 3};
const globalTicket = {id: 'ticket-global', title: 'Nightly export delay', kind: 'tooling', status: 'open',
  details: 'The first paragraph explains the delayed export.\n\nThe second paragraph records the observed schedule.',
  reporter_id: 'owner-1', business_id: null, created_at: '2026-10-27T06:00:00Z', updated_at: '2026-10-27T06:05:00Z', revision: 1};
const betaTicket = {id: 'ticket-beta', title: 'Beta report', kind: 'workflow', status: 'open',
  details: 'A foreign-scope report body.', reporter_id: 'owner-1', business_id: 'project-b',
  created_at: '2026-10-27T07:00:00Z', updated_at: '2026-10-27T07:00:00Z', revision: 1};
const unknownScopeTicket = {id: 'ticket-unknown-scope', title: 'Unknown scope report', kind: 'bug', status: 'open',
  details: 'An owner-authorized report with an id-only business scope.', reporter_id: 'owner-1', business_id: 'scope-without-slug',
  created_at: '2026-10-27T08:00:00Z', updated_at: '2026-10-27T08:00:00Z', revision: 1};
const conversionBusiness = {id: 'convert-me', title: 'Convert business work', task_type: 'task', status: 'open', revision: 1,
  overview: 'Business overview before conversion', business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {},
  children: []};
const conversionGlobal = {id: 'global-convert', title: 'Convert global work', task_type: 'task', status: 'open', revision: 1,
  overview: 'Global overview before conversion', business_id: null, business_slug: null, tags: [], assignee_ids: [], fields: {},
  children: []};
let businessConversionTask = {...conversionBusiness}, globalConversionTask = {...conversionGlobal};
const externalConversion = {id: 'external-convert', title: 'Externally converted work', task_type: 'task', status: 'open', revision: 1,
  overview: 'Local overview that must survive', business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []};
let externalConversionTask = {...externalConversion};
const externalClean = {id: 'external-clean', title: 'Clean external conversion', task_type: 'task', status: 'open', revision: 1,
  overview: 'Clean local overview', business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []};
let externalCleanTask = {...externalClean};
const externalGlobal = {id: 'external-global', title: 'Global externally converted work', task_type: 'task', status: 'open', revision: 1,
  overview: 'Global local overview', business_id: null, business_slug: null, tags: [], assignee_ids: [], fields: {}, children: []};
let externalGlobalTask = {...externalGlobal};
const freshBusiness = {id: 'fresh-business', title: 'Fresh business source', task_type: 'story', status: 'open', revision: 2,
  overview: 'Canonical fresh business overview', business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []};
const freshGlobal = {id: 'fresh-global', title: 'Fresh global source', task_type: 'story', status: 'open', revision: 2,
  overview: 'Canonical fresh global overview', business_id: null, business_slug: null, tags: [], assignee_ids: [], fields: {}, children: []};
const failedConversion = {id: 'failed-convert', title: 'Failed conversion work', task_type: 'task', status: 'open', revision: 1,
  overview: 'Failed conversion overview', business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}, children: []};
let failedConversionTask = {...failedConversion}, failConversionSave = false;
const devTicketCalls = [];
let denyDevTicketRead = false;
const inventoryCalls = [], itemGetCalls = [], routineRunCalls = [], jobGetCalls = [];
const linkCalls = [], unlinkCalls = [];
let failInventorySection = null, holdInventoryRelease = null, holdInventoryStarted = null;
let candidateCalls = [], linkFails = 0;
const candidates = [
  {type: 'task', id: 'candidate-a', business_id: null, business_slug: null, business_name: 'Global', label: 'Global candidate', can_thread: true, resource_type: 'tasks'},
  {type: 'material', id: 'candidate-b', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Brief candidate', can_thread: false, resource_type: 'documents'},
];
let denyProjectItems = false, lateProjectItems = null, denyProjectItemGet = false;
let denyActionRead = false, denyStoryRead = false, retryTaskFailures = 0, retryTaskAttempts = 0, projectRecordFailures = 0;
const board = {
  revision: 2, status: '', items: [],
  browser: null, activity: null,
};
const card = (item) => ({
  id: item.id, revision: 1, label: item.label, display_name: item.display_name, state: item.state, reason: '',
  archived: false, created_by: agent, unavailable: false, references: [],
  source: {type: item.type, id: item.id, business_id: item.business_id ?? null},
  resolved_source: {type: item.type, id: item.id, business_id: item.business_id ?? null, business_slug: item.business_slug ?? null,
    business_name: item.business_name ?? null, label: item.label, state: item.state, task_type: item.task_type,
    customer_state: item.state, resource_type: item.resource_type, revision: 1},
  conversation: item.conversation === null ? null : {type: item.type, id: item.id, business_id: item.business_id ?? null},
  source_revision: 1,
});
board.items = [
  card({id: 'story-1', type: 'task', label: 'Spring campaign', display_name: 'Spring campaign', state: 'open', task_type: 'story', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio'}),
  card({id: 'project-a', type: 'project', label: 'Alpha Studio', display_name: 'Wrong override name', state: null, business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio'}),
  card({id: 'project-b', type: 'project', label: 'Beta Lab', state: null, business_id: 'project-b', business_slug: 'beta', business_name: 'Beta Lab'}),
  card({id: 'task-1', type: 'task', label: 'Ordinary task', state: 'working', task_type: 'task', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio'}),
  card({id: 'signoff-1', type: 'signoff', label: 'Send the launch announcement', state: 'pending', business_id: null, business_slug: null, business_name: 'Global'}),
  card({id: 'signoff-input', type: 'signoff', label: 'Choose the launch date', state: 'pending', business_id: null, business_slug: null, business_name: 'Global'}),
];
// A real Focus reference: its conversation binding is the card identity, and its dashboard is the URL reader.
board.items.push({...card({id: 'ref-1', type: 'reference', label: 'Competitor pricing page', state: null, conversation: null}),
  source: null, resolved_source: null, conversation: {type: 'reference', id: 'ref-1', business_id: null},
  reference: {title: 'Competitor pricing page', target: {type: 'url', url: 'https://example.test/pricing'}},
  opening_target: {type: 'url', url: 'https://example.test/pricing'}});
board.items.find(item => item.id === 'story-1').display_name = 'A very long Focus-only Story name the card shows';
const focusPresentCalls = [];
let holdInventory = null;
const projectWorkByProject = {
  'project-a': {items: [
    workItem(),
    workItem({id: 'foreign-task', business_id: null, business_slug: null, business_name: 'Global', label: 'Global follow-up',
      association: {owned: false, linked: true, link_id: 'link-global', link_revision: 2}}),
    workItem({id: 'foreign-beta', business_id: 'project-b', business_slug: 'beta', business_name: 'Beta Lab', label: 'Beta foreign work',
      association: {owned: false, linked: true, link_id: 'link-beta', link_revision: 1}}),
    workItem({id: 'archived-task', label: 'Archived old work', archived: true, association: {owned: true, linked: false}}),
    workItem({id: 'dup', label: 'Duplicate row that must collapse', association: {owned: true, linked: false}}),
  ], next_cursor: 'work-page-2'},
  'project-b': {items: [workItem({id: 'dup', business_id: 'project-b', business_slug: 'beta', business_name: 'Beta Lab', label: 'Beta duplicate id'})], next_cursor: null},
};
const projectWorkCalls = [];

// Private conversations: one thread per scoped source, with a durable owner read cursor.
const privateThreads = new Map(), privateUnread = new Set(), markReads = [], sendCalls = [], readGets = [];
const privateThread = (source) => {
  const key = `${source.type}:${source.business_id ?? 'global'}:${source.id}`;
  if (!privateThreads.has(key)) privateThreads.set(key, {key, messages: [{
    id: key + '-m1', sequence: 1, body: 'A private message about ' + source.id, created_by: 'chief', created_by_name: 'Ordubashy',
    author_kind: 'agent', author_role: 'coordinator', is_owner: false, created_at: new Date().toISOString(),
    refs: source.type === 'task' && source.id === 'story-1'
      ? [{type: 'reference', id: 'ref-1', business_id: null, label: 'Competitor pricing page'}] : undefined}], read: 0});
  return privateThreads.get(key);
};
let holdReadSource = null, releaseRead = null, readStarted = null;
let denyConversationSource = null;
const sourceOf = body => `${body.source.type}:${body.source.business_id ?? 'global'}:${body.source.id}`;
const calls = [];
// A real-shaped supported account-email Signoff. It uses the same frozen fastmail_account
// envelope the canonical Signoff preview reads; the body is shown in full, never synthesized.
const emailSignoff = {...proposal, id: 'signoff-1', review_business_id: null, category: null,
  title: 'Send the launch announcement', operation_name: 'account_email_send', operation_version: 1,
  preview: {
    title: 'Send the launch announcement',
    rationale: 'The launch page is approved and ready to share with the owner’s mailing list.',
    affected_scope: {kind: 'global', business_ids: []},
    changes: [{kind: 'structured', resource: {resource_type: 'fastmail_account', resource_id: 'mailbox-1', label: 'Owner mailbox'},
      fields: [
        {path: 'from_address', before: null, after: 'owner@example.test'},
        {path: 'to', before: null, after: ['customer@example.test']},
        {path: 'subject', before: null, after: 'The new campaign is live'},
        {path: 'body', before: null, after: 'Hello from Alpha Studio,\n\nThe new campaign is live today. Open the page to see the full story.\n\n— Ordubashy'}
      ]}],
    effects: []
  }, expected_targets: [], input: undefined};
const inputSignoff = {...proposal, id: 'signoff-input', review_business_id: null, category: 'input_request', title: 'Choose the launch date',
  input: {title: 'Choose the launch date', explanation: 'Can the launch proceed?', kind: 'question', selection_mode: 'single',
    choices: [{id: 'yes', label: 'Yes'}, {id: 'hold', label: 'Hold'}], selection_required: true, notes_required: false}};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  const readBody = async () => { let raw = ''; for await (const chunk of req) raw += chunk; return raw ? JSON.parse(raw) : {}; };
  const reply = data => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({status: 'ok', data})); };
  const fail = (code, message = 'Unavailable', status = code === 'forbidden' ? 403 : 400) => {
    res.statusCode = status; res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({status: 'error', error: {code, message}}));
  };
  if (!url.pathname.startsWith('/api/')) {
    const file = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : file.endsWith('.png') ? 'image/png' : file.endsWith('.woff2') ? 'font/woff2' : 'text/html');
    res.end(fs.readFileSync(file));
    return;
  }
  calls.push(url.pathname);
  const body = ['POST', 'PUT'].includes(req.method) ? await readBody() : {};
  if (url.pathname === '/api/biz') { res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify(businesses)); }
  if (url.pathname === '/api/session') return reply({principal_id: 'owner', principal_kind: 'human', is_owner: true, is_installation_owner: true});
  if (url.pathname === '/api/root/agents') return reply({agents: profiles});
  if (url.pathname === '/api/root/agent-pins') return reply({initialized: true, pinned_agents: []});
  if (/\/biz\/[^/]+\/agents/.test(url.pathname)) return reply([]);
  if (url.pathname.endsWith('/workspace_types')) return reply({types: [
    {key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy'], renderer: 'focus_board'},
    {key: 'threads', display_name: 'Threads', supported_roles: ['ordubashy'], renderer: 'threads'}]});
  if (url.pathname.endsWith('/calendar_calendars_list')) return reply({configured: false, calendars: [], errors: []});
  if (url.pathname.endsWith('/calendar_today')) return reply({calendar: null, events: []});
  if (url.pathname.endsWith('/focus_notes_list')) return reply({notes: [], revision: 1});
  if (url.pathname.endsWith('/focus_notes_save')) return reply({notes: [], revision: 2});
  if (url.pathname.endsWith('/reply-notifications')) return reply({items: []});
  if (url.pathname.endsWith('/browser-surfaces')) return reply({browsers: [], desktop: null});
  if (url.pathname.endsWith('/operations/focus_get')) {
    if (body.item_id) {
      const item = board.items.find(entry => entry.id === body.item_id);
      return item ? reply({item, revision: board.revision}) : fail('not_found', 'No card');
    }
    return reply({...board, items: board.items.filter(item => !item.unavailable && !item.archived), next_offset: null});
  }
  if (url.pathname.endsWith('/operations/focus_present')) {
    focusPresentCalls.push({args: body});
    const source = body.source;
    const existing = board.items.find(item => item.source?.type === source.type && item.source?.id === source.id);
    if (!existing) {
      const business = businesses.find(row => (row.business_id || row.id) === source.id);
      board.items.push(card({id: source.id, type: source.type, label: business?.name || source.id, display_name: 'Focus-only pin name',
        business_id: source.business_id ?? null, business_slug: business?.slug || null, business_name: business?.name || null}));
    }
    return reply({item: board.items.at(-1), outcome: 'presented'});
  }
  if (url.pathname.endsWith('/operations/dev_tickets_list')) {
    devTicketCalls.push({path: url.pathname, args: body});
    if (denyDevTicketRead) return fail('forbidden', 'Dev report denied');
    if (body.ticket_id === 'ticket-wrong') return reply({items: [{...globalTicket, id: 'ticket-wrong', business_id: 'project-b'}], next_offset: null});
    const rows = [projectTicket, globalTicket, betaTicket, unknownScopeTicket].filter(ticket => !body.ticket_id || ticket.id === body.ticket_id);
    return reply({items: rows, next_offset: null});
  }
  if (url.pathname.endsWith('/operations/tasks_get')) {
    if (body.task_id === 'convert-me') return reply({task: businessConversionTask});
    if (body.task_id === 'global-convert') return reply({task: globalConversionTask});
    if (body.task_id === 'external-convert') return reply({task: externalConversionTask});
    if (body.task_id === 'failed-convert') return reply({task: failedConversionTask});
    if (body.task_id === 'external-clean') return reply({task: externalCleanTask});
    if (body.task_id === 'external-global') return reply({task: externalGlobalTask});
    if (body.task_id === 'fresh-business') return reply({task: freshBusiness});
    if (body.task_id === 'fresh-global') return reply({task: freshGlobal});
    if (body.task_id === 'retry-task' && retryTaskFailures > 0) {
      retryTaskFailures--; retryTaskAttempts++;
      return fail('temporarily_unavailable', 'Retry requested', 503);
    }
    if (body.task_id === 'story-1' && denyStoryRead) return fail('forbidden', 'Manager denied Story');
    if (body.task_id === 'story-1') { storyCall.reads++; return reply({task: {...story, overview: story.overview,
      related_tasks: [{id: 'linked-1', title: 'Linked launch review', task_type: 'task', status: 'working', business_id: 'project-a', business_slug: 'alpha'}],
      related_tasks_next_cursor: null}}); }
    if (body.task_id === 'customer-1') return reply({task: {id: 'customer-1', title: 'Customer One', task_type: 'customer', overview: 'Saved customer summary',
      customer_kind: 'person', customer_profile: {}, revision: 1, business_id: 'project-a', business_slug: 'alpha', status: 'open'}});
    if (body.task_id === 'task-1') return reply({task: {id: 'task-1', title: 'Ordinary task', task_type: 'task', status: 'working', revision: 1,
      business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {},
      children: [{id: 'child-2', title: 'Child two', status: 'open', business_id: 'project-a', business_slug: 'alpha'}]}});
    return reply({task: {id: body.task_id, title: body.task_id === 'child-2' ? 'Child two' : body.task_id, status: 'open', revision: 1,
      business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {}}});
  }
  if (url.pathname === '/api/biz/alpha/task/task-1') {
    storyCall.saves.push({task_id: 'task-1', changes: body.changes, set: body.set, expected_revision: body.expected_revision, key: body.idempotency_key});
    // The legacy task API returns an unwrapped task, unlike the operations envelope.
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({task: {id: 'task-1', title: body.set?.title || 'Ordinary task', task_type: 'task', status: 'working', revision: 2,
      business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {},
      children: [{id: 'child-2', title: 'Child two', status: 'open', business_id: 'project-a', business_slug: 'alpha'}]}}));
    return;
  }
  if (url.pathname === '/api/biz/alpha/task/failed-convert') {
    if (failConversionSave) {
      res.statusCode = 503; res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({status: 'error', error: {code: 'temporarily_unavailable', message: 'Conversion response lost'}}));
      return;
    }
    failedConversionTask = {...failedConversionTask, task_type: body.set?.task_type || failedConversionTask.task_type,
      overview: body.set?.overview ?? failedConversionTask.overview, revision: failedConversionTask.revision + 1};
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({task: failedConversionTask}));
    return;
  }
  if (url.pathname === '/api/biz/alpha/task/convert-me') {
    businessConversionTask = {...businessConversionTask, task_type: body.set?.task_type || businessConversionTask.task_type,
      overview: body.set?.overview ?? businessConversionTask.overview, revision: businessConversionTask.revision + 1};
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({task: businessConversionTask}));
    return;
  }
  if (url.pathname.endsWith('/operations/assignees_list')) return reply({assignees: [{id: 'owner', kind: 'human', display_name: 'Owner', is_owner: true}]});
  if (url.pathname.endsWith('/operations/tasks_update')) {
    storyCall.saves.push({task_id: body.task_id, changes: body.changes, expected_revision: body.expected_revision, key: body.idempotency_key});
    if (body.task_id === 'global-convert') {
      globalConversionTask = {...globalConversionTask, task_type: body.changes?.task_type || globalConversionTask.task_type,
        overview: body.changes?.overview ?? globalConversionTask.overview, revision: globalConversionTask.revision + 1};
      return reply({task: globalConversionTask});
    }
    if (body.task_id === 'task-1') {
      return reply({task: {id: 'task-1', title: body.changes.title || 'Ordinary task', task_type: 'task', status: 'working', revision: 2,
        business_id: 'project-a', business_slug: 'alpha', tags: [], assignee_ids: [], fields: {},
        children: [{id: 'child-2', title: 'Child two', status: 'open', business_id: 'project-a', business_slug: 'alpha'}]}});
    }
    storyCall.keyHistory.push(body.idempotency_key);
    if (storyCall.saveFails > 0) { storyCall.saveFails--; return fail('temporarily_unavailable', 'Lost response', 503); }
    if (body.expected_revision !== story.revision) return fail('revision_conflict', 'Story changed', 409);
    story.overview = body.changes.overview ?? story.overview; story.revision++;
    return reply({task: {...story}});
  }
  if (url.pathname.endsWith('/operations/tasks_list')) {
    if (body.parent_id === 'story-1') return reply({tasks: [{id: 'child-1', title: 'Draft the landing page', status: 'working', task_type: 'task',
      business_id: 'project-a', business_slug: 'alpha'}], next_cursor: null});
    return reply({tasks: [], next_cursor: null});
  }
  if (url.pathname.endsWith('/operations/tasks_associated_records')) {
    if (body.task_id === 'story-1' && body.record_type === 'action') return reply({items: [
      {type: 'action', id: 'global/action-related', business_id: null, label: 'Associated action', state: 'open', task_ids: ['story-1', 'child-1']},
    ], next_cursor: null});
    if (body.task_id === 'story-1' && body.record_type === 'signoff') return reply({items: [
      {type: 'signoff', id: 'signoff-1', business_id: null, label: 'Send the launch announcement', state: 'pending', task_ids: ['story-1']},
    ], next_cursor: null});
    return reply({items: [], next_cursor: null});
  }
  if (url.pathname.endsWith('/operations/relationships_list')) {
    if (body.item?.id === 'story-1') return reply({items: [
      {id: 'relationship-action', source: {type: 'task', id: 'story-1'},
        target_item: {type: 'action', id: 'global/action-related', business_id: null, label: 'Associated action', resource_type: 'action_logs'}},
      {id: 'relationship-signoff', source: {type: 'task', id: 'story-1'},
        target_item: {type: 'signoff', id: 'signoff-1', business_id: null, label: 'Send the launch announcement', resource_type: 'signoffs', can_thread: true}}
    ], next_cursor: null});
    return reply({items: [], next_cursor: null});
  }
  if (url.pathname.endsWith('/operations/tasks_thread_get')) return reply({notes: [{id: 'n1', sequence: 1, entry_kind: 'system', title: 'Created', body: 'Story activity', created_at: '2026-10-27T09:00:00Z'}], next_cursor: null});
  if (url.pathname.endsWith('/operations/owner_conversations_list')) {
    const rows = [
      {id: 'thread-project', source: {type: 'project', id: 'project-a', business_id: 'project-a'},
        resolved_source: {type: 'project', id: 'project-a', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Alpha Studio'},
        latest_sequence: 9, last_message_at: '2026-10-27T10:00:00Z', preview: 'Project private message', unread: false},
      {id: 'thread-task', source: {type: 'task', id: 'task-1', business_id: 'project-a'},
        resolved_source: {type: 'task', id: 'task-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Ordinary task', state: 'working'},
        latest_sequence: 7, last_message_at: '2026-10-27T09:00:00Z', preview: 'Task private message', unread: false},
      // The same scoped Story the Focus card uses: its Chat draft is shared by source, not by entry point.
      {id: 'thread-story', source: {type: 'task', id: 'story-1', business_id: 'project-a'},
        resolved_source: {type: 'task', id: 'story-1', business_id: 'project-a', business_slug: 'alpha', business_name: 'Alpha Studio', label: 'Spring campaign', state: 'open'},
        latest_sequence: 4, last_message_at: '2026-10-27T08:00:00Z', preview: 'Story private message', unread: false},
      {id: 'thread-signoff', source: {type: 'signoff', id: 'signoff-1', business_id: null},
        resolved_source: {type: 'signoff', id: 'signoff-1', business_id: null, business_slug: null, business_name: 'Global', label: 'Send the launch announcement', state: 'pending'},
        latest_sequence: 2, last_message_at: '2026-10-27T07:00:00Z', preview: 'Signoff private message', unread: false},
      {id: 'thread-signoff-input', source: {type: 'signoff', id: 'signoff-input', business_id: null},
        resolved_source: {type: 'signoff', id: 'signoff-input', business_id: null, business_slug: null, business_name: 'Global', label: 'Choose the launch date', state: 'pending'},
        latest_sequence: 2, last_message_at: '2026-10-27T07:00:00Z', preview: 'Input request', unread: false},
    ];
    return reply({items: rows, next_before: null});
  }
  if (url.pathname.endsWith('/operations/owner_conversation_related')) return reply({items: [], next_offset: null});
  if (url.pathname.includes('/operations/owner_conversation_')) {
    const key = sourceOf(body);
    if (denyConversationSource === key) return fail('forbidden', 'This source is no longer accessible');
    const thread = privateThread(body.source);
    if (url.pathname.endsWith('owner_conversation_send')) {
      sendCalls.push({source: body.source, text: body.text, key: body.idempotency_key});
      const sequence = thread.messages.at(-1).sequence + 1;
      thread.messages.push({id: key + '-m' + sequence, sequence, body: body.text, created_by: 'owner', created_by_name: 'Owner',
        author_kind: 'human', is_owner: true, created_at: new Date().toISOString()});
      return reply({message: thread.messages.at(-1), conversation: {id: key, revision: sequence, latest_sequence: sequence, message_count: thread.messages.length}});
    }
    if (url.pathname.endsWith('owner_conversation_mark_read')) {
      markReads.push({source: body.source, sequence: body.sequence});
      thread.read = body.sequence; privateUnread.delete(key);
      return reply({read_state: {last_read_sequence: thread.read, unread: privateUnread.has(key)}});
    }
    readGets.push({source: body.source});
    const messages = thread.messages.slice(-20);
    if (holdReadSource === key) { holdReadSource = null; readStarted?.(); await new Promise(resolve => { releaseRead = resolve; }); }
    return reply({conversation: {id: key, source: body.source, audience: 'owner_ordubashy', revision: 1,
      latest_sequence: thread.messages.at(-1).sequence, message_count: thread.messages.length},
      messages, next_before: null,
      participants: [{created_by: 'owner', created_by_name: 'Owner', author_kind: 'human', is_owner: true},
        {created_by: 'chief', created_by_name: 'Ordubashy', author_kind: 'agent'}],
      read_state: {last_read_sequence: thread.read, unread: privateUnread.has(key)}});
  }
  if (url.pathname.endsWith('/operations/project_record_get')) {
    if (projectRecordFailures > 0) { projectRecordFailures--; return fail('temporarily_unavailable', 'Project unavailable', 503); }
    const id = body.project_id;
    return reply({project: id === 'project-b' ? projectB : {...project, name: projectName}});
  }
  if (url.pathname.endsWith('/operations/project_items_list')) {
    inventoryCalls.push({args: body});
    if (denyProjectItems) return fail('forbidden', 'Project inventory denied');
    const section = body.section || 'work';
    if (failInventorySection === section) return fail('temporarily_unavailable', 'Try again', 503);
    if (holdInventory && body.section === holdInventory.section) {
      holdInventory.started?.();
      await new Promise(resolve => { holdInventory.release = resolve; });
    }
    if (lateProjectItems) { const late = lateProjectItems; lateProjectItems = null; return reply(late); }
    if (body.root_tasks_only) {
      projectWorkCalls.push({project_id: body.project_id, section: body.section, root_tasks_only: body.root_tasks_only,
        limit: body.limit, include_closed: body.include_closed, include_archived: body.include_archived, cursor: body.cursor});
      return reply(projectWorkByProject[body.project_id] || {items: [], next_cursor: null});
    }
    const rows = (inventory[section] || []).slice();
    if (body.cursor === 'work-page-2') return reply({items: [workItem({id: 'page-two', label: 'Second page work'})], next_cursor: null});
    return reply({items: rows, next_cursor: section === 'work' ? 'work-page-2' : null});
  }
  if (url.pathname.endsWith('/operations/project_items_candidates')) {
    candidateCalls.push({args: body});
    return reply({items: candidates.filter(item => !body.type || item.type === body.type), next_cursor: null});
  }
  if (url.pathname.endsWith('/operations/project_items_link')) {
    linkCalls.push({args: body});
    if (linkFails > 0) { linkFails--; return fail('temporarily_unavailable', 'Lost link response', 503); }
    return reply({item: {...body.item, label: 'Linked candidate', can_thread: true, association: {owned: false, linked: true, link_id: 'link-new', link_revision: 1}},
      link: {id: 'link-new', revision: 1}, already_present: false});
  }
  if (url.pathname.endsWith('/operations/project_items_unlink')) {
    unlinkCalls.push({args: body});
    return reply({removed: true});
  }
  if (url.pathname.endsWith('/operations/routines_runs')) {
    routineRunCalls.push({path: url.pathname, args: body});
    const runs = [
      {id: 'run-1', routine_id: 'routine-1', business_id: 'project-a', state: 'succeeded', trigger: 'schedule',
        started_at: '2026-10-27T09:00:00Z', result_json: {job_id: 'job-1', definition: {name: 'Weekly digest'}}},
      {id: 'run-2', routine_id: 'routine-1', business_id: 'project-a', state: 'failed', trigger: 'manual',
        started_at: '2026-10-26T09:00:00Z', result_json: {job_id: 'job-2'}},
      {id: 'run-3', routine_id: 'routine-1', business_id: 'project-a', state: 'queued', trigger: 'schedule',
        started_at: '2026-10-28T09:00:00Z', result_json: {}},
    ];
    return body.cursor === 'runs-page-2' ? reply({items: runs.slice(2), next_cursor: null})
      : reply({items: runs.slice(0, 2), next_cursor: 'runs-page-2'});
  }
  if (url.pathname.endsWith('/operations/jobs_get')) {
    jobGetCalls.push({path: url.pathname, args: body});
    return reply({job: {id: body.job_id, kind: 'routine.run', state: 'succeeded', attempts: 1,
      created_at: '2026-10-27T09:00:00Z', updated_at: '2026-10-27T09:01:00Z', error_json: null}, details_available: false});
  }
  if (url.pathname.endsWith('/operations/actions_get')) {
    if (denyActionRead) return fail('forbidden', 'Manager denied Action');
    return reply({action: {id: body.action_id, text: 'Canonical action text', category: 'delivery', occurred: {date: '2026-10-27'},
      performed_by: {name: 'Ordubashy'}, recorded_by: {name: 'Ordubashy'}, revision: 2}});
  }
  if (url.pathname.endsWith('/operations/tables_get')) {
    return reply({table: {id: body.table_id, title: 'Campaign calendar', description: 'Launch dates by channel.', revision: 1,
      row_count: 2, columns: [{id: 'channel', name: 'Channel'}, {id: 'date', name: 'Date'}]}});
  }
  if (url.pathname.endsWith('/operations/tables_rows_list')) {
    return reply({table_revision: 1, items: [{id: 'row-1', position: 0, cells: {channel: 'Web', date: '2026-11-01'}}], next_cursor: 'rows-page-2'});
  }
  if (url.pathname.endsWith('/operations/context_items_get')) {
    return reply({item: {type: 'document', id: body.item.id, kind: 'business_note', label: 'Context document', revision: 1}});
  }
  if (url.pathname.endsWith('/operations/notes_get')) {
    return reply({note: {id: body.document_id, kind: 'business_note', revision: 1, text: 'Authorized note text.\n\nSecond paragraph.'}});
  }
  if (url.pathname.endsWith('/operations/context_get')) {
    return reply({document: {id: body.document_id, kind: 'context_upload', revision: 1, body: 'Authorized document body.', complete: true}});
  }
  if (url.pathname.endsWith('/operations/project_item_get')) {
    if (denyProjectItemGet) return fail('forbidden', 'Project item denied');
    const requested = body.item || {};
    itemGetCalls.push({args: body});
    if (requested.type === 'routine') {
      const row = inventory.automation.find(entry => entry.id === requested.id) || inventory.automation[0];
      return reply({item: row, record: {routine: {id: row.id, name: row.label, status: row.state === 'paused' ? 'paused' : 'active',
        next: '2026-10-30T09:00:00Z', program: 'context.summarize', target: row.business_id == null ? 'root' : 'business',
        why: 'Summarize the week into a short digest.', revision: row.revision}}});
    }
    if (requested.type === 'reminder') {
      const row = inventory.other.find(entry => entry.id === requested.id) || inventory.other[0];
      return reply({item: row, record: {reminder: {reminder_id: row.id, revision: row.revision, text: row.summary,
        at: row.at, state: row.state}}});
    }
    if (requested.type === 'dev_ticket') {
      const row = inventory.other.find(entry => entry.id === requested.id);
      return reply({item: row, record: {ticket: projectTicket}});
    }
    const documentKind = requested.type === 'material' || requested.type === 'person' || requested.type === 'topic';
    const row = {type: requested.type, id: requested.id, business_id: requested.business_id, business_slug: requested.business_id === 'project-a' ? 'alpha' : null,
      business_name: 'Alpha Studio', label: documentKind ? 'Campaign brief' : requested.type === 'table' ? 'Campaign calendar' : 'Published the first page',
      revision: 1, resource_type: documentKind ? 'documents' : requested.type === 'table' ? 'data_tables' : requested.type === 'action' ? 'action_logs' : null,
      can_thread: requested.type === 'task'};
    if (requested.type === 'material') return reply({item: row, record: {document: {id: requested.id, title: 'Campaign brief',
      kind: 'business_note', revision: 1, body: 'The authorized read model body.\n\nA fuller second paragraph.'}}});
    if (requested.type === 'person') return reply({item: {...row, label: 'Campaign owner', resource_type: 'context_entities'},
      record: {person: {id: requested.id, label: 'Campaign owner', name: 'Campaign owner', role: 'Owner', org: 'Alpha Studio',
        about: 'The owner of the campaign.', revision: 1}}});
    if (requested.type === 'action') return reply({item: row, record: {action: {id: requested.id, text: 'The Canonical action text.', category: 'delivery',
      occurred: {date: '2026-10-27'}, performed_by: {name: 'Ordubashy'}, recorded_by: {name: 'Ordubashy'}, revision: 1}}});
    return reply({item: row, record: {summary: 'The authorized read model body.', revision: 1}});
  }
  if (url.pathname === '/api/root/signoffs/signoff-1') return reply({signoff: emailSignoff});
  if (url.pathname === '/api/root/signoffs/signoff-input') return reply({signoff: inputSignoff});
  if (url.pathname === '/api/root/signoffs') return reply({items: [emailSignoff, inputSignoff], next_cursor: null});
  if (url.pathname.startsWith('/api/root/signoffs/')) return reply({signoff: emailSignoff});
  if (url.pathname === '/api/root/global_progress_get') return reply({series: []});
  if (url.pathname.endsWith('/focus-layout')) return reply({revision: 1, blocks: [], card_ids: board.items.map(item => item.id)});
  return reply({items: [], messages: [], purposes: [], tags: [], agents: [], totals: {}});
});

const until = async (check, what) => { for (let i = 0; i < 240; i++) { if (check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };
const tab = page => page.locator('.agfocus-dock .rw-record:not([hidden])');
const currentTab = page => page.locator('.agfocus-dock .rw-record:not([hidden])');
const routeOk = data => ({status: 200, contentType: 'application/json', body: JSON.stringify({status: 'ok', data})});
const routeError = (code, message = 'Unavailable', status = 403) => ({status, contentType: 'application/json',
  body: JSON.stringify({status: 'error', error: {code, message}})});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    const errors = [];
    page.setDefaultTimeout(12000);
    page.on('pageerror', error => errors.push(error.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
    await page.goto(origin + '/#/global/agents');
    await page.locator('.agfocus-items [data-card-id="story-1"]').waitFor();
    assert.equal(await page.locator('.ttab[data-v="threads"]').count(), 0);
    assert.equal(await page.locator('.agfocus-items [data-card-id="project-a"] .agfocus-scope').isVisible(), false, 'Project cards have no duplicate same-name scope badge');
    assert.equal(await page.locator('.agfocus-items [data-card-id="project-a"] .ttl').textContent(), 'Alpha Studio', 'a Project card shows its real name, never the display-name override');

    // ---- Story: one full-width workspace, Chat default, dashboard replaces the chat area.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    let pane = currentTab(page);
    await pane.locator('.rw-head').waitFor();
    await pane.locator('.rw-conversation .gchat-scroll').waitFor();
    assert.equal(await pane.locator('.rw-title').textContent(), 'A very long Focus-only Story name the card shows');
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 2);
    assert.deepEqual(await pane.locator('.rw-tabs [role="tab"]').evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label'))),
      ['Story dashboard', 'Chat']);
    assert.equal(await pane.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'Chat is the fresh default');
    assert.equal(await pane.locator('.rw-dashboard').isVisible(), false);
    assert.equal(await page.locator('.record-toggle,.agfocus-thread-context,[data-manage],.agfocus-region,.agfocus-detail').count(), 0, 'no old Details/Context/side panel controls');
    assert.equal(await page.locator('.agthreads .agfocus-dock, .agfocus-dock .record-surface').count(), 0);
    // The same scoped conversation is reused on reselection; mounting twice is one controller key.
    assert.equal(await pane.locator('.rw-conversation[data-conversation]').count(), 1);

    if (shots) await pane.screenshot({path: path.join(shots, 'story-chat-desktop.png')});
    // A typed Focus Reference entered from a private Chat keeps its authorized Chat tab and binding.
    await pane.locator('.gchat-msg .gchat-reference').filter({hasText: 'Competitor pricing page'}).click();
    await currentTab(page).locator('.rw-title').filter({hasText: 'Competitor pricing page'}).waitFor();
    assert.equal(await currentTab(page).locator('.rw-tabs [role="tab"]').count(), 2, 'a chat-entry Focus Reference keeps Chat');
    assert.equal(await currentTab(page).locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    assert.deepEqual(await page.evaluate(() => _agpad.focusWorkspace.current().source), {type: 'reference', id: 'ref-1', business_id: null});
    await currentTab(page).locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Story'}).waitFor().catch(async () => { await pane.locator('[data-rw-tab="chat"]').waitFor(); });
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('.rw-dashboard').isVisible(), true);
    assert.equal(await pane.locator('.rw-chat').isVisible(), false, 'dashboard and chat are never side by side');
    assert.match(await pane.locator('.record-overview').textContent(), /First paragraph[\s\S]*Second paragraph/);
    assert.equal(await pane.locator('[data-story-overview]').isVisible(), false, 'read mode is not a permanent textarea');
    const storyTypeTab = pane.locator('.rw-tabs .task-type-icon-story').first();
    assert.equal(await storyTypeTab.count(), 1, 'the Story type tab keeps its icon');
    assert.match(await storyTypeTab.evaluate(el => getComputedStyle(el).maskImage || getComputedStyle(el).webkitMaskImage), /story\.svg/, 'the icon asset is a real mask, not a blank tab');
    await pane.locator('.record-child-row').filter({hasText: 'Draft the landing page'}).waitFor();
    await pane.locator('.record-link-open').filter({hasText: 'Associated action'}).waitFor();
    await pane.locator('.record-link-open').filter({hasText: 'Send the launch announcement'}).waitFor();
    await pane.locator('.task-links .task-link-chip').filter({hasText: 'Linked launch review'}).waitFor();
    if (shots) await pane.screenshot({path: path.join(shots, 'story-dashboard-desktop.png')});
    // Chat is not acknowledged while the dashboard is selected.
    {
      const key = 'task:project-a:story-1', thread = privateThread({type: 'task', id: 'story-1', business_id: 'project-a'});
      thread.messages.push({id: key + '-m2', sequence: 2, body: 'An unseen reply arrived while the dashboard was open', created_by: 'chief',
        created_by_name: 'Ordubashy', author_kind: 'agent', created_at: new Date().toISOString()});
      privateUnread.add(key);
      const readsBefore = markReads.length;
      await page.evaluate(() => agfocusRefresh(_agpad));
      await page.waitForTimeout(250);
      assert.equal(markReads.length, readsBefore, 'a hidden Chat never acknowledges an unseen message');
      await pane.locator('[data-rw-tab="chat"]').click();
      await until(() => markReads.length > readsBefore, 'chat acknowledgment after showing Chat');
      assert.equal(markReads.at(-1).sequence, 2);
      await page.waitForFunction(() => document.querySelector('.agfocus-dock .rw-conversation[data-conversation-unread="false"]'));
    }
    // Drafts and unsaved dashboard edits survive a tab switch and Back.
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').fill('Unsent Story note');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').click();
    await pane.locator('[data-story-overview]').fill('First paragraph of the campaign direction.\n\nSecond paragraph with the decision context.\n\nThird paragraph typed here.');
    await pane.locator('[data-rw-tab="chat"]').click();
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Unsent Story note');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    assert.equal(await pane.locator('[data-story-overview]').inputValue(), 'First paragraph of the campaign direction.\n\nSecond paragraph with the decision context.\n\nThird paragraph typed here.');
    // Uncertain retry keeps the exact key; conflict recovery keeps the typed overview.
    storyCall.saveFails = 1;
    await pane.locator('[data-story-save]').click();
    await page.waitForFunction(() => !document.querySelector('[data-story-error]').hidden);
    assert.match(await pane.locator('[data-story-error]').textContent(), /Couldn't confirm/);
    await pane.locator('[data-story-save]').click();
    await until(() => storyCall.saves.length >= 2, 'the retried save');
    assert.equal(storyCall.saves.at(-1).key, storyCall.keyHistory.at(-1), 'the retry uses the same request key');
    assert.equal(storyCall.saves[0].key, storyCall.saves[1].key);
    await pane.locator('[data-story-overview]').waitFor({state: 'hidden'});
    assert.equal(await pane.locator('[data-story-overview]').isVisible(), false, 'a successful save returns to read mode');
    assert.match(await pane.locator('.record-overview').textContent(), /Third paragraph typed here/);
    storyCall.saveFails = 0;
    // Conflict recovery: adopt the latest revision while keeping the typed overview.
    await pane.locator('.record-overview').click();
    await pane.locator('[data-story-overview]').fill('A conflicting edit that must be kept');
    story.revision = 99; story.overview = 'The server overview changed elsewhere';
    await pane.locator('[data-story-save]').click();
    await pane.locator('[data-story-latest]').waitFor();
    assert.match(await pane.locator('[data-story-error]').textContent(), /changed since you opened it/i);
    assert.equal(await pane.locator('[data-story-overview]').inputValue(), 'A conflicting edit that must be kept');
    await pane.locator('[data-story-latest]').click();
    await until(() => storyCall.reads >= 2);
    assert.equal(await pane.locator('[data-story-overview]').inputValue(), 'A conflicting edit that must be kept', 'adopting the revision never overwrites unsaved text');
    await pane.locator('[data-story-save]').click();
    await until(() => storyCall.saves.length >= 4);
    assert.equal(storyCall.saves.at(-1).expected_revision, 99);
    await page.waitForFunction(() => document.querySelector('.agfocus-dock .record-overview')?.textContent.includes('A conflicting edit that must be kept'));
    assert.match(await pane.locator('.record-overview').textContent(), /A conflicting edit that must be kept/);
    assert.deepEqual(errors, []);
    // Associated Actions are a same-pane navigation: no legacy modal, and an explicit Global
    // action id does not inherit the Story's Project scope.
    await pane.locator('.record-link-open').filter({hasText: 'Associated action'}).click();
    await pane.locator('.rw-action-text').waitFor();
    assert.equal(await page.locator('.modal').count(), 0, 'a related Action never opens the legacy modal');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.id), 'global/action-related');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.business_id), null, 'a Global action keeps its null scope');
    assert.match(await pane.locator('.rw-action-text').textContent(), /Canonical action text/);
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.record-overview').waitFor();
    // A dashboard hidden by navigation retains its exact scroll position.
    const scrollBefore = await pane.locator('.rw-dashboard').evaluate(el => {
      el.querySelector('.record-overview').style.height = '2200px'; el.scrollTop = 320; return el.scrollTop;
    });
    await page.evaluate(() => _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'scroll-ref', business_id: 'project-a'}, label: 'Scroll reference', summary: 'Synthetic reader'})));
    await page.evaluate(() => _agpad.focusWorkspace.back());
    pane = currentTab(page);
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('.rw-dashboard').evaluate(el => el.scrollTop), scrollBefore, 'Back restores dashboard scroll');
    // A direct Chat -> dashboard tab round trip must not overwrite the retained position with the hidden pane's 0.
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    assert.equal(await pane.locator('.rw-dashboard').evaluate(el => el.scrollTop), scrollBefore, 'tab round trip restores dashboard scroll');
    // Returning to an ancestor by a related link pops exactly the finite slice above it, never loops.
    await page.evaluate(async () => {
      window.managerRoot = {..._agpad.focusWorkspace.current()};
      await _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'action', id: 'ancestor-child', business_id: null}, label: 'Child'}));
    });
    await currentTab(page).locator('.rw-action-text').waitFor();
    const ancestorReturn = await page.evaluate(async () => {
      const api = _agpad.focusWorkspace;
      const child = api.entry();
      const original = Array.prototype.push;
      let count = 0;
      Array.prototype.push = function(...values) {
        if (values.length === 1 && values[0] === child && ++count > 100) throw new Error('NONADVANCING_ANCESTOR');
        return original.apply(this, values);
      };
      try {
        const navigate = await api.navigate(window.managerRoot);
        const open = await api.open(window.managerRoot, {root: false});
        return {navigate, open, count, source: api.current()?.source, childAlive: child.alive};
      } finally { Array.prototype.push = original; }
    });
    assert.deepEqual(ancestorReturn, {navigate: true, open: true, count: 0, source: {type: 'task', id: 'story-1', business_id: 'project-a'}, childAlive: false},
      'both ancestor paths collapse without mutating in a loop');
    pane = currentTab(page);
    await pane.locator('.record-overview').waitFor();
    // The finite ancestor slice is guarded: a dirty intermediate Customer prompts; Cancel
    // keeps it current, Discard removes only that slice and returns to the Story.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'task', id: 'customer-1', business_id: 'project-a'},
      label: 'Customer One', task_type: 'customer', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('[data-customer-summary]').fill('Dirty intermediate that must be guarded');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.hasUnsaved()), true);
    await page.evaluate(() => { void _agpad.focusWorkspace.navigate(window.managerRoot); });
    await page.locator('.modal').waitFor();
    await page.locator('.modal').getByRole('button', {name: 'Keep editing'}).click();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.id), 'customer-1', 'Keep editing leaves the dirty child current');
    await page.evaluate(() => { void _agpad.focusWorkspace.navigate(window.managerRoot); });
    await page.locator('.modal').waitFor();
    await page.locator('.modal').getByRole('button', {name: 'Discard'}).click();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.id), 'story-1', 'Discard removes exactly the ancestor slice');
    pane = currentTab(page);
    await pane.locator('.record-overview').waitFor();
    // A failed close-save keeps the draft and the pane; Discard then actually completes the close.
    await pane.locator('.record-overview').click();
    await pane.locator('[data-story-overview]').fill('A close draft that must survive a failed save');
    storyCall.saveFails = 1;
    await pane.locator('.rw-close').click();
    await page.locator('.modal').getByRole('button', {name: 'Save', exact: true}).click();
    await until(() => storyCall.saves.at(-1)?.changes?.overview === 'A close draft that must survive a failed save', 'failed close save');
    await page.waitForFunction(() => !document.querySelector('.modal'));
    assert.equal(await page.locator('.agfocus-dock').evaluate(el => el.hidden), false, 'failed Save leaves the record open');
    assert.equal(await pane.locator('[data-story-overview]').inputValue(), 'A close draft that must survive a failed save');
    assert.equal(await pane.locator('[data-story-error]').isVisible(), true, 'the dashboard retains its retry error');
    await pane.locator('.rw-close').click();
    await page.locator('.modal').getByRole('button', {name: 'Discard', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    storyCall.saveFails = 0;
    assert.deepEqual(errors, []);
    // An initial 503 gets a real Retry control and a second, live request; it never stays stuck.
    retryTaskFailures = 1;
    await page.evaluate(async () => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'retry-task', business_id: 'project-a'},
        label: 'Retry task'}, {biz: 'alpha'}));
    });
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.getByRole('alert').waitFor();
    assert.match(await pane.getByRole('alert').textContent(), /Couldn't|Retry/);
    assert.equal(await pane.getByRole('button', {name: 'Retry', exact: true}).count(), 1);
    await pane.getByRole('button', {name: 'Retry', exact: true}).click();
    await pane.locator('.tdetail').waitFor();
    assert.equal(await pane.getByRole('alert').count(), 0, 'the successful retry replaces the transient error');
    assert.equal(retryTaskAttempts, 1);
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    // A hidden retained Story is invalidated by its own denied source/Chat; it cannot reappear on Back.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').waitFor();
    await page.evaluate(async () => {
      window.managerHidden = _agpad.focusWorkspace.entry();
      await _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'action', id: 'after-story', business_id: null}, label: 'After Story'}));
    });
    await currentTab(page).locator('.rw-action-text').waitFor();
    denyStoryRead = true;
    denyConversationSource = 'task:project-a:story-1';
    await page.evaluate(() => window.managerHidden.chat.refresh());
    await page.waitForFunction(() => window.managerHidden.alive === false);
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.id), 'after-story', 'the visible unrelated record is preserved');
    assert.equal(await currentTab(page).locator('.rw-action-text').count(), 1);
    await page.evaluate(() => _agpad.focusWorkspace.back());
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.id), 'after-story', 'a denied hidden Story never returns through Back');
    denyStoryRead = false; denyConversationSource = null;
    await currentTab(page).locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    // A denied Action revalidation invalidates the whole entry; no stale authorized text remains.
    await page.evaluate(async () => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'action', id: 'denied-action', business_id: null}, label: 'Denied Action'}));
    });
    await currentTab(page).locator('.rw-action-text').waitFor();
    await page.evaluate(() => _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'after-action', business_id: null}, label: 'Other'})));
    denyActionRead = true;
    await page.evaluate(() => _agpad.focusWorkspace.back());
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    assert.equal(await page.locator('.rw-action-text').count(), 0, 'denied Action content is disposed, not retained');
    denyActionRead = false;
    // Customer exposes dirty(), not hasUnsaved(); Close must prompt and Keep editing must preserve it.
    await page.evaluate(async () => {
      agfocusShowDock(_agpad);
      await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'customer-1', business_id: 'project-a'},
        label: 'Customer One', task_type: 'customer', can_thread: true}, {biz: 'alpha'}));
    });
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-panel').waitFor();
    await pane.locator('[data-customer-summary]').fill('Unsaved customer summary');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.hasUnsaved()), true, 'Customer dirty() is visible to the workspace');
    await pane.locator('.rw-close').click();
    await page.locator('.modal').waitFor();
    assert.equal(await page.locator('.agfocus-dock').evaluate(el => el.hidden), false);
    await page.locator('.modal').getByRole('button', {name: 'Keep editing'}).click();
    assert.equal(await pane.locator('[data-customer-summary]').inputValue(), 'Unsaved customer summary');
    await pane.locator('.rw-close').click();
    await page.locator('.modal').getByRole('button', {name: 'Discard'}).click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    // A normal Task dashboard has no nested private conversation and child navigation updates header/source/Back.
    await page.locator('[data-card-id="task-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.tdetail').waitFor();
    assert.equal(await pane.locator('.rw-dashboard [data-conversation]').count(), 0, 'the dashboard mounts no second private conversation');
    // tDetail repaints after a successful write; the workspace adapter must stay pointed at the new controller.
    await pane.locator('.dtitle').click();
    await pane.locator('.dtitle-in').fill('Ordinary task saved');
    await pane.locator('.dtitle-in').press('Enter');
    await pane.locator('.dtitle').filter({hasText: 'Ordinary task saved'}).waitFor();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.hasUnsaved()), false);
    await pane.locator('[data-task-type]').selectOption('story');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.hasUnsaved()), true, 'the replacement tDetail still reports dirty');
    await pane.locator('.rw-close').click();
    await page.locator('.modal').waitFor();
    await page.locator('.modal').getByRole('button', {name: 'Keep editing'}).click();
    assert.equal(await pane.locator('.tdetail').count(), 1, 'Keep editing preserves the repainted editor');
    await pane.locator('.rw-close').click();
    await page.locator('.modal').getByRole('button', {name: 'Discard'}).click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    // Reopen the Task for child-navigation coverage.
    await page.locator('[data-card-id="task-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.tdetail').waitFor();
    await pane.locator('[data-open-task="child-2"]').click();
    await pane.locator('.rw-title').filter({hasText: 'Child two'}).waitFor();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.id), 'child-2');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.business_id), 'project-a');
    assert.equal(await pane.locator('.rw-back').isVisible(), true);
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Ordinary task'}).waitFor();
    // A different right-list selection starts a fresh Chat-first root path, even when the old
    // stack held other records; only clicking the already selected row keeps current state.
    await page.evaluate(() => _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'stack-doc', business_id: null},
      label: 'Stack document', summary: 'Synthetic reader'})));
    await page.locator('[data-card-id="project-b"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Beta Lab'}).waitFor();
    assert.equal(await pane.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'a fresh root selection is Chat-first');
    assert.equal(await pane.locator('.rw-back').isVisible(), false, 'a fresh root selection has no prior stack');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    // Project and non-Task dashboard initial load failures use the same live Retry contract.
    projectRecordFailures = 1;
    await page.locator('[data-card-id="project-b"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.rw-dashboard [role="alert"]').waitFor();
    assert.equal(await pane.locator('.rw-dashboard').getByRole('button', {name: 'Retry', exact: true}).count(), 1);
    await pane.locator('.rw-dashboard').getByRole('button', {name: 'Retry', exact: true}).click();
    await pane.locator('.rw-project-name').filter({hasText: 'Beta Lab'}).waitFor();
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    // ---- Project: full-width dashboard, lazy sections, real inventory and Add existing Project.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    assert.equal(await page.locator('.agfocus-dock').isVisible(), false, 'Close restores the preceding main Chat surface');
    // Selecting a Project opens Chat without expanding or reading its inventory.
    const projectBWorkBefore = projectWorkCalls.filter(call => call.project_id === 'project-b').length;
    await page.locator('[data-card-id="project-b"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-head').waitFor();
    assert.equal(await pane.locator('.rw-title').textContent(), 'Beta Lab');
    assert.equal(await pane.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('[data-card-id="project-b"] .agfocus-project-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(projectWorkCalls.filter(call => call.project_id === 'project-b').length, projectBWorkBefore, 'selection never forces a Project expansion read');
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('.rw-conversation .gchat-body').waitFor();
    // Both entry points send the same canonical Project source, including the Project UUID as business_id.
    assert.deepEqual(readGets.at(-1).source, {type: 'project', id: 'project-b', business_id: 'project-b'});
    // A typed Project reference keeps its canonical chip and gains an explicit same-pane Open action.
    const projectReference = await page.evaluate(() => {
      const entry = conversationRecordEntry({type: 'project', id: 'project-b', business_id: 'project-b', business_slug: 'beta', label: 'Beta Lab'});
      return {typed: entry.typedReferences.length, actions: entry.references.map(ref => ref.label)};
    });
    assert.deepEqual(projectReference, {typed: 1, actions: ['Open Project']});
    await page.evaluate(() => document.querySelector('[data-card-id="ref-1"] .ttl').click());
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Competitor pricing page'}).waitFor();
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 2, 'a Focus Reference keeps its private Chat');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.agfocus-reference-frame').waitFor();
    assert.equal(await pane.locator('.rw-dashboard [data-conversation]').count(), 0, 'the URL reader has no nested conversation');
    if (shots) await pane.screenshot({path: path.join(shots, 'focus-reference-desktop.png')});
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // Project A dashboard: authoritative owned + linked work, lazy families and row navigation.
    await page.locator('[data-card-id="project-a"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Alpha Studio'}).waitFor();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.rw-project-name').filter({hasText: 'Alpha Studio'}).waitFor();
    assert.equal(await pane.locator('.rw-project-summary').textContent(), 'A synthetic Project used by the workspace test.');
    assert.deepEqual(await pane.locator('[data-project-section]').evaluateAll(nodes => nodes.map(node => node.textContent)),
      ['Work', 'Signoffs', 'Actions', 'Documents', 'Tables', 'Automation', 'Other']);
    assert.deepEqual(inventoryCalls.filter(call => call.args.project_id === 'project-a').map(call => call.args.section), ['work'], 'only the visible section is read');
    assert.equal(inventoryCalls.at(-1).args.limit, 25);
    assert.equal(inventoryCalls.at(-1).args.root_tasks_only, false);
    await pane.locator('.rw-project-row').first().waitFor();
    const workRows = pane.locator('.rw-project-row');
    assert.equal(await workRows.count(), 4, 'owned, global linked, foreign linked and owned+linked work');
    assert.equal(await workRows.nth(0).locator('.rw-project-label').textContent(), 'Top-level Alpha work');
    assert.equal(await workRows.nth(0).locator('.rw-project-scope').isVisible(), false, 'own-scope work has no repeated scope label');
    assert.equal(await workRows.nth(1).locator('.rw-project-scope').textContent(), 'Global');
    assert.equal(await workRows.nth(1).locator('.rw-project-assoc').textContent(), 'Linked');
    assert.equal(await workRows.nth(1).locator('.rw-project-unlink').count(), 1, 'explicit links are unlinkable');
    assert.equal(await workRows.nth(2).locator('.rw-project-scope').textContent(), 'Beta Lab', 'foreign work keeps its own scope badge');
    assert.equal(await workRows.nth(2).locator('.rw-project-scope').isVisible(), true, 'foreign scope is never hidden as redundant');
    assert.equal(await workRows.nth(3).locator('.rw-project-unlink').count(), 1, 'owned+linked rows expose their explicit link');
    // The authoritative Project name updates the workspace header and Focus row without changing identity.
    projectName = 'Alpha Studio Prime';
    await workRows.nth(0).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Top-level Alpha work'}).waitFor();
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').filter({hasText: 'Alpha Studio Prime'}).waitFor();
    assert.equal(await page.locator('[data-card-id="project-a"] .ttl').textContent(), 'Alpha Studio Prime');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current().source.id), 'project-a', 'rename never changes source identity');
    projectName = 'Alpha Studio';
    // Cached project_item_prefetched never bypasses a later association check on reveal.
    await pane.locator('.rw-project-row').filter({hasText: 'Top-level Alpha work'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Top-level Alpha work'}).waitFor();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.tdetail').waitFor();
    await page.evaluate(() => _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'after-project-task', business_id: null}, label: 'Other'})));
    denyProjectItemGet = true;
    await page.evaluate(async () => await _agpad.focusWorkspace.back());
    await page.waitForFunction(() => document.querySelector('.rw-record:not([hidden]) .rw-project-name'));
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.id), 'project-a', 'a denied Project inventory task is dropped on reveal');
    denyProjectItemGet = false;
    pane = currentTab(page);
    await page.waitForFunction(() => {
      const more = document.querySelector('.rw-record:not([hidden]) [data-project-more]');
      return more && !more.hidden && !more.disabled && more.textContent === 'More';
    });
    // A denial while paging invalidates the previously readable page too; Retry restores it.
    denyProjectItems = true;
    await pane.locator('[data-project-more]').click();
    await pane.locator('[data-project-error]:not([hidden])').waitFor();
    assert.equal(await pane.locator('.rw-project-row').count(), 0, 'denied paging retains no stale rows');
    denyProjectItems = false;
    await pane.locator('[data-project-error]').getByRole('button', {name: 'Retry'}).click();
    await pane.locator('.rw-project-row').first().waitFor();
    // More work pages the same section.
    await pane.locator('[data-project-more]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Second page work'}).waitFor();
    // Closed work and archived history are separate opt-ins on the request.
    await pane.locator('[data-project-closed]').click();
    await until(() => inventoryCalls.at(-1).args.include_closed === true, 'closed-work inventory read');
    await pane.locator('[data-project-archived]').click();
    await until(() => inventoryCalls.at(-1).args.include_archived === true, 'archived inventory read');
    // The toggle is a real boolean: the second click requests false and the UI says false.
    await pane.locator('[data-project-archived]').click();
    await until(() => inventoryCalls.at(-1).args.include_archived === false, 'archived history disabled');
    assert.equal(await pane.locator('[data-project-archived]').getAttribute('aria-pressed'), 'false');
    // Other families load lazily and keep their own state.
    assert.equal(inventoryCalls.some(call => call.args.section === 'signoffs'), false);
    await pane.locator('[data-project-section="signoffs"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Send the launch announcement'}).waitFor();
    assert.equal(inventoryCalls.at(-1).args.section, 'signoffs');
    assert.equal(await pane.locator('.rw-project-row .rw-project-scope').first().textContent(), 'Global');
    assert.equal(await pane.locator('.rw-project-row .rw-project-unlink').count(), 1);
    // A threadable inventory row opens the shared workspace on Chat; Back restores this exact section.
    await pane.locator('.rw-project-row').filter({hasText: 'Send the launch announcement'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Send the launch announcement'}).waitFor();
    assert.equal(await pane.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true', 'a Project signoff keeps its private Chat');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    assert.equal(await pane.locator('[data-project-section="signoffs"]').getAttribute('aria-pressed'), 'true');
    await pane.locator('[data-project-section="documents"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Campaign brief'}).waitFor();
    assert.equal(inventoryCalls.at(-1).args.section, 'documents');
    // A document without a private channel opens its authorized reader in the same pane and has no Chat tab.
    await pane.locator('.rw-project-row').filter({hasText: 'Campaign brief'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-reader h3').filter({hasText: 'Campaign brief'}).waitFor();
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 1, 'non-threadable inventory records never promise Chat');
    assert.match(await pane.locator('.work-source').first().textContent(), /authorized read model body/);
    assert.match(await pane.locator('.work-source').first().textContent(), /fuller second paragraph/);
    assert.equal(await pane.getByRole('button', {name: /read more/i}).count(), 0, 'a complete short document has no continuation control');
    await pane.locator('[data-record-complete]').waitFor();
    if (shots) await pane.screenshot({path: path.join(shots, 'project-document-desktop.png')});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    assert.equal(await pane.locator('[data-project-section="documents"]').getAttribute('aria-pressed'), 'true', 'Back restores the Project dashboard section');
    // An ordinary Context document uses the same canonical reader in this pane, never a modal.
    await page.evaluate(() => _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'context-doc', business_id: 'project-a',
      resource_type: 'documents'}, label: 'Context document'}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('.work-source').filter({hasText: 'Authorized note text'}).waitFor();
    assert.equal(await page.locator('.modal').count(), 0, 'a Context document opens in the record pane');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    // A Project action uses its nested authorized envelope and never inherits another scope.
    await pane.locator('[data-project-section="actions"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Published the first page'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-action-text').filter({hasText: 'Canonical action text'}).waitFor();
    assert.equal(await page.locator('.modal').count(), 0);
    if (shots) await pane.screenshot({path: path.join(shots, 'project-action-desktop.png')});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    // A Project table reads through the same authorized model and hands rows to the existing table reader.
    await pane.locator('[data-project-section="tables"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Campaign calendar'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-reader h3').filter({hasText: 'Campaign calendar'}).waitFor();
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 1, 'a table has no private Chat');
    assert.equal(await pane.getByRole('button', {name: 'Open table', exact: true}).count(), 0, 'the table reader stays in this pane');
    await pane.locator('.rw-table').waitFor();
    assert.match(await pane.locator('.rw-table').textContent(), /Channel.*Date.*Web.*2026-11-01/);
    if (shots) await pane.screenshot({path: path.join(shots, 'project-table-desktop.png')});
    assert.equal(itemGetCalls.at(-1).args.project_id, 'project-a');
    assert.deepEqual(itemGetCalls.at(-1).args.item, {type: 'table', id: 'table-1', business_id: 'project-a'});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    assert.equal(await pane.locator('[data-project-section="tables"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await pane.locator('.rw-project-row').filter({hasText: 'Campaign calendar'}).count(), 1, 'Back restores the tables inventory');

    // Link an existing item: scope, kind, search, candidates, uncertain retry keeps the key, then unlink.
    await pane.locator('[data-project-link]').click();
    const dialog = page.locator('.modal');
    await dialog.waitFor();
    assert.equal(await dialog.locator('[data-link-scope]').inputValue(), 'project-a', 'the Project scope is offered first');
    assert.match(await dialog.locator('[data-link-scope]').textContent(), /Global[\s\S]*Alpha Studio[\s\S]*Beta Lab[\s\S]*Gamma Works[\s\S]*Old Project \(archived\)/);
    await dialog.locator('[data-link-type]').selectOption('material');
    await until(() => candidateCalls.some(call => call.args.type === 'material' && call.args.source_business_id === 'project-a'), 'canonical material candidates');
    await dialog.locator('[data-link-type]').selectOption('task');
    await dialog.locator('[data-link-search]').fill('Global candidate');
    await until(() => candidateCalls.some(call => call.args.type === 'task' && call.args.source_business_id === 'project-a' && call.args.query === 'Global candidate'), 'searched task candidates');
    await dialog.locator('[data-link-scope]').selectOption('');
    await until(() => candidateCalls.at(-1).args.source_business_id === null, 'Global candidate scope');
    linkFails = 1;
    const candidate = dialog.locator('.rw-link-option').first();
    await candidate.click();
    await dialog.locator('[data-link-error]:not([hidden])').waitFor();
    assert.match(await dialog.locator('[data-link-error]').textContent(), /Couldn't|Try again/);
    await candidate.click();
    await until(() => linkCalls.length >= 2, 'the linked candidate retry');
    assert.equal(linkCalls[0].args.idempotency_key, linkCalls[1].args.idempotency_key, 'an uncertain link retry reuses its exact request key');
    await dialog.waitFor({state: 'detached'});
    await until(() => inventoryCalls.filter(call => call.args.section === 'tables').length >= 2, 'inventory refresh after link');
    // Unlink only the explicit link, with its revision.
    await pane.locator('[data-project-section="signoffs"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Send the launch announcement'}).locator('.rw-project-unlink').click();
    await until(() => unlinkCalls.length === 1, 'unlink request');
    assert.equal(unlinkCalls[0].args.scope, 'global');
    assert.equal(unlinkCalls[0].args.project_id, 'project-a');
    assert.equal(unlinkCalls[0].args.link_id, 'link-signoff');
    assert.equal(unlinkCalls[0].args.expected_revision, 3);
    assert.ok(unlinkCalls[0].args.idempotency_key);
    // Row navigation pushes the target and Back restores this dashboard's exact section and scroll.
    await pane.locator('[data-project-section="work"]').click();
    await pane.locator('.rw-project-row').first().locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Top-level Alpha work'}).waitFor();
    assert.equal(await pane.locator('.rw-back').isVisible(), true);
    assert.equal(await pane.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    assert.equal(await pane.locator('.rw-title').textContent(), 'Alpha Studio');
    assert.equal(await pane.locator('[data-project-section="work"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await pane.locator('.rw-project-row').count(), 4);
    if (shots) {
      await page.waitForFunction(() => {
        const more = document.querySelector('.rw-record:not([hidden]) .rw-project-more');
        return !more || more.hidden || more.textContent === 'More';
      });
      await pane.screenshot({path: path.join(shots, 'project-dashboard-desktop.png')});
    }

    // ---- Routine inventory: the routine itself is the item; its history is independently paged.
    await pane.locator('[data-project-section="automation"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Weekly digest'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-routine h3').filter({hasText: 'Weekly digest'}).waitFor();
    await pane.locator('.rw-run-row').first().waitFor();
    assert.equal(await pane.locator('.rw-run-row').count(), 2, 'the first run page is bounded');
    assert.equal(routineRunCalls[0].path, '/api/biz/alpha/operations/routines_runs', 'history uses the routine source scope');
    assert.equal(routineRunCalls[0].args.routine_id, 'routine-1');
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 1, 'a routine has no private Chat');
    await pane.locator('[data-routine-more]').click();
    await page.waitForFunction(() => document.querySelectorAll('.rw-run-row').length === 3);
    assert.equal(routineRunCalls[1].args.cursor, 'runs-page-2', 'More uses the history cursor');
    // A structured run job opens through jobs_get, never through an inventory enumeration.
    if (shots) await pane.screenshot({path: path.join(shots, 'project-routine-desktop.png')});
    await pane.locator('.rw-run-job').first().click();
    pane = currentTab(page);
    await pane.locator('.rw-job h3').waitFor();
    assert.equal(jobGetCalls.at(-1).path, '/api/biz/alpha/operations/jobs_get');
    assert.equal(jobGetCalls.at(-1).args.job_id, 'job-1');
    await pane.locator('.rw-reader').getByText(/Full output is only available/).waitFor();
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-routine h3').filter({hasText: 'Weekly digest'}).waitFor();
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    assert.equal(await pane.locator('[data-project-section="automation"]').getAttribute('aria-pressed'), 'true');
    // A linked Global routine keeps its own scope and history route.
    await pane.locator('.rw-project-row').filter({hasText: 'Global digest'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-routine h3').filter({hasText: 'Global digest'}).waitFor();
    await pane.locator('.rw-run-row').first().waitFor();
    assert.equal(await pane.locator('.rw-run-row').count() > 0, true);
    assert.equal(routineRunCalls.at(-1).path, '/api/root/operations/routines_runs');
    assert.equal(routineRunCalls.at(-1).args.scope, 'global');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // ---- Reminder inventory: safe text/schedule/state only, no scheduling controls or Chat.
    await pane.locator('[data-project-section="other"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Follow up Friday'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-reminder-text').filter({hasText: 'Follow up Friday'}).waitFor();
    assert.match(await pane.locator('.rw-reminder-meta').textContent(), /Scheduled for:.*State: queued/);
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 1, 'a reminder has no private Chat');
    assert.equal(await pane.locator('button', {hasText: /schedule|cancel|delete/i}).count(), 0, 'reminder scheduling stays outside this reader');
    if (shots) await pane.screenshot({path: path.join(shots, 'project-reminder-desktop.png')});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    assert.equal(await pane.locator('[data-project-section="other"]').getAttribute('aria-pressed'), 'true');
    // A Dev report is the canonical owner {ticket} record: whitelisted fields, full details
    // paragraphs and optional resolution, with no invented Chat or raw JSON.
    await pane.locator('.rw-project-row').filter({hasText: 'Checkout retry loop'}).locator('.rw-project-open').click();
    pane = currentTab(page);
    await pane.locator('.rw-ticket h3').filter({hasText: 'Checkout retry loop'}).waitFor();
    assert.match(await pane.locator('.rw-ticket-meta').textContent(), /Bug.*Closed/);
    assert.match(await pane.locator('.rw-ticket-fields').textContent(), /Created.*Updated/s);
    assert.equal(await pane.locator('.rw-ticket > .rw-ticket-body p').count(), 2, 'both details paragraphs render');
    assert.match(await pane.locator('.rw-ticket').textContent(), /Replaced the retry guard/);
    assert.equal(await pane.locator('.rw-ticket-section h4').filter({hasText: /Verification|Deployment/}).count(), 0,
      'optional fields absent from this canonical payload are not inferred');
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 1, 'a Dev report has no private Chat');
    assert.doesNotMatch(await pane.locator('.rw-ticket').textContent(), /reporter_id|business_id|\{.*\}/);
    if (shots) await pane.screenshot({path: path.join(shots, 'project-ticket-desktop.png')});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // Standalone owner Dev reference: exact narrow root selector, explicit Global scope, Back.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({
      source: {type: 'dev_ticket', id: 'ticket-global', business_id: null}, label: 'Nightly export delay'})));
    pane = currentTab(page);
    await pane.locator('.rw-ticket h3').filter({hasText: 'Nightly export delay'}).waitFor();
    assert.deepEqual(devTicketCalls.at(-1).args, {ticket_id: 'ticket-global', status: 'all', details: true},
      'standalone Dev reference uses the exact owner selector');
    assert.equal(await pane.locator('.rw-ticket > .rw-ticket-body p').count(), 2);
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').count(), 1);
    assert.equal(await pane.locator('.rw-ticket-section h4').filter({hasText: 'Resolution'}).count(), 0, 'optional resolution is absent in this payload');
    assert.doesNotMatch(await pane.locator('.rw-ticket').textContent(), /reporter_id|business_id/);
    if (shots) await pane.screenshot({path: path.join(shots, 'dev-ticket-desktop.png')});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // A foreign-scope ticket is accepted only when its canonical business id matches the reference.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({
      source: {type: 'dev_ticket', id: 'ticket-beta', business_id: 'project-b', business_slug: 'beta'},
      label: 'Beta report'}, {biz: 'beta'})));
    pane = currentTab(page);
    await pane.locator('.rw-ticket h3').filter({hasText: 'Beta report'}).waitFor();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.business_id), 'project-b');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // A returned ticket from another scope is unavailable, never accepted because the id matches.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({
      source: {type: 'dev_ticket', id: 'ticket-wrong', business_id: null}, label: 'Wrong scope'})));
    await page.waitForFunction(() => _agpad.focusWorkspace.current()?.source.type === 'project');
    assert.equal(await page.locator('.rw-ticket').count(), 0, 'scope mismatch never paints another ticket');
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // An owner Dev reference with a foreign id-only scope still reads through root and checks
    // the returned canonical business_id; it is not rejected merely because no slug is loaded.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({
      source: {type: 'dev_ticket', id: 'ticket-unknown-scope', business_id: 'scope-without-slug'}, label: 'Unknown scope report'})));
    pane = currentTab(page);
    await pane.locator('.rw-ticket h3').filter({hasText: 'Unknown scope report'}).waitFor();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.business_id), 'scope-without-slug');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // Hidden revocation: the retained ticket is denied while another record is on top; Back
    // disposes it and lands on the project, not on stale report content.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({
      source: {type: 'dev_ticket', id: 'ticket-global', business_id: null}, label: 'Nightly export delay'})));
    pane = currentTab(page);
    await pane.locator('.rw-ticket h3').filter({hasText: 'Nightly export delay'}).waitFor();
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({
      source: {type: 'document', id: 'after-ticket', business_id: null}, label: 'Other'})));
    pane = currentTab(page);
    await pane.locator('.record-context-document').waitFor();
    denyDevTicketRead = true;
    await page.evaluate(async () => await _agpad.focusWorkspace.back());
    await page.waitForFunction(() => _agpad.focusWorkspace.current()?.source.type === 'project');
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.id), 'project-a', 'a denied hidden ticket is removed');
    assert.equal(await page.locator('.rw-ticket').count(), 0);
    denyDevTicketRead = false;
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // ---- Project document pages: the top-level context_get continuation envelope is
    // preserved and the existing bounded reader exposes Read more; the typed source id is
    // sent, not the workspace descriptor. Source changes/denials stay in that lifecycle.
    const pagedItem = {type: 'material', id: 'manager-paged-doc', business_id: 'project-a', business_slug: 'alpha',
      label: 'Paged source', resource_type: 'documents', can_thread: false};
    const firstChunk = 'CANONICAL FIRST CHUNK';
    const itemRoute = '**/operations/project_item_get', contextRoute = '**/operations/context_get';
    const continuationCalls = [];
    let continuationMode = 'success';
    await page.route(itemRoute, route => route.fulfill(routeOk({item: pagedItem, record: {document: {
      id: pagedItem.id, title: 'Paged source', kind: 'context_note', revision: 1, body: firstChunk,
      character_offset: 0, content_hash: 'version-a'}, complete: false, next_cursor: 'canonical-page-2'}})));
    await page.route(contextRoute, async route => {
      const args = route.request().postDataJSON();
      continuationCalls.push(args);
      if (continuationMode === 'deny') return route.fulfill(routeError('forbidden', 'Continuation denied'));
      const changed = continuationMode === 'changed';
      return route.fulfill(routeOk({document: {id: pagedItem.id, kind: 'context_note', revision: 1,
        body: 'CANONICAL SECOND CHUNK', character_offset: changed ? 1 : firstChunk.length,
        content_hash: changed ? 'version-b' : 'version-a'}, complete: true, next_cursor: null}));
    });
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromProjectItem(
      {type: 'material', id: 'manager-paged-doc', business_id: 'project-a', business_slug: 'alpha', label: 'Paged source',
        resource_type: 'documents', can_thread: false}, {id: 'project-a'})));
    pane = currentTab(page);
    await pane.getByText(firstChunk, {exact: true}).waitFor();
    assert.equal(await pane.getByRole('button', {name: /read more|more content/i}).count(), 1,
      'a non-complete Project document exposes its existing continuation control');
    await pane.getByRole('button', {name: /read more|more content/i}).click();
    await pane.getByText('CANONICAL SECOND CHUNK').waitFor();
    assert.deepEqual({id: continuationCalls.at(-1).document_id, scope: continuationCalls.at(-1).scope,
      cursor: continuationCalls.at(-1).cursor}, {id: 'manager-paged-doc', scope: 'business', cursor: 'canonical-page-2'});
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    continuationMode = 'changed';
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromProjectItem(
      {type: 'material', id: 'manager-paged-doc', business_id: 'project-a', business_slug: 'alpha', label: 'Paged source',
        resource_type: 'documents', can_thread: false}, {id: 'project-a'})));
    pane = currentTab(page);
    await pane.getByText(firstChunk, {exact: true}).waitFor();
    await pane.getByRole('button', {name: /read more|more content/i}).click();
    await pane.locator('[data-record-changed]').waitFor();
    await pane.getByRole('button', {name: 'Read from the start'}).waitFor();
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    continuationMode = 'deny';
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromProjectItem(
      {type: 'material', id: 'manager-paged-doc', business_id: 'project-a', business_slug: 'alpha', label: 'Paged source',
        resource_type: 'documents', can_thread: false}, {id: 'project-a'})));
    pane = currentTab(page);
    await pane.getByText(firstChunk, {exact: true}).waitFor();
    await pane.getByRole('button', {name: /read more|more content/i}).click();
    await page.waitForFunction(() => _agpad.focusWorkspace.current()?.source.id === 'project-a');
    assert.equal(await page.locator('.record-context-document').count(), 0, 'a denied continuation removes the retained document entry');
    await page.unroute(contextRoute);
    await page.unroute(itemRoute);
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();

    // ---- Project table rows: initial and continuation failures both retain a real Retry
    // with the same table/scope; append Retry resumes the same cursor without duplicates.
    const retryTable = {type: 'table', id: 'table-rows-retry', business_id: 'project-a', business_slug: 'alpha',
      label: 'Retry table', resource_type: 'data_tables', can_thread: false};
    const tableItemRoute = '**/operations/project_item_get', rowsRoute = '**/operations/tables_rows_list';
    const rowCalls = [];
    let rowPhase = 'initial-fail';
    await page.route(tableItemRoute, route => route.fulfill(routeOk({item: retryTable, record: {table: {
      id: retryTable.id, title: 'Retry table', revision: 1,
      columns: [{id: 'channel', name: 'Channel'}, {id: 'date', name: 'Date'}]}}})));
    await page.route(rowsRoute, async route => {
      const args = route.request().postDataJSON();
      rowCalls.push(args);
      if (rowPhase === 'initial-fail') { rowPhase = 'initial-ok'; return route.fulfill(routeError('temporarily_unavailable', 'Rows unavailable', 503)); }
      if (rowPhase === 'initial-ok') { rowPhase = 'append-fail'; return route.fulfill(routeOk({table_revision: 1,
        items: [{id: 'row-1', position: 0, cells: {channel: 'Web', date: '2026-11-01'}}], next_cursor: 'rows-page-2'})); }
      if (rowPhase === 'append-fail') { rowPhase = 'append-ok'; return route.fulfill(routeError('temporarily_unavailable', 'Append unavailable', 503)); }
      return route.fulfill(routeOk({table_revision: 1,
        items: [{id: 'row-2', position: 1, cells: {channel: 'Email', date: '2026-11-02'}}], next_cursor: null}));
    });
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromProjectItem(
      {type: 'table', id: 'table-rows-retry', business_id: 'project-a', business_slug: 'alpha', label: 'Retry table',
        resource_type: 'data_tables', can_thread: false}, {id: 'project-a'})));
    pane = currentTab(page);
    await pane.getByRole('button', {name: 'Retry', exact: true}).waitFor();
    assert.equal(rowCalls.length, 1, 'the initial row failure is not swallowed as a loaded empty table');
    assert.equal(rowCalls[0].table_id, 'table-rows-retry');
    assert.equal(rowCalls[0].cursor, undefined);
    await pane.getByRole('button', {name: 'Retry', exact: true}).click();
    await pane.getByText('Web', {exact: true}).waitFor();
    assert.equal(await pane.getByRole('row').count(), 2, 'header plus the first row');
    await pane.getByRole('button', {name: 'More rows'}).click();
    await pane.getByRole('button', {name: 'Retry', exact: true}).waitFor();
    assert.equal(await pane.getByText('Web', {exact: true}).count(), 1, 'a failed append keeps the authorized row');
    await pane.getByRole('button', {name: 'Retry', exact: true}).click();
    await pane.getByText('Email', {exact: true}).waitFor();
    assert.equal(await pane.getByText('Web', {exact: true}).count(), 1, 'append Retry does not duplicate the first row');
    assert.equal(await pane.getByText('Email', {exact: true}).count(), 1);
    assert.equal(rowCalls.at(-1).cursor, 'rows-page-2');
    // A successful Project-item revalidation must reach the nested table controller, not
    // stop at an undefined guard result.
    const beforeOnShow = rowCalls.length;
    await page.evaluate(async () => await _agpad.focusWorkspace.entry().dashboard.onShow());
    await until(() => rowCalls.length > beforeOnShow, 'nested table onShow rows');
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.rw-project-name').waitFor();
    await page.unroute(rowsRoute);
    await page.unroute(tableItemRoute);

    // ---- One inventory authorization denial invalidates every cached Project section, not
    // just the active one. A held response from another section cannot reveal its old row.
    await pane.locator('[data-project-section="tables"]').click();
    await pane.getByRole('button', {name: 'Campaign calendar', exact: true}).waitFor();
    await pane.locator('[data-project-section="work"]').click();
    await pane.locator('.rw-project-row').filter({hasText: 'Top-level Alpha work'}).locator('.rw-project-open').waitFor();
    const invRoute = '**/operations/project_items_list';
    let releaseTable = null, tableStarted;
    const tableStartedPromise = new Promise(resolve => { tableStarted = resolve; });
    await page.route(invRoute, async route => {
      const args = route.request().postDataJSON();
      if (args.section === 'tables') { tableStarted?.(); await new Promise(resolve => { releaseTable = resolve; }); }
      return route.fulfill(routeError('forbidden', 'Project inventory permission revoked'));
    });
    await pane.locator('[data-project-section="tables"]').click();
    await tableStartedPromise;
    await pane.locator('[data-project-section="work"]').click();
    await pane.getByText('This project inventory is unavailable.', {exact: true}).waitFor();
    assert.equal(await pane.getByRole('button', {name: 'Campaign calendar', exact: true}).count(), 0,
      'the other cached section was cleared before its held response returned');
    releaseTable?.();
    await page.waitForTimeout(100);
    assert.equal(await pane.getByRole('button', {name: 'Campaign calendar', exact: true}).count(), 0,
      'the held response cannot resurrect a cleared section');
    await page.unroute(invRoute);
    await pane.locator('[data-project-error]:not([hidden])').getByRole('button', {name: 'Retry', exact: true}).click();
    await pane.locator('.rw-project-row').filter({hasText: 'Top-level Alpha work'}).locator('.rw-project-open').waitFor();
    assert.equal(await pane.locator('.rw-project-name').textContent(), 'Alpha Studio');

    // ---- Add existing Project to Focus: explicit presentation only, real directory and archived state.
    assert.equal(focusPresentCalls.length, 0, 'reading Projects never pins one');
    await pane.locator('.rw-close').click();
    await page.locator('[data-focus-add]').click();
    const add = page.locator('.modal');
    await add.waitFor();
    await add.locator('[name="kind"][value="project"]').check();
    assert.equal(await add.locator('[data-add-project]').isVisible(), true);
    const options = await add.locator('[name="project"] option').evaluateAll(nodes => nodes.map(node => node.textContent));
    assert.deepEqual(options, ['Alpha Studio', 'Beta Lab', 'Gamma Works', 'Old Project (archived)']);
    await add.locator('[name="project"]').selectOption('project-c');
    await add.getByRole('button', {name: 'Add'}).click();
    await add.waitFor({state: 'detached'});
    await until(() => focusPresentCalls.length === 1, 'focus_present');
    assert.deepEqual(focusPresentCalls[0].args.source, {type: 'project', id: 'project-c', business_id: 'project-c'});
    await page.locator('.agfocus-items [data-card-id="project-c"]').waitFor();
    assert.equal(await page.locator('[data-card-id="project-c"] .ttl').textContent(), 'Gamma Works', 'the real Project name wins over the presentation-only name');
    assert.equal(await page.locator('[data-card-id="project-c"] .agfocus-scope').isVisible(), false);

    // ---- Scoped Project expansion: distinct rows for duplicate ids, scope labels and no forced pin.
    const projectAWork = projectWorkCalls.filter(call => call.project_id === 'project-a').length;
    await page.locator('[data-card-id="project-a"] .agfocus-project-toggle').click();
    await page.locator('.agfocus-items [data-card-id="project-a/work/task:project-a:dup"]').waitFor();
    assert.equal(projectWorkCalls.filter(call => call.project_id === 'project-a').length, projectAWork + 1);
    assert.equal(projectWorkCalls.at(-1).root_tasks_only, true);
    assert.equal(projectWorkCalls.at(-1).section, 'work');
    assert.equal(await page.locator('[data-card-id="project-a/work/task:project-a:dup"]').count(), 1, 'duplicate inventory rows collapse to one scoped row');
    assert.equal(await page.locator('[data-card-id="project-a/work/task:global:foreign-task"] .agfocus-scope').isVisible(), true, 'linked global work keeps its scope label');
    const betaScope = page.locator('[data-card-id="project-a/work/task:project-b:foreign-beta"] .agfocus-scope');
    await betaScope.waitFor();
    assert.equal(await betaScope.isVisible(), true, 'foreign Project work keeps its Beta Lab label');
    assert.equal(await betaScope.textContent(), 'Beta Lab');
    assert.equal(await page.locator('[data-card-id="project-a/work/task:global:foreign-task"]').evaluate(el => el._item._workBusinessId), 'project-a');
    assert.equal(await page.locator('[data-card-id="project-a/work/task:project-a:dup"] .agfocus-scope').isVisible(), false, 'same-scope work omits the repeated badge');
    assert.equal(await page.locator('.agfocus-items [data-card-id="project-a/work/task:project-a:archived-task"]').count(), 0, 'archived derived work follows the board filter');
    await page.locator('.agfocus-options > summary').click();
    await page.locator('[data-focus-archived]').click();
    await page.locator('.agfocus-items [data-card-id="project-a/work/task:project-a:archived-task"]').waitFor();
    await page.locator('[data-focus-archived]').click();
    await page.waitForFunction(() => !document.querySelector('.agfocus-items [data-card-id="project-a/work/task:project-a:archived-task"]'));
    await page.locator('.agfocus-options > summary').click();
    const projectBToggle = page.locator('[data-card-id="project-b"] .agfocus-project-toggle');
    if (await projectBToggle.getAttribute('aria-expanded') !== 'true') await projectBToggle.click();
    await page.locator('.agfocus-items [data-card-id="project-b/work/task:project-b:dup"]').waitFor();
    assert.equal(await page.locator('[data-card-id="project-a/work/task:project-a:dup"]').count(), 1, 'the other Project keeps its own row');
    assert.equal(await page.locator('[data-card-id="project-a/work/task:project-a:dup"]').count(), 1);
    assert.equal(await page.locator('[data-card-id="project-b/work/task:project-b:dup"]').count(), 1);
    if (shots) await page.screenshot({path: path.join(shots, 'focus-project-expanded-desktop.png')});
    // Following derived work pushes its record in the workspace; Back returns to the Project workspace.
    await page.locator('[data-card-id="project-a"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Alpha Studio'}).waitFor();
    await page.locator('[data-card-id="project-a/work/task:global:foreign-task"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Global follow-up'}).waitFor();
    assert.equal(await pane.locator('.rw-back').isVisible(), true);
    await pane.locator('.rw-back').click();
    await pane.locator('.rw-title').filter({hasText: 'Alpha Studio'}).waitFor();
    await pane.locator('.rw-close').click();

    // ---- Focus reselection: same controller, shared source drafts and visible Chat.
    await page.locator('.ttab[data-v="focus"]').click();
    assert.equal(await page.locator('.agthreads-details,[data-manage]').count(), 0);
    await page.locator('[data-card-id="story-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await pane.locator('.rw-title').textContent(), 'A very long Focus-only Story name the card shows', 'Focus retains its presentation label');
    assert.equal(await pane.locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true');
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Unsent Story note', 'the same scoped Chat draft survives Focus reselection');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').filter({hasText: 'A conflicting edit that must be kept'}).waitFor();
    await page.locator('[data-card-id="project-a"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Alpha Studio'}).waitFor();
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), '', 'another scoped source starts with its own draft');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus .agfocus-dock').hidden);
    await page.locator('[data-card-id="story-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Unsent Story note', 'drafts survive closing and reselecting');

    // ---- Revocation: a denied source leaves no stale private content; a late read cannot recreate it.
    await pane.getByText('A private message about story-1', {exact: true}).waitFor();
    denyConversationSource = 'task:project-a:story-1';
    await page.evaluate(() => document.querySelector('.agfocus .agfocus-dock [data-rw-tab="chat"]').click());
    await page.evaluate(() => _agpad.focusWorkspace.refresh());
    await page.waitForFunction(() => document.querySelector('.agfocus .agfocus-dock').hidden);
    assert.equal(await page.locator('.rw-title').count(), 0, 'the disposed record leaves no header');
    assert.equal(await page.locator('.rw-conversation').count(), 0);
    denyConversationSource = null;
    await page.locator('.ttab[data-v="status"]').click();
    await page.locator('.ttab[data-v="focus"]').click();
    await page.locator('[data-card-id="project-a"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.rw-project-name').waitFor();
    // A pending inventory read that resolves after the retained Project entry is denied must
    // neither repaint denied content nor recreate a closed pane. The denial is not a manual close.
    holdInventory = {section: 'other'};
    const started = new Promise(resolve => { holdInventory.started = resolve; });
    await page.locator('[data-project-section="other"]').click();
    await started;
    await page.evaluate(async () => {
      window.managerProject = _agpad.focusWorkspace.entry();
      await _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'after-project-revoked', business_id: null}, label: 'Other'}));
    });
    denyConversationSource = 'project:project-a:project-a';
    await page.evaluate(() => window.managerProject.chat.refresh());
    await page.waitForFunction(() => window.managerProject.alive === false);
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.current()?.source.id), 'after-project-revoked');
    holdInventory.release?.();
    holdInventory = null;
    await page.waitForTimeout(200);
    assert.equal(await page.locator('.agfocus-dock .rw-project-row').count(), 0, 'a late Project read never repaints denied content');
    assert.equal(await page.locator('.rw-project-name').count(), 0);
    denyConversationSource = null;
    await currentTab(page).locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // ---- Task subtype conversion: the successful canonical write adopts the existing Story
    // dashboard in place, preserving the source identity, Chat instance/draft, selected tab and
    // stack. The same path is exercised for a Global Task. An external type change while a
    // local overview is dirty must not replace that editor.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'convert-me',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Convert business work', task_type: 'task', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').fill('Business conversion draft');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.tdetail').waitFor();
    await pane.locator('[data-task-type]').selectOption('story');
    await pane.locator('[data-task-overview]').fill('Converted business story overview');
    await pane.locator('[data-save-type]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('.record-overview').count(), 1, 'the adopted Story dashboard renders its overview');
    assert.equal(await pane.locator('.tdetail').count(), 0, 'the old Task editor is disposed');
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    if (shots) await pane.screenshot({path: path.join(shots, 'task-conversion-desktop.png')});
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Business conversion draft',
      'the same Chat instance and draft survive conversion');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'global-convert',
      business_id: null}, label: 'Convert global work', task_type: 'task', can_thread: true})));
    pane = currentTab(page);
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').fill('Global conversion draft');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('[data-editor]').waitFor();
    await pane.locator('[data-task-type]').selectOption('story');
    await pane.locator('[data-task-overview]').fill('Converted global story overview');
    await pane.locator('[data-save]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('.tdetail').count(), 0, 'the Global Task editor is disposed after conversion');
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Global conversion draft',
      'the Global conversion keeps the same Chat instance/draft');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // A failed canonical conversion keeps the editor, the chosen type and the typed overview;
    // only a successful retry may replace the dashboard.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'failed-convert',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Failed conversion work', task_type: 'task', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.tdetail').waitFor();
    await pane.locator('[data-task-type]').selectOption('story');
    await pane.locator('[data-task-overview]').fill('Failed conversion keeps this draft');
    failConversionSave = true;
    await pane.locator('[data-save-type]').click();
    await pane.locator('#td-error:not([hidden])').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Task dashboard', 'a failed save does not adopt the type');
    assert.equal(await pane.locator('.tdetail').count(), 1);
    assert.equal(await pane.locator('[data-task-type]').inputValue(), 'story', 'the chosen type remains in the failed editor');
    assert.equal(await pane.locator('[data-task-overview]').inputValue(), 'Failed conversion keeps this draft');
    failConversionSave = false;
    await pane.locator('[data-save-type]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // A clean source revalidation adopts the external type immediately on reveal.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'external-clean',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Clean external conversion', task_type: 'task', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.tdetail').waitFor();
    externalCleanTask = {...externalCleanTask, task_type: 'story', overview: 'External clean overview', revision: 2};
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    assert.equal(await pane.locator('.tdetail').count(), 0);
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // The same dirty external-change guard applies to the Global Task controller.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'external-global',
      business_id: null}, label: 'Global externally converted work', task_type: 'task', can_thread: true})));
    pane = currentTab(page);
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').fill('Global external draft');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('[data-editor]').waitFor();
    await pane.locator('[data-task-overview]').fill('Global local overview that must survive');
    externalGlobalTask = {...externalGlobalTask, task_type: 'story', overview: 'External Global overview', revision: 2};
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('[data-global-task-status]').filter({hasText: /changed type|Save or discard/i}).waitFor();
    assert.equal(await pane.locator('[data-editor]').count(), 1, 'the dirty Global editor remains');
    assert.equal(await pane.locator('.record-overview').count(), 0);
    assert.equal(await pane.locator('[data-task-overview]').inputValue(), 'Global local overview that must survive');
    assert.equal(await pane.locator('[data-reload]:visible').count(), 1, 'the Global changed notice exposes its reload control');
    await pane.locator('[data-reload]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard');
    assert.equal(await pane.locator('[data-editor]').count(), 0);
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').inputValue(), 'Global external draft',
      'explicit Global reload retains the separate Chat draft');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // Fresh descriptors with absent/stale task_type take the canonical subtype from tasks_get.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'fresh-business',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Fresh business source', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard',
      'a missing descriptor task_type is normalized from the first canonical read');
    assert.equal(await page.locator('.rw-record:not([hidden])').count(), 1);
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'customer-1',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Customer source', task_type: 'task', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('[data-customer-name]').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Customer dashboard',
      'a stale business descriptor takes the canonical Customer subtype');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'fresh-global',
      business_id: null, task_type: 'task'}, label: 'Fresh global source', task_type: 'task', can_thread: true})));
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await pane.locator('[data-rw-tab="dashboard"]').getAttribute('aria-label'), 'Story dashboard',
      'a stale descriptor task_type is normalized from the first canonical read');
    assert.equal(await page.locator('.rw-record:not([hidden])').count(), 1);
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // An external type change must not destroy a locally dirty overview.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    await page.evaluate(async () => await _agpad.focusWorkspace.open(rwRecordFromItem({source: {type: 'task', id: 'external-convert',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Externally converted work', task_type: 'task', can_thread: true}, {biz: 'alpha'})));
    pane = currentTab(page);
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').fill('External conversion draft');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('[data-task-overview]').fill('Local overview that must survive the external change');
    externalConversionTask = {...externalConversionTask, task_type: 'story', overview: 'Canonical external overview', revision: 2};
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('#td-error').filter({hasText: /changed type|Save or discard/i}).waitFor();
    assert.equal(await pane.locator('.record-overview').count(), 0, 'a dirty Task editor is not replaced by the external Story');
    assert.equal(await pane.locator('.tdetail').count(), 1, 'the dirty editor and its base revision remain');
    assert.equal(await pane.locator('[data-task-overview]').inputValue(), 'Local overview that must survive the external change');
    assert.equal(await pane.locator('#td-reload:visible').count(), 1, 'the changed notice exposes the explicit reload control');
    // The same guard runs on the Back return path, not only on a tab switch.
    await page.evaluate(async () => await _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'action', id: 'action-1',
      business_id: 'project-a', business_slug: 'alpha'}, label: 'Published the first page'})));
    pane = currentTab(page);
    await pane.locator('.rw-back').click();
    pane = currentTab(page);
    await pane.locator('.tdetail').waitFor();
    await pane.locator('#td-error').filter({hasText: /changed type|Save or discard/i}).waitFor();
    assert.equal(await pane.locator('[data-task-overview]').inputValue(), 'Local overview that must survive the external change');
    assert.equal(await pane.locator('.record-overview').count(), 0, 'Back revalidation also keeps the dirty Task editor');
    await pane.locator('.rw-close').click();
    await page.locator('.modal').waitFor();
    await page.locator('.modal').getByRole('button', {name: 'Discard'}).click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);

    // ---- Signoff dashboard: the existing canonical card and exactly one composer for its own channel.
    // The Focus board hands pending Signoffs to the shared attention queue, so a Signoff record is
    // selected through its Focus Signoff queue chip.
    await page.locator('.ttab[data-v="focus"]').click();
    await page.locator('[data-signoff-key="signoff-1"]').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Send the launch announcement'}).waitFor();
    assert.deepEqual({type: readGets.at(-1).source.type, id: readGets.at(-1).source.id,
      business_id: readGets.at(-1).source.business_id ?? null}, {type: 'signoff', id: 'signoff-1', business_id: null},
      'the canonical Signoff source scope is Global');
    assert.equal(await pane.locator('.rw-tabs [role="tab"]').last().getAttribute('aria-label'), 'Chat');
    assert.equal(await pane.locator('.rw-conversation .conversation-inline-compose textarea').count(), 1);
    if (shots) await pane.screenshot({path: path.join(shots, 'signoff-chat-desktop.png')});
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.rw-dashboard .sg-row[data-id="signoff-1"]').first().waitFor();
    const emailPreview = pane.locator('.rw-dashboard .sg-email');
    await emailPreview.waitFor();
    assert.match(await emailPreview.textContent(), /The new campaign is live/);
    assert.match(await emailPreview.textContent(), /Hello from Alpha Studio,[\s\S]*Open the page to see the full story/);
    assert.match(await emailPreview.textContent(), /customer@example\.test/);
    assert.equal(await pane.locator('.rw-chat .conversation-inline-compose textarea').count(), 1, 'the workspace Chat owns the Signoff private channel');
    assert.equal(await pane.locator('.rw-dashboard .sg-conversation textarea').count(), 0, 'the canonical card never mounts a second composer for its own channel');
    assert.equal(await pane.locator('.rw-dashboard .sg-approve').count() >= 1, true, 'the canonical review controls remain available');
    if (shots) await pane.screenshot({path: path.join(shots, 'signoff-dashboard-desktop.png')});
    // The input-question family remains covered alongside the real email/message family.
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus .agfocus-dock').hidden);
    await page.locator('[data-signoff-key="signoff-input"]').click();
    pane = currentTab(page);
    await pane.locator('.rw-title').filter({hasText: 'Choose the launch date'}).waitFor();
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.rw-dashboard .sg-row[data-id="signoff-input"]').first().waitFor();
    await pane.locator('.rw-dashboard .sg-choices [data-choice="yes"]').first().waitFor();
    assert.equal(await pane.locator('.rw-dashboard .sg-email').count(), 0, 'the input question is not replaced by the email fixture');
    await pane.locator('.rw-close').click();
    await page.waitForFunction(() => document.querySelector('.agfocus .agfocus-dock').hidden);
    // Reopen the email Signoff for the 390px check.
    await page.locator('[data-signoff-key="signoff-1"]').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.rw-dashboard .sg-email').waitFor();

    // ---- Narrow layout: one full-width pane, no horizontal overflow, readable rows.
    await page.setViewportSize({width: 390, height: 844});
    await page.waitForTimeout(150);
    const noOverflow = async () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1 && document.body.scrollWidth <= innerWidth + 1);
    assert.equal(await noOverflow(), true, 'the Signoff dashboard fits 390px');
    if (shots) await pane.screenshot({path: path.join(shots, 'signoff-dashboard-390.png')});
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await noOverflow(), true, 'the Signoff chat fits 390px');
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await page.locator('.ttab[data-v="focus"]').click();
    await page.locator('[data-card-id="story-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').waitFor();
    assert.equal(await noOverflow(), true, 'the Story dashboard fits 390px');
    if (shots) await pane.screenshot({path: path.join(shots, 'story-dashboard-390.png')});
    await pane.locator('[data-rw-tab="chat"]').click();
    await pane.locator('.rw-conversation .conversation-inline-compose textarea').waitFor();
    assert.equal(await noOverflow(), true, 'the Story chat fits 390px');
    if (shots) await pane.screenshot({path: path.join(shots, 'story-chat-390.png')});
    await pane.locator('.rw-close').click();
    // The bounded stack never silently drops an edited oldest entry. The 21st record asks,
    // and Keep editing retains the Story draft and the current page.
    await page.locator('[data-card-id="story-1"] .ttl').click();
    pane = currentTab(page);
    await pane.locator('[data-rw-tab="dashboard"]').click();
    await pane.locator('.record-overview').click();
    await pane.locator('[data-story-overview]').fill('Dirty oldest entry that must not be evicted');
    await page.evaluate(async () => {
      for (let i = 0; i < 19; i++) await _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'bounded-' + i,
        business_id: null}, label: 'Related ' + i, summary: 'Synthetic reader'}));
    });
    const evicted = page.evaluate(() => _agpad.focusWorkspace.navigate(rwRecordFromItem({source: {type: 'document', id: 'bounded-20',
      business_id: null}, label: 'Related 20', summary: 'Synthetic reader'})));
    await page.locator('.modal').waitFor();
    assert.equal(await page.evaluate(() => _agpad.focusWorkspace.hasUnsaved()), true, 'the dirty oldest entry is still tracked');
    assert.equal(await page.locator('[data-story-overview]').count(), 1, 'the dirty Story dashboard is retained');
    await page.locator('.modal').getByRole('button', {name: 'Keep editing'}).click();
    assert.equal(await evicted, false, 'canceling eviction leaves the current stack in place');
    await currentTab(page).locator('.rw-close').click();
    await page.locator('.modal').getByRole('button', {name: 'Discard'}).click();
    await page.waitForFunction(() => document.querySelector('.agfocus-dock').hidden);
    const projectToggle = page.locator('[data-card-id="project-a"] .agfocus-project-toggle');
    if (await projectToggle.getAttribute('aria-expanded') !== 'true') await projectToggle.click();
    await page.locator('.agfocus-items [data-card-id="project-a/work/task:project-a:dup"]').waitFor();
    assert.equal(await noOverflow(), true, 'expanded Project Focus fits 390px');
    if (shots) await page.screenshot({path: path.join(shots, 'focus-project-expanded-390.png')});

    assert.deepEqual(errors, []);
    console.log('Record workspace: Story/Project/Signoff dashboards, Task subtype conversion, tabs, drafts, Back, revocation, scoped Project expansion, catalog link/unlink, Focus reselection and 390px layout passed');
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });