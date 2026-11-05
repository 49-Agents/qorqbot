// Unit coverage for the shared optimistic task state controller.
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../js/task-state.js"), "utf8");
const pending = () => { let resolve, reject; const promise = new Promise((r, j) => { resolve = r; reject = j; }); return {promise, resolve, reject}; };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const posts = [], reads = [], writes = [];
let readTask = {id: "a", status: "open", revision: 1}, mode = "ok", response, readGate = null;
const context = vm.createContext({
  post: async (path, body) => {
    posts.push({path, body});
    if (path.endsWith("tasks_get")) { reads.push({path, body}); if (readGate) return readGate.promise; return {status: "ok", data: {task: {...readTask}}}; }
    writes.push({path, body});
    if (mode === "conflict") return {status: "error", error: {code: "revision_conflict", message: "Changed elsewhere"}};
    if (mode === "accepted" || mode === "signoff_required") return {status: mode, data: {task: {...readTask, status: body.changes.status, revision: readTask.revision + 1}}};
    if (mode === "lost") return response.promise;
    return response ? response.promise : {status: "ok", data: {task: {...readTask, status: body.changes.status, revision: readTask.revision + 1}}};
  },
  coreData: result => { if (!result || !["ok", "accepted"].includes(result.status) || result.error) { const e = new Error(result?.error?.message || "failed"); e.code = result?.error?.code; throw e; } return result.data; },
  newRequestId: (() => { let n = 0; return () => `request-${++n}`; })(), setTimeout
});
vm.runInContext(source, context, {filename: "task-state.js"});
const run = code => vm.runInContext(code, context);
const task = (status = "open", revision = 1, id = "a") => ({id, status, revision, title: id});

(async () => {

let events = [];
run(`taskStateSubscribe((scope,id,snapshot)=>events.push({scope,id,snapshot}));`); context.events = events;
assert.equal(run(`taskStateObserve("acme", ${JSON.stringify(task())}).status`), "open");
assert.equal(events.length, 1, "first observation emits");
assert.equal(run(`taskStateObserve("acme", ${JSON.stringify(task())}).status`), "open");
assert.equal(events.length, 1, "identical refresh is quiet");

// Projection is immediate, and clicks serialize against the returned revision.
response = pending(); mode = "ok";
const first = run(`taskStateSet("acme", ${JSON.stringify(task())}, "doing")`);
assert.equal(run(`taskStateSnapshot("acme", "a").task.status`), "doing");
run(`taskStateSet("acme", {id:"a",status:"doing",revision:1}, "waiting"); taskStateSet("acme", {id:"a",status:"waiting",revision:1}, "blocked")`);
assert.equal(writes.length, 1); assert.equal(writes[0].body.expected_revision, 1);
readTask = task("doing", 2); const response1 = response; response = pending(); response1.resolve({status:"ok", data:{task:{...readTask}}}); await flush();
assert.equal(writes.length, 2); assert.equal(writes[1].body.expected_revision, 2); assert.equal(writes[1].body.changes.status, "blocked");
readTask = task("blocked", 3); const response2 = response; response2.resolve({status:"ok", data:{task:{...readTask}}}); await flush();
assert.equal((await first).task.status, "blocked");

// A projected object with the old revision cannot overwrite confirmed state.
assert.equal(run(`taskStateObserve("acme", {id:"a",status:"open",revision:3}).status`), "blocked");
assert.equal(run(`taskStateObserve("acme", {id:"a",status:"doing",revision:2}).status`), "blocked");

// A compact new projection still accepts missing display fields from an older
// full list response without reverting state/revision.
run('taskStateObserve(null, {id:"compact",status:"doing",revision:4})');
const compact=run('taskStateObserve(null, {id:"compact",status:"open",revision:3,title:"Read me",assignee_ids:[]})');
assert.equal(compact.status,'doing');assert.equal(compact.revision,4);
assert.equal(compact.title,'Read me');assert.equal(compact.assignee_ids.length,0);

// Global and a business named "global" are distinct scopes.
response = null; readTask = task("open", 4);
const globalResult = run(`taskStateSet(null, {id:"a",status:"open",revision:4}, "doing")`);
await flush(); await globalResult;
assert.match(posts.at(-1).path, /^\/root\/operations\/tasks_update$/);
assert.equal(run(`taskStateObserve("global", {id:"a",status:"open",revision:4}).status`), "open");
const businessGlobal = run(`taskStateSet("global", {id:"a",status:"open",revision:4}, "doing")`); await flush(); await businessGlobal;
assert.match(posts.at(-1).path, /^\/biz\/global\/operations\/tasks_update$/);

// Conflict and a lost response reconcile once and never replay queued intent.
mode = "conflict"; readTask = task("open", 5); response = null;
const conflictResult = run(`taskStateSet("acme", {id:"a",status:"open",revision:5}, "doing")`); await flush();
const conflictSnapshot = await conflictResult; assert.equal(conflictSnapshot.error.code, "revision_conflict"); assert.equal(writes.length, 5);
mode = "lost"; readTask = task("doing", 6); response = pending();
const lostResult = run(`taskStateSet("acme", {id:"a",status:"open",revision:5}, "doing")`); response.resolve({status:"error", error:{message:"timeout"}}); await flush();
assert.equal((await lostResult).error, null, "read-back confirms an uncertain mutation");

// Initial reads are shared while the entry is locked; a click during
// reconciliation is discarded instead of starting a second write.
mode = "ok"; readTask = task("open", 1, "c"); readGate = pending(); response = null;
const c1 = run(`taskStateSet("acme", {id:"c",status:"open"}, "doing")`);
const c2 = run(`taskStateSet("acme", {id:"c",status:"open"}, "waiting")`);
assert.equal(reads.filter(r => r.body.task_id === "c").length, 1);
readGate.resolve({status:"ok", data:{task:{...readTask}}}); readGate = null;
await c1; await c2; assert.equal(writes.filter(w => w.body.task_id === "c").length, 1);

mode = "conflict"; readTask = task("open", 1, "d"); readGate = pending();
const d1 = run(`taskStateSet("acme", {id:"d",status:"open",revision:1}, "doing")`); await flush();
run(`taskStateSet("acme", {id:"d",status:"open",revision:1}, "waiting")`);
readGate.resolve({status:"ok", data:{task:{...readTask}}}); readGate = null; await flush();
assert.equal((await d1).error.code, "revision_conflict");
assert.equal(writes.filter(w => w.body.task_id === "d").length, 1);

// A signoff_required accepted response is still a valid core response; a
// malformed/read failure becomes a visible error without claiming it saved.
mode = "signoff_required"; readTask = task("doing", 6);
const approval = run(`taskStateSet("acme", {id:"a",status:"doing",revision:6}, "waiting")`); await flush();
assert.equal((await approval).error.code, "signoff_required");
mode = "ok"; readTask = {id:"bad", status:"open"};
const bad = run(`taskStateSet("fresh", {id:"bad",status:"open"}, "doing")`); await flush();
assert.match((await bad).error.message, /invalid task/i);
assert.equal(typeof run("taskStateSubscribe"), "function", "controller is independent of navigation modules");
console.log("task state controller checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
