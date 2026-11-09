'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const tasksSource=fs.readFileSync(require.resolve('../js/tasks.js'),'utf8');
const stateSource=fs.readFileSync(require.resolve('../js/task-state.js'),'utf8');
const between=(from,to)=>{const a=tasksSource.indexOf(from),b=tasksSource.indexOf(to,a);assert.ok(a>=0&&b>a,from);return tasksSource.slice(a,b);};
const line=prefix=>tasksSource.split('\n').find(l=>l.startsWith(prefix));
const slices=[line('let tGen = 0;'),line('let tPersonalLoaded'),line('let tPrincipal'),
 between('const tAssignees','const tClosed'),between('const tBusiness =','const tPersonalRows'),between('const tBiz =','// the business owner'),line('const tHuman'),
 between('let tRefreshPending','async function tFetchTask'),between('function tRelease()','// Reload the lists'),between('// Reload the lists without','// ---------------------------------------------------------------- search')].join('\n');

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const bizA={slug:'a',id:'ba',owner:'ann'},bizB={slug:'b',id:'bb',owner:'bob'};
const row=(id,extra={})=>({id,title:'old '+id,status:'open',revision:1,business_id:'ba',assignee_ids:[],...extra});
const canon=(id,extra={})=>({id,title:'new '+id,status:'open',revision:2,business_id:'ba',assignee_ids:[],owners:[],...extra});

function setup({principal='p1',personal=[],tasks=[]}={}){
 const gets=[],posts=[],toasts=[];
 const state={biz:'a',scope:'biz',bizs:[bizA,bizB],tasks,tasksBiz:'a',tasksError:null,myTasks:personal};
 let sig=0;
 const ctx={state,embeddedTaskPane:false,document:{activeElement:null,querySelector:()=>null},globalAgentProfiles:[],
  loadGlobalAgentProfiles:()=>Promise.resolve(),
  api:path=>{const d=deferred();gets.push({path,...d});return d.promise;},
  post:(path,body)=>{const d=deferred();posts.push({path,body,...d});return d.promise;},
  toast:m=>toasts.push(m),uiFailure:e=>e.message,tPaintSig:null,tVisSig:()=>String(++sig),paintCount:0,businessPaints:0,
  paintTasks(){ctx.paintCount++;},businessTasksPaint(){ctx.businessPaints++;},
  coreData:x=>x,newRequestId:()=>'k',setTimeout,clearTimeout,Promise};
 vm.createContext(ctx);
 vm.runInContext(stateSource,ctx);vm.runInContext(slices,ctx);
 vm.runInContext(`tPersonalLoaded=true;tPrincipal=${JSON.stringify(principal)};`,ctx);
 return {ctx,state,gets,posts,toasts,run:code=>vm.runInContext(code,ctx),
  settle:()=>{const open=gets.splice(0);return open;}};
}
const titles=list=>Array.from(list,t=>t.title),ids=list=>Array.from(list,t=>t.id);
const unhandled=[];process.on('unhandledRejection',e=>unhandled.push(e));

test('twrite resolves on the mutation response while full-list reads are held; canonical row is visible and stale reads cannot erase it',async()=>{
 const h=setup({tasks:[row('t1')],personal:[{...row('t1',{assignee_ids:['p1']}),business:bizA}]});
 h.ctx.refreshTasks();                       // a read already in flight when the write starts
 const early=h.gets.splice(0);
 assert.equal(early.length,2);
 let result;const write=h.ctx.twrite(h.state.tasks[0],{title:'new t1'},()=>assert.fail('no error')).then(r=>{result=r;});
 await flush();assert.equal(result,undefined,'not reported before the server acknowledges');
 assert.equal(h.state.tasks[0].title,'old t1');
 h.posts[0].resolve({task:canon('t1',{assignee_ids:['p1']})});
 await write;
 assert.equal(result.task.title,'new t1');
 assert.deepEqual(titles(h.state.tasks),['new t1']);assert.deepEqual(titles(h.state.myTasks),['new t1']);
 assert.equal(h.state.tasks[0].business.slug,'a');
 assert.ok(h.ctx.paintCount>0&&h.ctx.businessPaints>0);
 assert.equal(h.gets.length,2,'background refresh started, still pending');
 early[0].resolve({tasks:[row('t1')],principal_id:'p1'});early[1].resolve([row('t1')]);await flush();
 assert.deepEqual(titles(h.state.tasks),['new t1'],'a read that began before the acknowledgement is discarded');
 h.gets[0].resolve({tasks:[canon('t1',{revision:3,title:'server t1'})],principal_id:'p1'});h.gets[1].resolve([canon('t1',{revision:3,title:'server t1'})]);await flush();
 assert.deepEqual(titles(h.state.tasks),['server t1']);
});

