#!/usr/bin/env node
// node tests/test_agentpane.js — no dependencies, network, or live business writes.
// Load the real shared task refresh and log renderer over a small DOM stub.
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const ROOT = path.join(__dirname, "..");
const byId = {}, store = {}, calls = [], intervals = [], mounted = [];
let now = Date.parse("2026-09-07T12:00:00Z"), checks = 0;
const ok = (value, label) => { assert.ok(value, label); checks++; };
const equal = (value, expected, label) => { assert.deepEqual(JSON.parse(JSON.stringify(value)), expected, label); checks++; };
function mkEl() {
  const classes = new Set(), attrs = {};
  let html = "";
  return {
    isConnected: true, hidden: false, disabled: false, scrollTop: 0, writes: 0,
    children: [], dataset: {}, style: { setProperty() {} }, textContent: "", value: "",
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
      toggle(x, force) { const on = force === undefined ? !classes.has(x) : force; on ? classes.add(x) : classes.delete(x); return on; } },
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value; this.writes++;
      for (const match of value.matchAll(/\bid="([^"]+)"/g)) byId[match[1]] = mkEl();
    },
    querySelector: function(selector) {
      if (selector === "input") return this._termInput ||= mkEl();
      if (selector === "button[type=submit]") return this._termSubmit ||= mkEl();
      return selector.startsWith("#") ? byId[selector.slice(1)] || null : null;
    },
    querySelectorAll: () => [], closest: () => split,
    setAttribute: (k, v) => { attrs[k] = String(v); }, removeAttribute: k => { delete attrs[k]; }, getAttribute: k => attrs[k] ?? null,
    addEventListener() {}, removeEventListener() {}, focus() {}, blur() {}, remove() {},
    appendChild(el) { this.children.push(el); return el; },
  };
}
const split = mkEl();
const context = {
  console, URLSearchParams, TextEncoder, TextDecoder, Set, Map,
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  crypto: { randomUUID: () => "test-request-id" },
  Date: class extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } },
  localStorage: { getItem: key => store[key] ?? null, setItem: (key, value) => { store[key] = String(value); } },
  location: { search: "", hash: "" }, innerWidth: 1600, innerHeight: 900,
  document: {
    hidden: false, activeElement: null, body: mkEl(),
    querySelector: selector => selector.startsWith("#") ? byId[selector.slice(1)] ||= mkEl() : null,
    querySelectorAll: () => [], createElement: mkEl, addEventListener() {},
  },
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: fn => fn(), addEventListener() {},
  mounted,
};
let taskRows = [], logRows = [], agentRows = [{ name: "alpha" }];
let handler = async url => {
  if (url.includes("/task/")) return {task:taskRows.find(t=>t.id===decodeURIComponent(url.split("/").at(-1)))};
  if (url.includes("/tasks")) return taskRows;
  if (url.includes("/log?")) return logRows;
  if (/\/agents(?:\?|$)/.test(url)) return agentRows;
  if (url.endsWith("/terminals")) return [];
  throw new Error(`Unexpected request: ${url}`);
};
context.fetch = async (url, options = {}) => {
  calls.push({ url, method: options.method || "GET" });
  const result = url === "/api/me/tasks" ? {principal_id:"owner",tasks:[]} : await handler(url);
  return { json: async () => result };
};
context.window = context;
vm.createContext(context);
const run = code => vm.runInContext(code, context);
for (const name of ["core", "side", "agents", "task-snooze", "task-state", "tasks", "conversation", "agentpane"]) {
  vm.runInContext(fs.readFileSync(path.join(ROOT, "js", `${name}.js`), "utf8"), context, { filename: `${name}.js` });
}
// The unrelated task panel and terminal mount are outside this test's DOM surface.
run('state.bizs = ["a","b","offline"].map(slug=>({slug,id:slug,name:slug,owner:"owner",agents:["alpha","beta"]}));');
run("paintTasks = () => {}; realPaintList = paintList; paintList = () => {}; mountTerminal = name => mounted.push(name); broadcastButton = () => ''; agentNavButton = () => ''; wireBroadcastButton = () => {}; wireAgentNavToggle = () => {};");
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const tick = () => run("agpadTick()");
const countCalls = pattern => calls.filter(c => c.url !== "/api/me/tasks" && c.url.includes(pattern)).length;
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const task = (id, extra = {}) => ({ id, title: id, status: "open", owners: ["agent:alpha"], notes: [], blocked_by: [], ...extra });
const log = (id, actor = "agent:alpha") => ({ id: "log-"+id, actor, ts: "2026-09-07T11:00:00Z", tag: "coding", summary: id });
function mount(biz = "a", name = "alpha") {
  if (byId.agpad) byId.agpad.isConnected = false;
  byId.agpad = mkEl(); byId["agpad-toggle"] = mkEl();
  context.nextBiz = biz; context.nextAgent = name;
  run('state.biz = nextBiz; state.agent = nextAgent; state.tab = "agents"; agpadMount(nextAgent, $("#agpad"));');
}

