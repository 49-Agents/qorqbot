'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
const read=n=>fs.readFileSync(require.resolve('../js/'+n+'.js'),'utf8');
const tasks=read('tasks'),start=tasks.indexOf('function taskRecordReference('),end=tasks.indexOf('function tControlPopover(',start);
const helper=tasks.slice(start,end);
const workspace=read('agent-workspace'),a=workspace.indexOf('async function agentOpenRecordReference('),b=workspace.indexOf('\n}',a)+2;
const reference=workspace.slice(a,b);
function harness(embedded=false){
 const calls=[],errors=[],project={id:'p1',slug:'project'};
 const ctx={state:{scope:'global',globalTab:'agents',project:'project',projects:[project]},embeddedAgentPane:embedded,parent:{agentOpenRecordReference:async ref=>{calls.push(ref);return true;}},
  loadGlobalAgentProfiles:async()=>[{agent_id:'ord',role:{id:'ordubashy'}}],globalRosterAgent:()=>true,selectGlobal:async()=>{},globalAgentsNavigate:async(id,{view})=>{ctx._agpad.view=view;return true;},
  _agpad:{agentId:'ord',view:'focus'},agfocusOpenRecord:async(p,ref)=>{calls.push(ref);return true;},toast:m=>errors.push(m)};
 vm.createContext(ctx);vm.runInContext(reference+'\n'+helper,ctx);return {ctx,calls,errors};
}
function plain(v){return JSON.parse(JSON.stringify(v));}
for(const embedded of [false,true])test(`project and Global details route canonical references (${embedded?'embedded':'normal'})`,async()=>{
 const {ctx,calls}=harness(embedded);
 assert.equal(await ctx.tDetail({id:'x',title:'Project task',project:{id:'p1',slug:'project'}}),true);
 assert.equal(await ctx.tDetail({id:'y',title:'Global task',project_id:null}),true);
 assert.deepEqual(plain(calls),[{type:'task',id:'x',project_id:'p1',project_slug:'project',label:'Project task',task_type:null},
  {type:'task',id:'y',project_id:null,project_slug:null,label:'Global task',task_type:null}]);
});
test('unknown or conflicting project identity cannot fall back to Global',async()=>{
 const {ctx,calls,errors}=harness();
 assert.equal(await ctx.taskOpenReference({id:'x',project_id:'missing'}),false);
 assert.equal(await ctx.taskOpenReference({id:'x',project_id:'missing',project_slug:'not-accessible'}),false);
 assert.equal(await ctx.taskOpenReference({id:'x',project_id:'p1',project_slug:'other'}),false);
 assert.equal(calls.length,0);assert.equal(errors.length,3);
});
test('progress references preserve scope and beforeOpen refusal',async()=>{
 const {ctx,calls}=harness();vm.runInContext(read('task-progress'),ctx);
 assert.equal(await ctx.taskProgressOpenReference('project',{type:'task',id:'x'},async()=>false),undefined);assert.equal(calls.length,0);
 assert.equal(await ctx.taskProgressOpenReference('project',{type:'task',id:'x'},async()=>true),true);
 assert.equal(await ctx.taskProgressOpenReference(null,{type:'task',id:'y'},async()=>true),true);
 assert.deepEqual(plain(calls).map(c=>[c.id,c.project_id,c.project_slug]),[['x','p1','project'],['y',null,null]]);
});
test('work links preserve canonical scope through the shared task opener',async()=>{
 const {ctx,calls}=harness();ctx.modal=()=>{throw Error('unexpected overlay');};
 const s=read('work-groups'),a=s.indexOf('async function workOpenLink('),b=s.indexOf('// An unsaved creation form',a);
 const c=s.indexOf('async function taskLinkOpen('),d=s.indexOf('// The shared chain-link glyph',c);
 vm.runInContext(s.slice(a,b)+'\n'+s.slice(c,d),ctx);
 await ctx.workOpenLink({project:'project',error:e=>{throw e;}},{resource_type:'tasks',resource_id:'x'});
 await ctx.workOpenLink({project:null,error:e=>{throw e;}},{resource_type:'tasks',resource_id:'y'});
 await ctx.taskLinkOpen({task_id:'z',project_id:'p1',project_slug:'project'});
 await ctx.taskLinkOpen({task_id:'old-global'});
 assert.deepEqual(plain(calls).map(c=>[c.id,c.project_id]),[['x','p1'],['y',null],['z','p1'],['old-global',null]]);
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

// The Todo board is gone: a Task reference opens its record in Ordubashy's Focus, like every other record type.
test('task links and other record links open in Focus',async()=>{
 const {ctx}=harness();const opens=[],views=[];
 const navigate=ctx.globalAgentsNavigate;ctx.globalAgentsNavigate=async(id,options)=>{views.push(options.view);return navigate(id,options);};
 ctx.agfocusOpenRecord=async(p,ref)=>{opens.push([p.view,ref.type,ref.id]);return true;};
 assert.equal(await ctx.agentOpenRecordReference({type:'task',id:'task1',project_id:null}),true);
 assert.equal(await ctx.agentOpenRecordReference({type:'table',id:'table1',project_id:null}),true);
 assert.deepEqual(views,['focus','focus']);
 assert.deepEqual(opens,[['focus','task','task1'],['focus','table','table1']]);
 assert.doesNotMatch(reference,/attentionBoard/);
});
test('a task reference inside Focus opens in the Focus record workspace, not a board',async()=>{
 const source=read('agent-focus'),start=source.indexOf('async function agfocusOpenPairedReference('),end=source.indexOf('function agfocusActive(',start);
 assert.ok(start>=0 && end>start);
 const ctx={toast:()=>{},_agpad:null,agpadSurfaceClose:async()=>{},agentOpenRecordReference:async()=>false,
  rwRecordFromItem:({source})=>({source,key:source.type+':'+source.id}),agfocusWorkspaceMode:async()=>true,agfocusShowDock:()=>{}};
 vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
 const navigated=[];const pane={focusWorkspace:{alive:()=>true,navigate:async(target,opts)=>{navigated.push([target.source.type,target.source.id,opts.parentTable]);return true;}}};
 assert.equal(await ctx.agfocusOpenRecord(pane,{type:'task',id:'task1',project_id:null}),true);
 assert.deepEqual(plain(navigated),[['task','task1',null]]);
 ctx.rwRecordFromItem=({source})=>({source,scopeMissing:true});
 assert.equal(await ctx.agfocusOpenRecord(pane,{type:'task',id:'task2',project_id:'gone'}),false);
 assert.equal(navigated.length,1);
});
test('native Chat task references use the shared thread opener and ignore stale mounts',async()=>{
 const {ctx}=harness();const opens=[];
 ctx._agpad.open=true;ctx._agpad.workspaceRole='ordubashy';
 const source=read('global-chat'),start=source.indexOf('async function globalChatOpenFocusReference('),end=source.indexOf('function globalChatAccept(',start);
 vm.runInContext(source.slice(start,end),ctx);
 ctx.agentOpenRecordReference=async ref=>{opens.push(plain(ref));return true;};
 const ref={type:'task',id:'task1',project_id:'p1',project_slug:'project'};
 assert.equal(await ctx.globalChatOpenFocusReference(ref,{pane:ctx._agpad,agentId:'ord'}),true);
 assert.deepEqual(opens,[ref]);
 assert.equal(await ctx.globalChatOpenFocusReference(ref,{pane:ctx._agpad,agentId:'other'}),false);
 assert.equal(await ctx.globalChatOpenFocusReference(ref,{pane:ctx._agpad,isCurrent:()=>false}),false);
 assert.equal(opens.length,1);
});
test('a cancelled pane navigation never opens the task thread',async()=>{
 const {ctx,calls}=harness();ctx.globalAgentsNavigate=async()=>false;
 assert.equal(await ctx.agentOpenRecordReference({type:'task',id:'task1',project_id:null}),false);
 assert.equal(calls.length,0);
});
test('task references inside a paired record also use the normal thread opener',async()=>{
 const {ctx}=harness();const opens=[];
 ctx.agentOpenRecordReference=async ref=>{opens.push(plain(ref));return true;};
 const source=read('agent-focus'),start=source.indexOf('async function agfocusOpenPairedReference('),end=source.indexOf('// Unread state',start);
 vm.runInContext(source.slice(start,end),ctx);
 const ref={type:'task',id:'task1',project_id:'p1',project_slug:'project'};
 assert.equal(await ctx.agfocusOpenPairedReference(ctx._agpad,ref),true);
 assert.deepEqual(opens,[ref]);
});
