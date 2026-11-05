#!/usr/bin/env node
// node tests/test_workspace_dispatch.js — the agent pane's workspace registry,
// role-derived views and renderer dispatch. No dependencies, network or writes.
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const ROOT = path.join(__dirname, "..");
const ELEMENTS = { split: null };
let checks = 0;
const ok = (value, label) => { assert.ok(value, label); checks++; };
const equal = (value, expected, label) => {
  assert.deepEqual(JSON.parse(JSON.stringify(value)), JSON.parse(JSON.stringify(expected)), label); checks++;
};

function mkEl(byId) {
  const classes = new Set(), attrs = {}, events = {}, memo = {};
  let html = "";
  return {
    isConnected: true, hidden: false, disabled: false, scrollTop: 0, offsetWidth: 40, offsetLeft: 0, writes: 0,
    children: [], dataset: {}, style: { setProperty() {} }, textContent: "", value: "",
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
      toggle(x, force) { const on = force === undefined ? !classes.has(x) : force; on ? classes.add(x) : classes.delete(x); return on; } },
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value; this.writes++;
      for (const key of Object.keys(memo)) delete memo[key];
      for (const match of value.matchAll(/\bid="([^"]+)"/g)) byId[match[1]] = mkEl(byId);
    },
    querySelector: function(selector) {
      if (selector.startsWith("#")) return byId[selector.slice(1)] || null;
      return memo[selector] ||= mkEl(byId);
    },
    querySelectorAll: () => [], closest: () => ELEMENTS.split,
    setAttribute: (k, v) => { attrs[k] = String(v); }, removeAttribute: k => { delete attrs[k]; }, getAttribute: k => attrs[k] ?? null,
    addEventListener(type, fn) { (events[type] ||= []).push(fn); },
    removeEventListener(type, fn) { events[type] = (events[type] || []).filter(item => item !== fn); },
    fire(type) { for (const fn of (events[type] || []).slice()) fn(); },
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }),
    focus() {}, blur() {}, remove() {}, after() {}, appendChild(node) { this.children.push(node); return node; },
  };
}

// One isolated page: fresh globals, real dashboard modules, a controllable fetch.
function boot(handler) {
  const byId = {}, store = {}, calls = [], messages = [], nav = { urls: [] };
  const context = {
    console, URL, URLSearchParams, TextEncoder, TextDecoder, Set, Map, JSON, Math,
    AbortController: class { constructor() { this.signal = {}; } abort() {} },
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [Date.parse("2026-10-01T12:00:00Z")])); } static now() { return Date.parse("2026-10-01T12:00:00Z"); } },
    crypto: { randomUUID: () => "request-id" },
    localStorage: { getItem: key => store[key] ?? null, setItem: (key, value) => { store[key] = String(value); } },
    location: { search: "", hash: "#studio", href: "http://qorqut.test/#studio", origin: "http://qorqut.test" },
    history: { replaceState: (state, title, url) => {
      nav.urls.push(String(url));
      const next = new URL(String(url));
      context.location.search = next.search; context.location.href = next.href;
    } },
    innerWidth: 1600, innerHeight: 900,
    document: { hidden: false, activeElement: null, body: mkEl(byId),
      querySelector: selector => selector.startsWith("#") ? byId[selector.slice(1)] ||= mkEl(byId) : null,
      querySelectorAll: () => [], createElement: () => mkEl(byId), addEventListener() {} },
    setInterval: () => 1, setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: fn => fn(),
    addEventListener: (type, fn) => { (messages[type] ||= []).push(fn); },
    removeEventListener: (type, fn) => { messages[type] = (messages[type] || []).filter(item => item !== fn); },
  };
  context.window = context;
  vm.createContext(context);
  const split = mkEl(byId); ELEMENTS.split = split;
  context.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET", body: options.body });
    const result = await handler(url, options);
    if (result && result.raw) return result;
    return { json: async () => result };
  };
  for (const name of ["core", "task-snooze", "task-state", "tasks", "agentpane"]) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, "js", `${name}.js`), "utf8"), context, { filename: `${name}.js` });
  }
  const run = code => vm.runInContext(code, context);
  run('paintTasks = () => {}; paintList = () => {}; mountTerminal = () => {}; broadcastButton = () => ""; agentNavButton = () => ""; wireBroadcastButton = () => {}; wireAgentNavToggle = () => {}; tOpenTask = () => {}; agentCurrentWorkMount = () => {}; globalTasksMount = () => {}; fakeMounts = 0; fakeTicks = 0; fakeDisposed = 0; focusMounts = 0; focusTicks = 0; agfocusMount = () => { focusMounts++; }; agfocusTick = () => { focusTicks++; };');
  run('state.bizs = [{slug:"studio", id:"biz-1", name:"Studio"}]; state.biz = "studio"; state.scope = "business"; state.tab = "agents"; state.agents = [{name:"worker-one", agent_id:"agent-w1", role:{id:"global-worker"}}];');
  const pane = (name, options) => {
    if (byId.agpad) byId.agpad.isConnected = false;
    byId.agpad = mkEl(byId); byId["agpad-toggle"] = mkEl(byId);
    context.paneName = name; context.paneOptions = options;
    run('agpadMount(paneName, $("#agpad"), paneOptions)');
    return run("_agpad");
  };
  return { run, calls, store, byId, messages, context, pane, nav, body: () => byId["agpad-body"], split };
}
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const declares = types => async url => url.endsWith("/operations/workspace_types")
  ? { status: "ok", data: { types } }
  : url.endsWith("/operations/workspace_get") ? { status: "ok", data: { workspace: null } } : { status: "ok", data: {} };