test('error and signoff outcomes never patch as committed and return without awaiting reloads',async()=>{
 for(const reply of [{error:{message:'nope'}},{status:'signoff_required',task:canon('t1',{title:'pending approval'})}]){
  const h=setup({tasks:[row('t1')]});let failures=0;
  const write=h.ctx.twrite(h.state.tasks[0],{title:'x'},()=>failures++);
  h.posts[0].resolve(reply);
  assert.equal(await write,null);                 // GETs are never resolved
  assert.equal(failures,reply.error?1:0);
  assert.deepEqual(titles(h.state.tasks),['old t1']);
  assert.equal(h.gets.length,2);
 }
 const h=setup({tasks:[row('t1')]});let failures=0;
 const write=h.ctx.twrite(h.state.tasks[0],{},()=>failures++);h.posts[0].reject(Object.assign(new Error('boom'),{code:'x'}));
 assert.equal(await write,null);assert.equal(failures,1);assert.deepEqual(h.toasts,['boom']);
});

test('a background read failure neither fails the committed save nor leaves an unhandled rejection',async()=>{
 const h=setup({tasks:[row('t1')]});let failures=0;
 const write=h.ctx.twrite(h.state.tasks[0],{title:'new t1'},()=>failures++);
 h.posts[0].resolve({task:canon('t1')});
 const r=await write;
 for(const g of h.gets)g.reject(new Error('offline'));await flush();await flush();
 assert.equal(r.task.title,'new t1');assert.equal(failures,0);assert.deepEqual(h.toasts,[]);
 assert.deepEqual(unhandled,[]);
 assert.deepEqual(titles(h.state.tasks),['new t1']);
});

test('navigating A to B before the create is acknowledged never inserts the task into B',async()=>{
 const h=setup({tasks:[row('a1')],personal:[]});
 const add=h.ctx.tadd({title:'created'});
 assert.equal(h.posts[0].path,'/biz/a/task');assert.equal(h.posts[0].body.owner,'human:ann');
 h.state.biz='b';h.state.tasksBiz='b';h.state.tasks=[row('b1',{business_id:'bb'})];
 h.posts[0].resolve({task:canon('c1',{title:'created',assignee_ids:['p1']})});
 const r=await add;
 assert.equal(r.task.id,'c1');
 assert.deepEqual(ids(h.state.tasks),['b1']);
 assert.equal(ids(h.state.myTasks).join(),'c1');
 assert.equal(h.state.myTasks[0].business.slug,'a');
});

test('a created task joins the selected business list when it belongs to the initiating business',async()=>{
 const h=setup({tasks:[row('a1')]});
 const add=h.ctx.tadd({title:'created'});h.posts[0].resolve({task:canon('c1',{title:'created'})});await add;
 assert.deepEqual(ids(h.state.tasks),['a1','c1']);assert.equal(h.state.tasks[1].business.slug,'a');
});

test('responses for one task arriving in reverse revision order keep the newest',async()=>{
 const h=setup({tasks:[row('t1')]});
 const first=h.ctx.twrite('t1',{title:'second'}),second=h.ctx.twrite('t1',{title:'third'});
 h.posts[1].resolve({task:canon('t1',{revision:3,title:'third'})});await second;
 h.posts[0].resolve({task:canon('t1',{revision:2,title:'second'})});await first;
 assert.equal(h.state.tasks[0].revision,3);assert.equal(h.state.tasks[0].title,'third');
});

test('personal membership follows the known principal and waits for refresh when identity is unknown',async()=>{
 const mine=()=>({...row('t1',{assignee_ids:['p1']}),business:bizA});
 let h=setup({principal:'p1',tasks:[row('t1'),row('t2')],personal:[mine()]});
 let w=h.ctx.twrite('t1',{}); h.posts[0].resolve({task:canon('t1',{assignee_ids:['p2']})});await w;
 assert.deepEqual(ids(h.state.myTasks),[],'reassigned away: removed');
 w=h.ctx.twrite('t2',{});h.posts[1].resolve({task:canon('t2',{assignee_ids:['p1']})});await w;
 assert.deepEqual(ids(h.state.myTasks),['t2'],'assigned to the principal: added');

 h=setup({principal:null,tasks:[row('t1'),row('t2')],personal:[mine()]});
 w=h.ctx.twrite('t1',{});h.posts[0].resolve({task:canon('t1',{assignee_ids:['p2']})});await w;
 assert.deepEqual(titles(h.state.myTasks),['new t1'],'existing row updated, membership left to refresh');
 w=h.ctx.twrite('t2',{});h.posts[1].resolve({task:canon('t2',{assignee_ids:['p1']})});await w;
 assert.deepEqual(ids(h.state.myTasks),['t1']);

 h=setup({principal:null});
 h.run('tPrincipal=null');const p=h.ctx.refreshTasks();h.gets[0].resolve({tasks:[],principal_id:'p9'});h.gets[1].resolve([]);await p;
 assert.equal(h.run('tPrincipal'),'p9');
});

test('absent, malformed or mismatched canonical data skips the patch and falls back to a background refresh',async()=>{
 for(const task of [undefined,{title:'no id',revision:2},canon('t1',{revision:undefined}),canon('other'),canon('t1',{business_id:'bb'})]){
  const h=setup({tasks:[row('t1')]});
  const w=h.ctx.twrite('t1',{title:'new t1'});h.posts[0].resolve({task,ok:true});
  const r=await w;
  assert.ok(r);assert.deepEqual(titles(h.state.tasks),['old t1']);assert.equal(h.gets.length,2);
 }
});
