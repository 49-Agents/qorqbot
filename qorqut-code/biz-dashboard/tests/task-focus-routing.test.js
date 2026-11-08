'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
const read=n=>fs.readFileSync(require.resolve('../js/'+n+'.js'),'utf8');
const tasks=read('tasks'),start=tasks.indexOf('function taskRecordReference('),end=tasks.indexOf('function tControlPopover(',start);
const helper=tasks.slice(start,end);
const workspace=read('agent-workspace'),a=workspace.indexOf('async function agentOpenRecordReference('),b=workspace.indexOf('\n}',a)+2;
const reference=workspace.slice(a,b);
function harness(embedded=false){
 const calls=[],errors=[],business={id:'p1',slug:'project'};
 const ctx={state:{scope:'global',globalTab:'agents',biz:'project',bizs:[business]},embeddedAgentPane:embedded,parent:{agentOpenRecordReference:async ref=>{calls.push(ref);return true;}},
  loadGlobalAgentProfiles:async()=>[{agent_id:'ord',role:{id:'ordubashy'}}],globalRosterAgent:()=>true,selectGlobal:async()=>{},globalAgentsNavigate:async()=>true,
  _agpad:{agentId:'ord',view:'focus'},agfocusOpenPairedReference:async(p,ref)=>{calls.push(ref);return true;},toast:m=>errors.push(m)};
 vm.createContext(ctx);vm.runInContext(reference+'\n'+helper,ctx);return {ctx,calls,errors};
}
function plain(v){return JSON.parse(JSON.stringify(v));}
for(const embedded of [false,true])test(`project and Global details route canonical references (${embedded?'embedded':'normal'})`,async()=>{
 const {ctx,calls}=harness(embedded);
 assert.equal(await ctx.tDetail({id:'x',title:'Project task',business:{id:'p1',slug:'project'}}),true);
 assert.equal(await ctx.tDetail({id:'y',title:'Global task',business_id:null}),true);
 assert.deepEqual(plain(calls),[{type:'task',id:'x',business_id:'p1',business_slug:'project',label:'Project task',task_type:null},
  {type:'task',id:'y',business_id:null,business_slug:null,label:'Global task',task_type:null}]);
});
test('unknown or conflicting project identity cannot fall back to Global',async()=>{
 const {ctx,calls,errors}=harness();
 assert.equal(await ctx.taskOpenReference({id:'x',business_id:'missing'}),false);
 assert.equal(await ctx.taskOpenReference({id:'x',business_id:'missing',business_slug:'not-accessible'}),false);
 assert.equal(await ctx.taskOpenReference({id:'x',business_id:'p1',business_slug:'other'}),false);
 assert.equal(calls.length,0);assert.equal(errors.length,3);
});
test('progress references preserve scope and beforeOpen refusal',async()=>{
 const {ctx,calls}=harness();vm.runInContext(read('task-progress'),ctx);
 assert.equal(await ctx.taskProgressOpenReference('project',{type:'task',id:'x'},async()=>false),undefined);assert.equal(calls.length,0);
 assert.equal(await ctx.taskProgressOpenReference('project',{type:'task',id:'x'},async()=>true),true);
 assert.equal(await ctx.taskProgressOpenReference(null,{type:'task',id:'y'},async()=>true),true);
 assert.deepEqual(plain(calls).map(c=>[c.id,c.business_id,c.business_slug]),[['x','p1','project'],['y',null,null]]);
});
test('work links use canonical scope without a Task overlay',async()=>{
 const {ctx,calls}=harness();ctx.modal=()=>{throw Error('unexpected overlay');};
 const s=read('work-groups'),a=s.indexOf('async function workOpenLink('),b=s.indexOf('// An unsaved creation form',a);
 const c=s.indexOf('async function taskLinkOpen('),d=s.indexOf('// The shared chain-link glyph',c);
 vm.runInContext(s.slice(a,b)+'\n'+s.slice(c,d),ctx);
 await ctx.workOpenLink({biz:'project',error:e=>{throw e;}},{resource_type:'tasks',resource_id:'x'});
 await ctx.workOpenLink({biz:null,error:e=>{throw e;}},{resource_type:'tasks',resource_id:'y'});
 await ctx.taskLinkOpen({task_id:'z',business_id:'p1',business_slug:'project'});
 await ctx.taskLinkOpen({task_id:'old-global'});
 assert.deepEqual(plain(calls).map(c=>[c.id,c.business_id]),[['x','p1'],['y',null],['z','p1'],['old-global',null]]);
});

test('existing Focus record guard prevents displacement and retains the controls',async()=>{
 const source=read('agent-focus');const a=source.indexOf('async function agfocusWorkspaceMode('),b=source.indexOf('function agfocusClearSelection(',a);
 const classes=new Set(),controls={isConnected:true},dock={hidden:true},layout={scrollTop:12,classList:{add:v=>classes.add(v),remove:v=>classes.delete(v)}};
 const board={isConnected:true,classList:{add:v=>classes.add(v),remove:v=>classes.delete(v)},querySelector:s=>s==='.agfocus-dock'?dock:s==='.agfocus-layout'?layout:s==='.agfocus-dock-content'?{}:null};
 let open=true,allow=false;const prior={alive:()=>true,isOpen:()=>open,close:async()=>{if(allow)open=false;},dispose(){}};
 const pane={workspaceRole:'ordubashy',focusMount:board,focusWorkspace:prior,focusWorkspacePaired:false,controls};
 const make=new Function('$','recordWorkspaceMount','agfocusWorkspaceOptions',source.slice(a,b)+';return {agfocusWorkspaceMode,agfocusShowDock};');
 const methods=make((s,p)=>p.querySelector(s),()=>({alive:()=>true}),()=>({}));
 assert.equal(await methods.agfocusWorkspaceMode(pane,true),false);assert.equal(open,true);assert.equal(pane.focusWorkspace,prior);
 allow=true;assert.equal(await methods.agfocusWorkspaceMode(pane,true),true);methods.agfocusShowDock(pane);
 assert.equal(pane.controls,controls);assert.equal(controls.isConnected,true);assert.equal(dock.hidden,false);assert.ok(classes.has('agfocus-record-open'));
});