(async () => {
  context.portraitTask = task('portraits', {business:{slug:'a',id:'a'}, created_by:'one', created_by_name:'Alpha', created_by_kind:'agent'});
  run('state.biz="a"; state.agents=[{name:"alpha",agent_id:"alpha-id",department:"marketing"}]');
  const unknownPortrait = run('tAssignerAvatars({business:{slug:"a"}})');
  ok(unknownPortrait.includes('identity-unknown') && !unknownPortrait.includes('identity-agent'), 'missing creator ID shows an unknown creator');
  run('state.agents=[]');
  const portraits = run('tAssignerAvatars(portraitTask)');
  ok(portraits.includes('identity-agent') && portraits.includes('<svg'), 'creator uses the matching business agent icon');
  ok(run('tAssignerAvatars({business:{slug:"a"},created_by:"human-id",created_by_name:"owner",created_by_kind:"human"})').includes('identity-founder'), 'human creator uses founder avatar');
  equal(run('tAssignerAvatars({business:{slug:"a"}})').includes('Unknown creator'), true, 'missing creator has an explicit unknown portrait');
  ok(run('identityAvatar("business", "<unsafe>")').includes('&lt;'), 'business initials and labels are escaped');
  equal(run('identityAvatar("agent","Onbashy",{kind:"business-agent",icon:"business"})').match(/>(.)<\/span>$/)[1], run('identityAvatar("business",state.bizs.find(b=>b.slug===state.biz).name)').match(/>(.)<\/span>$/)[1], 'business agent wears the business letter');
  ok(run('agentGlyph({icon:"triangle",department:"operations"},14)').includes('M12 3 22 21H2Z'), 'legacy triangle icon does not fall back to layers');
  run('agentNavPaint = agentNavRefresh = () => {}; state.agents=[{name:"zeta",display_name:"Zeta",kind:"business-agent",icon:"business"},{name:"alpha",department:"marketing"}]; realPaintList()');
  const nav = run('$("#agentrows").innerHTML');
  ok(!nav.includes('<h3>') && nav.indexOf('data-a="alpha"') < nav.indexOf('data-a="zeta"'), 'agent nav is one flat list without department headings');
  run('state.agents=[]');
  run('state.bizs[1].agent_meta={alpha:{icon:"triangle",department:"operations"}}');
  const assignees = run('tAssigneeAvatars({business:{slug:"b"},owners:["agent:alpha","human:user","agent:beta","human:other"]})');
  equal((assignees.match(/class="identity-avatar /g) || []).length, 2, 'only two assignee avatars are shown');
  ok(assignees.includes('+2</button>'), 'additional assignees use an overflow count');
  ok(assignees.includes('M12 3 22 21H2Z'), 'assignee appearance comes from the task business');
  ok(!assignees.includes('</span>alpha'), 'assignee names are not visible beside avatars');
  const beforeFetch = context.fetch, personalGate = defer(), localGate = defer(), taskRequests = [];
  context.fetch = async url => { taskRequests.push(url); return {json:() => url === '/api/me/tasks' ? personalGate.promise : localGate.promise}; };
  run('state.tab="context"');
  const personalLoad = run('refreshTasks()');
  ok(run('refreshTasks()') === personalLoad, 'repeated personal refresh shares its promise');
  equal(taskRequests, ['/api/me/tasks'], 'personal pane does not fetch all business tasks');
  personalGate.resolve({tasks:[]}); await personalLoad;
  const readyPersonal = defer(); taskRequests.length = 0;
  context.fetch = async url => { taskRequests.push(url); return {json:() => url === '/api/me/tasks' ? readyPersonal.promise : localGate.promise}; };
  run('state.tab="agents"'); const combinedLoad = run('refreshTasks()');
  readyPersonal.resolve({tasks:[task('early',{business:{slug:'a'}})]}); await flush();
  equal(run('state.myTasks[0].id'), 'early', 'personal tasks arrive before delayed agent task list');
  localGate.resolve([]); await combinedLoad; context.fetch = beforeFetch;
  // Delayed responses: retain the latest click and use each confirmed revision.
  const oldFetch = context.fetch;
  let serverTask = task('rapid', {business:{slug:'a',id:'a'},revision:7});
  const saves = [];
  context.fetch = async (url, options={}) => {
    if (options.method === 'POST' && !url.includes('/operations/tasks_get')) {
      const response=defer(); saves.push({body:JSON.parse(options.body),response});
      return {json:()=>response.promise};
    }
    return {json:async()=>url==='/api/me/tasks' ? {tasks:[JSON.parse(JSON.stringify(serverTask))]} : url.includes('/operations/tasks_get') ? {status:'ok',data:{task:JSON.parse(JSON.stringify(serverTask))}} : []};
  };
  context.rapid=JSON.parse(JSON.stringify(serverTask));
  run('state.biz="a";state.myTasks=[rapid];tSetStatus(rapid,"doing");tSetStatus(rapid,"waiting");tSetStatus(rapid,"blocked")');
  equal(saves.length,1,'rapid clicks share one in-flight save');
  equal(saves[0].body.task_id, 'rapid', 'first save targets the selected task');
  equal(saves[0].body.changes, {status:'doing'}, 'first save carries the requested status');
  equal(saves[0].body.expected_revision, 7, 'first save uses original revision');
  await run('refreshTasks()');
  equal(run('state.myTasks[0].status'),'blocked','polling preserves the latest optimistic state');
  serverTask={...serverTask,status:'doing',revision:8};saves[0].response.resolve({status:'ok',data:{task:serverTask}});await flush();
  equal(saves.length,2,'only the latest queued state is saved next');
  equal(saves[1].body.task_id, 'rapid', 'queued save targets the selected task');
  equal(saves[1].body.changes, {status:'blocked'}, 'queued save carries the latest status');
  equal(saves[1].body.expected_revision, 8, 'queued save uses confirmed revision');
  serverTask={...serverTask,status:'blocked',revision:9};saves[1].response.resolve({status:'ok',data:{task:serverTask}});await flush();
  ok(run('_taskStateEntry("a","rapid").desired') === undefined, 'settled task clears its queue');
  equal(run('state.myTasks[0].status'),'blocked','last clicked state survives final refresh');
  run('tSetStatus(state.myTasks[0],"waiting")');
  equal(saves[2].body.expected_revision,9,'a subsequent click uses the latest revision');
  saves[2].response.resolve({status:'error',error:{code:'revision_conflict',message:'Revision conflict'}});await flush();await flush();await flush();
  ok(run('_taskStateEntry("a","rapid").desired') === undefined,'failed save clears queued intentions');
  equal(run('state.myTasks[0].status'),'blocked','failed save restores server state');
  run('tSetStatus(state.myTasks[0],"done");tSetStatus(state.myTasks[0],"open")');
  equal(saves.length,4,'clicking away from done queues behind its immediate save');
  equal(saves[3].body.changes.status,'done','completion sends done immediately');
  serverTask={...serverTask,status:'done',revision:10};saves[3].response.resolve({status:'ok',data:{task:serverTask}});await flush();
  equal(saves.length,5,'latest open state is saved after done confirms');
  equal(saves[4].body.changes.status,'open','queued open state follows done');
  serverTask={...serverTask,status:'open',revision:11};saves[4].response.resolve({status:'ok',data:{task:serverTask}});await flush();
  context.fetch=oldFetch;
  run('state.myTasks=[];state.tasks=[]');

  const fixtures = [
    task("open"), task("working", { status: "working" }), task("waiting", { status: "waiting" }),
    task("stalled", { status: "waiting" }), task("dependency", { blocked_by: ["blocker"] }),
    task("done", { status: "done", blocked_by: ["blocker"] }), task("cancelled", { status: "cancelled" }),
    task("assigned", { owners: ["human:user"], notes: [{ by: "agent:alpha" }], by: "human:user" }),
    task("unassigned", { owners: [], notes: [{ by: "agent:alpha" }] }),
    task("edited-only", { owners: ["agent:beta"], by: "agent:alpha", notes: [{ by: "human:user" }] }),
    task("later-note", { owners: ["agent:beta"], notes: [{ by: "human:user" }, { by: "agent:alpha" }] }),
    task("missing-note", { owners: ["human:user"] }),
    task("assigned-done", { owners: ["human:user"], status: "done", notes: [{ by: "agent:alpha" }] }),
  ];
  context.fixtures = fixtures;
  equal(run('agpadGroups(fixtures, "alpha").open.map(t => t.id)'), ["open", "working", "waiting", "stalled", "dependency"], "owned open states, excluding closed");
  equal(run('agpadGroups(fixtures, "alpha").waiting.map(t => t.id)'), ["assigned", "unassigned"], "waiting on uses first-note proxy, not latest editor or later author");
  equal(run('agpadGroups([], "alpha")'), { open: [], waiting: [], snoozed: [] }, "separate deferred group, without a duplicate blocked section");
  context.deferred = [task('deferred', {snoozed_until:'2999-01-01T00:00:00Z'}), task('expired', {snoozed_until:'2000-01-01T00:00:00Z'})];
  equal(run('agpadGroups(deferred, "alpha").open.map(t=>t.id)'), ['expired'], 'expired snooze is active');
  equal(run('agpadGroups(deferred, "alpha").snoozed.map(t=>t.id)'), ['deferred'], 'snoozed owned task stays in separate group');
  context.shared = [task("shared", { owners: ["agent:beta", "agent:alpha"], notes: [{ by: "agent:alpha" }] })];
  equal(run('agpadGroups(shared, "alpha").open.map(t => t.id)'), ["shared"], "second assignee sees the shared task");
  equal(run('agpadGroups(shared, "alpha").waiting'), [], "shared assignment is not duplicated into waiting on");
  context.late = [task("late-first-note", { owners: ["human:user"], created: "2026-09-01", notes: [{ by: "agent:alpha", ts: "2026-09-07" }] })];
  equal(run('agpadGroups(late, "alpha").waiting.map(t => t.id)'), ["late-first-note"], "documented proxy includes first notes added after creation");
  context.unsafe = task('unsafe" data-bad="x', { title: '<img src=x onerror="alert(1)">', status: 'open" onclick="bad', due: '<b>soon</b>', owners: ['<svg onload="bad">'] });
  context.relatedTask = task('linked', {business:{slug:'a'}, tags:['custom-tag'], parent:{id:'group', title:'Launch group', status:'open'}});
  equal(run('tRelatedItems(relatedTask).map(item=>item.label)'), ['Launch group'], 'row context shows only the parent Task');
  ok(!run('tChips(relatedTask)').includes('custom-tag'), 'row parent slot omits ordinary task tags');
  ok(run('tChips({...relatedTask,id:"unlinked",parent:null})').includes('aria-hidden="true"'), 'unparented task reserves an empty slot');
  const html = run("tRow(unsafe, true)");
  ok(!/<(?:img|script)\b/.test(html), "task title is escaped by the shared renderer");
  ok(html.includes("&lt;img") && html.includes("&lt;b&gt;") && html.includes("&lt;svg"), "title, due, and owner are escaped");
  ok(!html.includes(' onclick="') && !html.includes(' data-bad="'), "status and id cannot inject attributes");
  equal(intervals.filter(i => i.ms === 1000).length, 1, "one local pane reconciliation timer");
  const body = () => byId["agpad-body"];
  const view = name => {
    // Activity is retained as an optional view, but is absent from normal agent navigation.
    if (name === 'activity') run('if (!_agpad.views.includes("activity")) _agpad.views.push("activity")');
    return run(`agpadSetView(_agpad, ${JSON.stringify(name)})`);
  };

  // A mount shares the task refresh without flashing the previous business's cache.
  const freshTasks = defer(), freshLog = defer();
  const normal = handler;
  handler = url => url.includes("/tasks") ? freshTasks.promise : freshLog.promise;
  run('state.tasks = [{ id: "old-business", title: "PRIVATE OLD BUSINESS", owners: ["agent:alpha"], status: "open" }];');
  mount();
  ok(body().innerHTML.includes("Loading tasks"), "task cache is withheld until refreshed");
  ok(!body().innerHTML.includes("PRIVATE OLD BUSINESS"), "no prior-business task flash");
  equal(run('_agpad.view'), "tasks", "Tasks is the default view");
  equal([...byId.agpad.innerHTML.matchAll(/data-v="([^"]+)"/g)].map(m => m[1]), ["tasks", "inbox", "status"], "business agent navigation includes Tasks, Inbox, and Agent status");
  ok(byId.agpad.innerHTML.includes(run('tTabs(["tasks", "inbox", "status"], "tasks", "agpad-body")')), "navigation uses the drawer's exact icon markup");
  ok(byId.agpad.innerHTML.includes('aria-controls="agpad-body"') && byId.agpad.innerHTML.includes('aria-labelledby="agpad-body-tab-tasks"'), "agent tabs reference their own accessible panel");
  ok(!byId.agpad.innerHTML.includes('id="ttab-tasks"'), "agent tab IDs cannot collide with the drawer");
  ok(!byId.agpad.hidden, "default is expanded");
  equal(countCalls("/tasks"), 1, "one shared task refresh on mount");
  equal(countCalls("/log"), 0, "Tasks does not fetch hidden Activity");
  freshTasks.resolve(fixtures); await flush(); tick();
  ok(body().innerHTML.includes('<h2>Open tasks</h2><span class="c">5</span>'), "loaded open count");
  ok(body().innerHTML.includes('<h2>Waiting on</h2><span class="c">2</span>'), "waiting count excludes owned tasks");
  equal((body().innerHTML.match(/data-id="waiting"/g) || []).length, 1, "owned waiting task appears only once");
  ok(!body().innerHTML.includes('<h2>Blocked') && !body().innerHTML.includes('agpad-meta'), "no Blocked section or custom status/due line");
  ok(body().innerHTML.includes(run('tRow(fixtures[0], true)')), "pane uses the shared task row markup");
  ok(!body().innerHTML.includes('class="tplus"') && !body().innerHTML.includes('class="tshbtn"'), "agent groups do not create department tasks");
  handler = normal; taskRows = fixtures;
  const writes = body().writes;
  body().scrollTop = 80;
  tick(); tick();
  equal(body().writes, writes, "unchanged task data preserves DOM and hover");
  equal(body().scrollTop, 80, "scroll position survives reconciliation");
  run('state.tasks[0].status = "done"'); tick();
  ok(!body().innerHTML.includes('data-id="open"'), "optimistic in-place completion removes the task");
  equal(countCalls("/tasks"), 1, "local reconciliation never polls tasks itself");

  handler = url => url.includes("/log") ? freshLog.promise : Promise.resolve(fixtures);
  view("activity");
  equal(run('tView'), "tasks", "agent tabs do not change the drawer view");
  equal(calls.filter(c => c.url.includes("/log")).map(c => c.url), ["/api/biz/a/log?n=200"], "Activity lazily requests the business sample");
  for (let i = 0; i < 10; i++) tick();
  equal(countCalls("/log"), 1, "pending activity requests do not overlap");
  freshLog.resolve([log("alpha action"), log("beta action", "agent:beta"), log("human action", "human:alpha")]);
  await flush(); tick();
  equal(run('_agpad.logs.length'), 1, "activity uses exact actor matching");
  ok(body().innerHTML.includes("alpha action") && !body().innerHTML.includes("beta action"), "only selected agent activity renders");
  equal(body().innerHTML, run('tActivityHtml(_agpad.logs)'), "activity uses the drawer's full log markup");
  const logWrites = body().writes; tick();
  equal(body().writes, logWrites, "unchanged log data preserves its DOM and tooltips");
  handler = normal;
  now += 12000; logRows = [log("new activity")]; tick(); await flush();
  equal(countCalls("/log"), 2, "visible Activity refreshes after 12 seconds");
  ok(body().innerHTML.includes("new activity"), "new activity replaces the previous sample");

  // Errors preserve the previous log and recover on the next poll.
  handler = async () => { throw new Error("offline"); };
  now += 12000; tick(); await flush();
  ok(body().innerHTML.includes("Could not refresh") && body().innerHTML.includes("new activity"), "failed fetch retains the last good log");
  handler = normal; logRows = []; now += 12000; tick(); await flush();
  ok(body().innerHTML.includes("latest 200 business log entries"), "empty state explains the sampled history");
  ok(!body().innerHTML.includes("Could not refresh"), "successful retry clears the error");
  const beforeTasks = countCalls("/log");
  view("tasks"); now += 24000; tick();
  equal(countCalls("/log"), beforeTasks, "switching back to Tasks stops Activity polling");
  view("activity"); await flush();

  const hiddenCalls = countCalls("/log");
  run('state.tab = "context"'); now += 24000; tick();
  equal(countCalls("/log"), hiddenCalls, "inactive Agents tab does not poll");
  run('state.tab = "agents"'); context.document.hidden = true; tick();
  equal(countCalls("/log"), hiddenCalls, "background browser document does not poll");
  context.document.hidden = false; byId.agpad.isConnected = false; tick();
  equal(countCalls("/log"), hiddenCalls, "detached pane does not poll");

  // Resolve the old business last, including a response to an inactive Activity tab.
  const oldLog = defer();
  handler = url => url.includes("/log") ? oldLog.promise : Promise.resolve(fixtures);
  mount("a"); await flush(); view("activity");
  const newTasks = defer();
  handler = url => url.includes("/tasks") ? newTasks.promise : Promise.resolve([log("business b action")]);
  mount("b"); await flush();
  ok(body().innerHTML.includes("Loading tasks"), "new business with the same agent withholds old tasks");
  equal(run('_agpad.view'), "tasks", "a fresh mount always defaults to Tasks");
  view("activity"); await flush();
  oldLog.resolve([log("STALE BUSINESS A")]); await flush();
  ok(!body().innerHTML.includes("STALE") && body().innerHTML.includes("business b action"), "late previous-business activity is discarded");
  newTasks.resolve([task("business b task")]); await flush(); view("tasks");
  ok(body().innerHTML.includes("business b task"), "new business's tasks replace loading state");
  const hiddenLog = defer(); handler = () => hiddenLog.promise;
  view("activity"); view("tasks");
  const taskHtml = body().innerHTML;
  hiddenLog.resolve([log("INACTIVE TAB")]); await flush();
  equal(body().innerHTML, taskHtml, "late Activity response cannot repaint Tasks");
  ok(!run('_agpad.logs.some(e => e.summary === "INACTIVE TAB")'), "inactive Activity response is discarded");
  store["agpad:b"] = "off";
  handler = normal; mount("a"); await flush();
  ok(!byId.agpad.hidden, "first business restores its expanded preference");
  mount("b");
  ok(!byId.agpad.hidden, "removed toggle cannot strand a pane behind an old collapsed preference");

  handler = async () => { throw new Error("offline"); };
  mount("offline"); await flush();
  ok(body().innerHTML.includes("Tasks unavailable"), "initial shared task failure has a visible state");
  view("activity"); await flush();
  ok(body().innerHTML.includes("Activity unavailable"), "initial activity failure has a visible state");
  handler = normal; taskRows = [task("recovered")]; await run("refreshTasks()"); view("tasks");
  ok(body().innerHTML.includes("recovered"), "later shared task refresh recovers the pane");

  // Owner inspection never acquires the agent's lease or acknowledges events.
  run('state.agents = [{name:"alpha",agent_id:"agent-alpha"}]');
  const inboxItem = {id:1,source_id:'story-1',source_type:'stories',business_slug:'offline',summary:'<private update>',event_type:'updated',actor_name:'Owner',created_at:'2026-09-07T11:00:00Z'};
  const inboxData = {items:[inboxItem],subscriptions:[],next_cursor:1,delivery:{state:'pending',reason:'offline',last_attempt_at:null}};
  handler = async url => url.includes('/inbox') ? {status:'ok',data:url.includes('after=') ? {...inboxData,items:[{...inboxItem,id:2}],next_cursor:null} : inboxData} : normal(url);
  view('inbox'); await flush();
  ok(body().innerHTML.includes('Notice pending') && body().innerHTML.includes('starts one manually'), 'an offline pending notice says it needs a manual session start');
  ok(!/watcher|Monitor/i.test(body().innerHTML), 'no watcher or Monitor setup is shown');
  for (const [name, text] of [['idle','Nothing waiting'],['deferred','Notice deferred'],['submitted','processing unconfirmed'],['uncertain','not resent'],['stale','Session changed'],['unavailable','delivery unavailable'],['unknown','status unavailable']]) {
    run(`_agpad.inbox = {..._agpad.inbox, delivery:{state:${JSON.stringify(name)},reason:${name==='unknown'?'"nudge_status_unavailable"':'null'},last_attempt_at:null}}; _agpad.bodyHtml=''; agpadPaintInbox(_agpad)`);
    ok(body().innerHTML.toLowerCase().includes(text.toLowerCase()), name + ' delivery is described');
    ok(!/\bread\b|acknowledged/i.test(body().querySelector?.('.agpad-inbox-status')?.textContent || body().innerHTML.match(/agpad-inbox-status[^]*?<\/div>/)?.[0] || ''), name + ' makes no read or acknowledged claim');
  }
  run('_agpad.inbox = {..._agpad.inbox, delivery:{state:"pending",reason:"offline",last_attempt_at:null}}; _agpad.bodyHtml=""; agpadPaintInbox(_agpad)');
  ok(body().innerHTML.includes('&lt;private update&gt;') && !body().innerHTML.includes('<private update>'), 'source summaries are escaped');
  await run('agpadRefreshInbox(_agpad, true)');
  equal(run('_agpad.inbox.items.map(e=>e.id)'), [1,2], 'more appends pending updates');
  ok(calls.filter(c=>c.url.includes('/inbox')).every(c=>c.method==='GET'), 'owner inbox only performs reads');
  run('_agpad.inbox.items = [{id:3, created_at:"2026-09-10T00:00:00Z", message:{sender_name:"<Onbashy>",recipient_name:"Marketing",reply_to:"message-one",body:"<script>alert(1)</script>\\nSecond line"}}]; agpadPaintInbox(_agpad)');
  ok(body().innerHTML.includes('Message from &lt;Onbashy&gt;'), 'message sender is escaped');
  ok(body().innerHTML.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), 'message body is rendered as escaped text');
  ok(body().innerHTML.includes('<details') && body().innerHTML.includes(' · Reply'), 'message can expand inline and identifies replies');
  ok(!body().innerHTML.includes('data-inbox-source'), 'direct message is not routed as a context source');
  // Current participants route; originals stay visible as attribution.
  run('_agpad.inbox.items = [{id:4, created_at:"2026-09-10T00:00:00Z", message:{id:"m4",sender_id:"old-onbashy",recipient_id:"ord",current_sender_id:"agent-alpha",current_recipient_id:"ord",sender_name:"Onbashy",recipient_name:"Ordubashy",current_sender_name:"Tamga",current_recipient_name:"Ordubashy",body:"Status"}}]; agpadPaintInbox(_agpad)');
  ok(body().innerHTML.includes('Message from Tamga') && body().innerHTML.includes('Originally from Onbashy'), 'moved conversation shows the current and original sender');
  ok(!body().innerHTML.includes('Originally to') && !body().innerHTML.includes('Read-only history'), 'unchanged recipient and live history carry no extra labels');
  run('_agpad.inbox.items = [{id:5, created_at:"2026-09-10T00:00:00Z", message:{id:"m5",sender_id:"old-strategist",recipient_id:"old-onbashy",current_sender_id:"agent-alpha",current_recipient_id:"agent-alpha",sender_name:"Strategist",recipient_name:"Onbashy",current_sender_name:"Tamga",current_recipient_name:"Tamga",body:"Old plan"}}]; agpadPaintInbox(_agpad)');
  ok(body().innerHTML.includes('Read-only history') && body().innerHTML.includes('data-read-only="true"'), 'collapsed history is marked read-only');
  ok(body().innerHTML.includes('Originally from Strategist') && body().innerHTML.includes('Originally to Onbashy'), 'collapsed history keeps both original participants');
  ok(!/reply/i.test(body().innerHTML.replace(/Read-only history/g, '')), 'collapsed history offers no reply');
  const delayedInbox = defer(); handler = () => delayedInbox.promise;
  const waitingInbox = run('agpadRefreshInbox(_agpad)');
  view('tasks'); const beforeInboxResponse = body().innerHTML;
  delayedInbox.resolve({status:'ok',data:{...inboxData,items:[{...inboxItem,summary:'LATE INBOX'}]}});
  await waitingInbox; await flush();
  equal(body().innerHTML,beforeInboxResponse,'late inbox response cannot repaint another view');
  handler = normal;

  // A pane click opens the exact existing task detail and stays open through outside-click handling.
  context.detailIds = [];
  run('savedDetail = tDetail; tDetail = t => detailIds.push(t.id); tSearch = "no match"; tView = "activity"; tInside = false;');
  byId.agpad.onclick({ target: { closest: () => ({ dataset: { id: "recovered" } }) } });
  equal(context.detailIds, ["recovered"], "pane click opens the selected task detail");
  equal(run('tView'), "tasks", "cross-pane navigation selects the drawer's Tasks tab");
  equal(run('tSearch'), "", "cross-pane navigation clears a filter hiding the selected task");
  ok(run('tPanelOpen() && tInside'), "outside-click handling keeps the opened drawer visible");
  await run('tOpenTask("missing")'); run("tDetail = savedDetail;");
  equal(context.detailIds, ["recovered"], "missing tasks cannot open a different detail");
  // Selection changes and navigation while renderAgents is awaiting its list.
  const first = defer(); context.content = mkEl();
  handler = url => /\/agents(?:\?|$)/.test(url) ? first.promise : Promise.resolve([]);
  run('state.biz = "a"; state.agent = "alpha"; state.tab = "agents"');
  const pendingRender = run("renderAgents(content)");
  ok(context.content.innerHTML.includes('agentrows'), 'agent layout renders before list response');
  run('state.tab = "context"; content.innerHTML = "Context page"'); first.resolve([{ name: "alpha" }]); await pendingRender;
  equal(context.content.innerHTML, "Context page", "agent-list response cannot overwrite another tab");
  const second = defer();
  handler = url => /\/agents(?:\?|$)/.test(url) ? second.promise : Promise.resolve([]);
  run('state.tab = "agents"'); const wrongBiz = run("renderAgents(content)");
  run('state.biz = "other"; content.innerHTML = "Other business"'); second.resolve([{ name: "alpha" }]); await wrongBiz;
  equal(context.content.innerHTML, "Other business", "agent-list response cannot overwrite another business");
  const older = defer();
  handler = url => /\/agents(?:\?|$)/.test(url) ? older.promise : Promise.resolve([]);
  run('state.biz = "a"; state.agent = "alpha"'); const olderRender = run("renderAgents(content)");
  handler = normal; agentRows = [{ name: "alpha" }, { name: "beta" }];
  run('state.agent = "beta"'); await run("renderAgents(content)"); await flush();
  older.resolve([{ name: "alpha" }]); await olderRender;
  equal(mounted, ["beta"], "last selection wins even when the previous request finishes last");
  equal(run("_agpad.agent"), "beta", "pane follows the selected terminal");
  ok(!byId.agpad.innerHTML.includes("<h1>"), "agent pane omits the redundant agent heading");
  const refresh = defer(), mountsBefore = mounted.length;
  handler = url => /\/agents(?:\?|$)/.test(url) ? refresh.promise : Promise.resolve([]);
  const cachedRender = run("renderAgents(content)");
  equal(mounted.length, mountsBefore + 1, "cached agent connects before roster refresh finishes");
  equal(mounted.at(-1), "beta", "cached navigation preserves selected agent");
  refresh.resolve([{ name: "alpha" }, { name: "beta" }]); await cachedRender;
  equal(mounted.length, mountsBefore + 1, "fresh roster does not reconnect the cached terminal");
  const mountsAfter = mounted.length;
  run('state.biz = "uncached"');
  const foreign = defer();
  handler = url => /\/agents(?:\?|$)/.test(url) ? foreign.promise : Promise.resolve([]);
  const foreignRender = run("renderAgents(content)");
  equal(mounted.length, mountsAfter, "another business cannot use the cached roster");
  foreign.resolve([]); await foreignRender;
  run('state.biz = "a"'); handler = normal;
  agentRows = []; await run("renderAgents(content)");
  ok(byId.agpad.hidden, "no-agent state hides the pane");
  equal(run("state.agent"), null, "no-agent state clears the old selection");
  ok(calls.every(c => c.method === "GET"), "pane and shared refresh issue no business writes");

  // Every activity surface uses explicit refs.task, never words in the summary or other evidence.
  context.linkEntries = [
    log("plain action with recovered in its title"),
    { ...log("linked action"), refs: { task: " recovered " } },
    { ...log("url only"), refs: { url: "https://example.test/task/recovered" } },
    { ...log("empty reference"), refs: { task: "   " } },
    { ...log("invalid reference"), refs: { task: ["recovered"] } },
    { ...log("closed task action"), refs: { task: "closed-linked" } },
  ];
  equal(run('linkEntries.map(logTaskId)'), ["", "recovered", "", "", "", "closed-linked"], "only explicit nonempty string task references count");
  const linkedHtml = run('logHtml(linkEntries)');
  equal((linkedHtml.match(/class="ltask"/g) || []).length, 2, "only associated activity rows get a task icon");
  equal((linkedHtml.match(/class="le has-task"/g) || []).length, 2, "only linked rows reserve room for the icon");
  ok(linkedHtml.includes(run('T_VIEW_ICON.tasks')), "related-task buttons reuse the Tasks navigation icon");
  context.maliciousLog = { ...log('<img src=x onerror="bad()">'), refs: { task: 'id" onclick="bad()' } };
  const safeLog = run('logHtml([maliciousLog])');
  ok(!safeLog.includes('<img') && !safeLog.includes(' onclick="'), "log summary and task reference cannot inject markup");

  const linkedButton = mkEl(), linkedRow = mkEl();
  linkedRow.querySelector = sel => sel === '.ltask' ? linkedButton : null;
  context.logHost = { querySelectorAll: () => [linkedRow] };
  context.linkClicks = [];
  const navigation = defer(); context.navigation = navigation.promise;
  run('state.biz = "a"; savedOpenTask = tOpenTask; tOpenTask = id => { linkClicks.push(id); return navigation; }; logTips(logHost, [linkEntries[1]]);');
  const pendingLink = linkedButton.onclick();
  ok(linkedButton.disabled, "the task icon waits for a pending lookup");
  await linkedButton.onclick();
  equal(context.linkClicks, ["recovered"], "one click opens the explicit task; pending clicks do not duplicate navigation");
  navigation.resolve(); await pendingLink;
  ok(!linkedButton.disabled, "task icon is available again after navigation");
  run('state.biz = "b"'); await linkedButton.onclick();
  equal(context.linkClicks, ["recovered"], "old-business activity cannot open tasks in the new business");
  run('state.biz = "a"'); linkedButton.isConnected = false; await linkedButton.onclick();
  equal(context.linkClicks, ["recovered"], "removed activity rows cannot open tasks");
  run('tOpenTask = savedOpenTask;');

  // Linked tasks may be completed or not yet in the shared cache when Activity arrives.
  context.openedLinks = []; context.linkWarnings = [];
  run('savedDetail = tDetail; savedNotify = notify; tDetail = t => openedLinks.push(t.id); notify = n => linkWarnings.push(typeof n === "string" ? n : n.text); state.tasks = [];');
  taskRows = [task("closed-linked", { status: "done" })];
  const beforeLookup = countCalls('/task/');
  await run('tOpenTask("closed-linked")');
  equal(context.openedLinks, ["closed-linked"], "a task absent from the cache is loaded and opens even when completed");
  ok(countCalls('/task/') > beforeLookup, "a missing task loads its business-scoped detail");
  const lateLookup = defer(); handler = () => lateLookup.promise;
  run('state.tasks = [];');
  const pendingLookup = run('tOpenTask("late-task")');
  run('state.biz = "b"; state.tasks = [];');
  lateLookup.resolve({task:task("late-task")}); await pendingLookup;
  equal(context.openedLinks, ["closed-linked"], "a lookup resolving after a business switch cannot open a stale task");
  equal(run('state.tasks'), [], "the shared refresh discards tasks from the previous business");
  handler = normal; taskRows = []; run('tView = "activity"');
  await run('tOpenTask("missing-task")');
  equal(context.linkWarnings, ["Task is no longer available."], "a dangling link has a useful outcome");
  equal(run('tView'), "activity", "a missing task leaves the current Activity view in place");
  handler = async () => { throw new Error('offline'); };
  await run('tOpenTask("offline-task")');
  equal(context.linkWarnings[1], "Could not load task.", "a failed lookup is reported without opening an unrelated task");
  run('tDetail = savedDetail; notify = savedNotify;'); handler = normal;
  ok(calls.every(c => c.method === "GET"), "activity task navigation never mutates tasks or logs");

  // Inbox links open the exact canonical related source.
  context.sourceOpens = [];
  context.tablesOpen = (biz, id) => context.sourceOpens.push(['table', biz, id]);
  context.storyCall = async (biz, op, args) => ({item:{label:'Reference without URL'}});
  context.modal = options => context.sourceOpens.push(['modal', options.title]);
  run('state.scope = "business"; state.biz = "b"');
  await run('agpadOpenInboxSource({inboxSource:"table-one",sourceType:"data_tables",business:"b"})');
  await run('agpadOpenInboxSource({inboxSource:"entity-one",sourceType:"context_entities",business:"b"})');
  equal(context.sourceOpens, [['table','b','table-one'],['modal','Reference without URL']],
    'table opens its exact ID and a reference without a URL still opens details');

  // A pending ResizeObserver debounce or animation frame can outlive its
  // terminal. Reading xterm's dimensions after disposal throws in a browser.
  const terminals = [], observers = [], frames = [], resizeTimers = [];
  context.Terminal = class {
    constructor() {
      terminals.push(this); this.cols = 80; this.rows = 24; this.resizes = 0;
      const terminal = this;
      this._core = { _renderService: { get dimensions() {
        if (terminal.disposed) throw new Error("Measured a disposed terminal");
        return { css: { cell: { width: 8, height: 16 } } };
      } } };
    }
    open(host) { host.clientWidth = 640; host.clientHeight = 320; }
    resize(cols, rows) { this.cols = cols; this.rows = rows; this.resizes++; }
    dispose() { this.disposed = true; }
    scrollToBottom() {} focus() {} onData() {}
  };
  context.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe() {} disconnect() {}
  };
  context.EventSource = class { addEventListener() {} close() {} };
  context.requestAnimationFrame = callback => frames.push(callback);
  context.setTimeout = callback => resizeTimers.push(callback);
  handler = async () => ({});
  vm.runInContext(fs.readFileSync(path.join(ROOT, "js/terminal.js"), "utf8"), context, { filename: "terminal.js" });
  run('mountTerminal("alpha", $("#chat"))');
  observers[0].callback(); // queue a fit, then replace the terminal before it fires
  run('mountTerminal("beta", $("#chat"))');
  frames.forEach(callback => callback()); resizeTimers.forEach(callback => callback());
  ok(terminals[0].disposed, "switching agents disposes the previous terminal");
  equal(terminals[0].resizes, 0, "queued callbacks never measure or resize a disposed terminal");
  equal(terminals[1].resizes, 1, "current terminal still fits normally");
  byId.termhost.isConnected = false; byId.termhost.clientWidth = 480;
  observers[1].callback(); resizeTimers.pop()();
  equal(terminals[1].cols, 80, "detached host cannot resize the background terminal");
  run("teardownTerm()"); await flush();
  console.log(`${checks} agent pane checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