const readFails = async () => ({ status: "ok", data: {} });
// A stand-in extension renderer, registered the way an extension asset does.
const FAKE = `registerWorkspaceRenderer("fake_board", { mount: p => { fakeMounts++; $("#agpad-body", p.el).innerHTML = "Fake board"; },
  tick: () => { fakeTicks++; }, dispose: () => { fakeDisposed++; }, leading: true,
  views: { fake_review: { label: "Fake review", icon: "<svg></svg>" } } })`;
const fakeType = { key: "fake_review", display_name: "Fake review", supported_roles: ["global-worker"], renderer: "fake_board", presentation_actions: [] };

(async () => {
  // Core-only: Focus is the only registered workspace; no extension vocabulary remains.
  const seed = boot(readFails);
  equal(seed.run('workspaceViewsFor("ordubashy").map(type=>type.key)'), ["focus"], "ordubashy declares Focus");
  equal(seed.run('workspaceViewsFor("global-worker").map(type=>type.key)'), ["focus"], "a global worker keeps Focus without a declaration, as the host declares");
  equal(seed.run('workspaceViewsFor("founder")'), [], "an undeclared role has no workspace type");
  equal(seed.run('agentPaneViews("global","ordubashy",["inbox","globalTasks"])'), ["focus", "browser", "desktop", "inbox", "globalTasks", "status"],
    "Focus keeps its existing first-tab position for the coordinator");
  equal(seed.run('agentPaneViews("business","global-worker",["tasks","inbox"])'), ["focus", "tasks", "inbox", "status"], "Focus leads the base views for a global worker when the registry is unavailable");
  equal(seed.run('agentPaneViews("business",null,["tasks","inbox"])'), ["tasks", "inbox", "status"], "a roleless agent keeps only base views");
  ok(!seed.run('workspaceTypes().some(type => type.key === "threads" || type.renderer === "threads")'),
    "the removed Threads type is not registered or declared");
  equal(seed.run('agentPaneViews("global","ordubashy",["business","campaign_review"],false,"test-agent")'),
    ["focus", "browser", "desktop", "viewbox", "status"], "stale base arrays never re-add retired views or Threads");
  ok(seed.run('(() => { const views = agentPaneViews("global","learning-applier",["viewbox","business","campaign_review"],false,"test-agent"); return !views.includes("viewbox") && !views.includes("business") && !views.includes("campaign_review"); })()'),
    "Learning Applier is never offered Viewbox by a stale base array");
  equal(seed.run('agentPaneViews("global","global-worker",["viewbox","business"],false,"test-agent")'),
    ["focus", "viewbox", "status"], "other agents keep their Viewbox entry");
  equal(seed.run('agentPaneViews("business",null,["tasks","business","campaign_review"])'),
    ["tasks", "status"], "a roleless pane filters retired keys out of a stale base array");
  ok(seed.run('T_VIEW_LABEL.synth_review === undefined && T_VIEW_ICON.synth_review === undefined'), "core carries no extension label or icon");

  // Registration is validated and rejects duplicates.
  const throws = (code, label) => { let failed = false; try { seed.run(code); } catch (_) { failed = true; } ok(failed, label); };
  throws('registerWorkspaceRenderer("focus_board", { mount() {}, views: {} })', "a duplicate renderer name is rejected");
  throws('registerWorkspaceRenderer("Bad Name", { mount() {}, views: {} })', "a malformed renderer name is rejected");
  throws('registerWorkspaceRenderer("x_board", { views: {} })', "a definition without mount is rejected");
  throws('registerWorkspaceRenderer("x_board", { mount() {}, views: { y: { label: "Y" } } })', "a view without an icon is rejected");
  throws('registerWorkspaceRenderer("x_board", { mount() {}, tick: 1, views: {} })', "a non-function tick is rejected");
  equal(seed.run('typeof registerWorkspaceRenderer'), "function", "the global registration function exists");

  // Focus dispatch: the registry mounts the existing renderer and its tick.
  seed.run('state.agent = "chief"; state.scope = "global"');
  const focusPane = seed.pane("chief", { biz: null, workspaceScope: "global", workspaceRole: "ordubashy", agentId: "agent-o", isCurrent: () => true });
  equal(focusPane.view, "focus", "the coordinator pane still opens on Focus");
  equal(seed.run("focusMounts"), 1, "Focus mounts through its registry renderer");
  equal(focusPane.nextFocus, 0, "mounting Focus still resets its poll gate");
  const ticks = seed.run("focusTicks");
  seed.run("agpadTick()");
  ok(seed.run("focusTicks") > ticks, "the pane tick still drives agfocusTick");
  equal(seed.run('[..._agpad.el.innerHTML.matchAll(/data-v="([^"]+)"/g)].map(m=>m[1])'),
    ["focus", "inbox", "status"], "the coordinator pane lists exactly its declared views");

  // A registration alone adds no view; the server's declaration does, with its role.
    seed.run(FAKE);
  equal(seed.run('workspaceViewsFor("global-worker").map(type=>type.key)'), ["focus"], "an asset registration alone adds no view beyond the Focus fallback");
  equal(seed.run("T_VIEW_LABEL.fake_review"), "Fake review", "the registered label is available to the tab strip");
  seed.run('state.agent = "worker-one"; state.scope = "business"');
  const stale = seed.pane("worker-one", { workspaceRole: "global-worker", agentId: "agent-w1", isCurrent: () => true, initialView: "fake_review" });
  ok(!stale.views.includes("fake_review"), "a stale link or registration does not add the view");
  equal(seed.run("agpadRequestView(_agpad, 'fake_review')"), true, "an undeclared key waits on the declaration read");
  await flush();
  ok(!seed.byId.agpad.innerHTML.includes('data-v="fake_review"'), "no undeclared tab appears");

  // An undeclared key is never added as a tab by a request; shown directly it is an honest placeholder.
  equal(seed.run("agpadRequestView(_agpad, 'made_up_review')"), false, "an undeclared workspace key is not added");
  equal(seed.run("agpadRequestView(_agpad, 'Bad Key')"), false, "an invalid workspace key is refused");

  // ?workspace=<key> opens that view once and is consumed from the URL.
  seed.context.location.search = "?workspace=fake_review&agent=agent-w1";
  seed.context.location.href = "http://qorqut.test/?workspace=fake_review&agent=agent-w1#studio";
  equal(seed.run('agTakeLinkedWorkspace("#studio")'), "fake_review", "the deep link names the workspace to open");
  const consumed = seed.nav.urls.at(-1) || "";
  ok(consumed.includes("agent=agent-w1") && !consumed.includes("workspace="), "the consumed deep link leaves the URL");
  seed.context.location.search = "?workspace=fake_review";
  equal(seed.run('agTakeLinkedWorkspace("#other")'), null, "a deep link for another workspace is not consumed");
  seed.context.location.search = "";

  // The declaration exposes the registered view for its role only, leading the pane; disposal is registered.
  const declared = boot(declares([
    { key: "focus", display_name: "Focus", supported_roles: ["ordubashy"], renderer: "focus_board", presentation_actions: [] },
    fakeType,
    { key: "orphan_review", display_name: "Orphan", supported_roles: ["global-worker"], renderer: "orphan_board", presentation_actions: [] },
  ]));
  declared.run(FAKE);
  const declaredPane = declared.pane("worker-one", { workspaceRole: "global-worker", agentId: "agent-w1", isCurrent: () => true });
  await declared.run("workspaceTypesLoad()"); await flush();
  equal(declared.calls.filter(call => call.url.includes("/operations/workspace_types")).length, 1, "the declaration is read once per page load");
  equal(declared.calls.filter(call => call.url.endsWith("/root/extensions")).length, 1, "the extension manifest is read once per page load");
  equal(declared.run('workspaceViewsFor("global-worker").map(type=>type.key)'), ["fake_review", "orphan_review"], "the declaration decides the views");
  equal(declared.run('workspaceViewsFor("founder")'), [], "a role the declaration omits has no workspace");
  equal(declaredPane.views.join(","), "fake_review,inbox,viewbox,orphan_review,status", "a leading renderer's view comes first; others follow the base views");
  equal(declaredPane.view, "fake_review", "a pane that opened on the Focus fallback moves to the declared view once the declaration omits Focus for its role");
  ok(declared.body().innerHTML.includes("Fake board"), "the registered renderer mounts");
  equal(declared.run("fakeMounts"), 1, "mounted once");
  declared.run("agpadTick()");
  ok(declared.run("fakeTicks") > 0, "the registered tick runs");
  declared.run("agpadRequestView(_agpad, 'orphan_review')");
  ok(declared.body().innerHTML.includes("orphan_board"), "a declared type with no renderer shows the placeholder and its renderer name");
  declared.run("agpadDispose()");
  equal(declared.run("fakeDisposed"), 1, "disposal calls the registered callback");

  // Explicit server declarations stay authoritative over the Focus fallback.
  const emptied = boot(declares([]));
  emptied.pane("worker-one", { workspaceRole: "global-worker", agentId: "agent-w1", isCurrent: () => true });
  await emptied.run("workspaceTypesLoad()"); await flush();
  equal(emptied.run('workspaceViewsFor("global-worker")'), [], "an explicit empty declaration removes the Focus fallback for a global worker");
  equal(emptied.run('workspaceViewsFor("ordubashy")'), [], "an explicit empty declaration removes the Focus fallback for the coordinator");
  const noFocus = boot(declares([{ ...fakeType }]));
  noFocus.pane("worker-one", { workspaceRole: "global-worker", agentId: "agent-w1", isCurrent: () => true });
  await noFocus.run("workspaceTypesLoad()"); await flush();
  equal(noFocus.run('workspaceViewsFor("global-worker").map(type=>type.key)'), ["fake_review"], "a declaration omitting Focus does not retain the fallback");
  equal(noFocus.run('workspaceViewsFor("ordubashy")'), [], "a declaration omitting Focus gives the coordinator no workspace");
  const focusOrdubashyOnly = boot(declares([{ key: "focus", display_name: "Focus", supported_roles: ["ordubashy"], renderer: "focus_board", presentation_actions: [] }]));
  await focusOrdubashyOnly.run("workspaceTypesLoad()"); await flush();
  equal(focusOrdubashyOnly.run('workspaceViewsFor("global-worker")'), [], "a Focus declaration without global-worker is authoritative");

  // The manifest loader: absent, present, malformed and failing extensions (mocked GET /api/root/extensions).
  // A tiny synthetic extension script; real extension assets are tested in their own repositories.
  const SYNTH_JS = "/extensions/synth/assets/synth.js";
  const SYNTH_SOURCE = `registerWorkspaceRenderer("synth_board", { mount: p => { $("#agpad-body", p.el).innerHTML = "Synth board"; }, leading: true,
    views: { synth_review: { label: "Synth", icon: "<svg></svg>" } } })`;
  const paperType = { key: "synth_review", display_name: "Synth", supported_roles: ["global-worker"], renderer: "synth_board", presentation_actions: [] };
  const withManifest = (extensions, types, { failScripts = false } = {}) => {
    const app = boot(async url => url.endsWith("/root/extensions") ? { status: "ok", data: { extensions } }
      : url.endsWith("/operations/workspace_types") ? { status: "ok", data: { types } } : { status: "ok", data: {} });
    app.added = [];
    app.context.document.createElement = () => mkEl({});
    app.context.document.body.appendChild = node => {
      const src = node.src || node.href; app.added.push(src);
      if (failScripts && node.src) return void Promise.resolve().then(() => node.onerror());
      if (src === SYNTH_JS) vm.runInContext(SYNTH_SOURCE, app.context, { filename: "synth.js" });
      Promise.resolve().then(() => node.onload());
    };
    return app;
  };
  const paperManifest = { id: "synth", assets: { scripts: [SYNTH_JS], styles: ["/extensions/synth/assets/synth.css"] } };
  const bizPane = app => app.pane("worker-one", { workspaceRole: "global-worker", agentId: "agent-w1", isCurrent: () => true });

  const absent = withManifest([], [paperType]);
  bizPane(absent); await flush();
  equal(absent.added, [], "an empty manifest loads nothing");
  equal(absent.run('workspaceViewsFor("global-worker").map(type=>type.key)'), ["synth_review"], "the declaration alone still lists a declared type");
  ok(absent.byId.agpad.innerHTML.includes("data-v=\"synth_review\""), "a declared type without assets is listed, never rendered as loaded");
  absent.run("agpadRequestView(_agpad, 'synth_review')");
  ok(absent.body().innerHTML.includes("Unsupported workspace"), "a declared type with no loaded renderer shows the unsupported state");

  const baseline = boot(async () => ({ status: "error", error: { code: "not_found", message: "no" } }));
  bizPane(baseline); await flush();
  equal(baseline.run('agpadRequestView(_agpad, "synth_review")'), false, "a server without the endpoint is core-only and a stale extension link is refused");

  const present = withManifest([paperManifest], [paperType]);
  const presentPane = bizPane(present); await flush();
  equal(present.added, ["/extensions/synth/assets/synth.css", SYNTH_JS], "styles then script load from the extension path");
  equal(presentPane.views.join(","), "synth_review,inbox,viewbox,status", "The registered view leads the pane once declared and registered");
  ok(present.run("T_VIEW_LABEL.synth_review") === "Synth" && present.run("T_VIEW_ICON.synth_review").startsWith("<svg"), "the extension supplies its label and icon");
  present.run("agpadRequestView(_agpad, 'synth_review')"); await flush();
  ok(present.body().innerHTML.includes("Synth"), "the registered renderer mounts");
  present.run("workspaceTypesLoad(); extensionsLoad()"); await flush();
  equal(present.added.length, 2, "assets load once per page");

  const undeclared = withManifest([paperManifest], []);
  const undeclaredPane = bizPane(undeclared); await flush();
  ok(!undeclaredPane.views.includes("synth_review"), "loaded assets alone never add an extension view");
  const wrongRole = withManifest([paperManifest], [{ ...paperType, supported_roles: ["research"] }]);
  ok(!bizPane(wrongRole).views.includes("synth_review") , "a declared type for another role is not offered");

  const dup = withManifest([{ id: "synth", assets: { scripts: [SYNTH_JS, SYNTH_JS], styles: ["/extensions/synth/assets/synth.css", "/extensions/synth/assets/synth.css"] } }], [paperType]);
  bizPane(dup); await flush();
  equal(dup.added, ["/extensions/synth/assets/synth.css", SYNTH_JS], "duplicate script and style URLs append only once");

  const hostile = withManifest([
    { id: "paper", assets: { scripts: ["https://evil.example/x.js"] } },
    { id: "paper2", assets: { scripts: ["/extensions/other/assets/a.js"] } },
    { id: "paper3", assets: { scripts: ["/extensions/paper3/assets/../../x.js"] } },
    { id: "paper4", assets: { scripts: ["/extensions/paper4/assets/a.css"] } },
    { id: "ok", assets: { scripts: ["/extensions/ok/assets/a.js"] } },
    { id: "ok", assets: { scripts: ["/extensions/ok/assets/b.js"] } },
    { id: 7, assets: {} }, null,
  ], []);
  bizPane(hostile); await flush();
  equal(hostile.added, ["/extensions/ok/assets/a.js"], "remote, cross-extension, traversal, mistyped and duplicate entries are not loaded");

  const failing = withManifest([paperManifest], [paperType], { failScripts: true });
  bizPane(failing); await flush();
  failing.run("agpadRequestView(_agpad, 'synth_review')");
  ok(failing.body().innerHTML.includes("Unsupported workspace"), "a failed script load leaves the ordinary unsupported state");

  console.log(`${checks} workspace dispatch checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
