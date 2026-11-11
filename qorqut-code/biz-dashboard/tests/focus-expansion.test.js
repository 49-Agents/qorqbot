'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../js/agentpane.js'), 'utf8');
const code = source.slice(source.indexOf('// ------------------------------------------------------------ Focus nested surfaces'), source.indexOf('function agpadVisibility('));
function node() {
  return {isConnected:true, hidden:false, children:[], className:'', classList:{add(){},remove(){}},
    replaceChildren(...children){this.children.forEach(c=>c.isConnected=false); this.children=children;}, setAttribute(){}, focus(){this.focused=true;}};
}
function harness({active=true}={}) {
  const body=node(), title=node(), close=node(), layout=node(), shell=node(), board=node(), opener=node();
  shell.querySelector=s=>s==='.agfocus-surface-body'?body:s==='.agfocus-surface-title'?title:s==='[data-surface-close]'?close:null;
  board.querySelector=s=>s===':scope > .agfocus-surface'?shell:s==='.agfocus-layout'?layout:null;
  const p={agentId:'a', agent:'A', open:true, view:'focus', views:['focus','browser','desktop'],workspaceRole:'ordubashy',focusMount:board,el:node()};
  let disposed=0, browser=0, desktop=0;
  const context={window:{},document:{activeElement:opener,createElement:node}, _agpad:active?p:null,state:{scope:active?'global':'business',globalTab:'agents'},
    AGPAD_FOCUS_SURFACES:new Set(['browser','desktop','viewbox']),agentPaneViewAllowed:()=>true,agpadFocusSurfaces:q=>q.views.includes('focus'),
    agpadActive:q=>context._agpad===q, $:(selector,root)=>root?.querySelector?.(selector) || null, agpadRememberView(){},agpadAnimateContent(){},agfocusPaint(){},
    agfocusDesktopMount(){desktop++;},agbrowserMount(){browser++;},agpadModeKey:()=>null,toast(){},
    loadGlobalAgentProfiles:async()=>[{agent_id:'a',role:{id:'ordubashy'}}],globalRosterAgent:()=>true,
    selectGlobal:async()=>{context.state.scope='global';},globalAgentsNavigate:async()=>{context._agpad=p;},
    agpadSetView:async(q,view)=>{q.view=view;},ls(){}};
  vm.createContext(context);vm.runInContext(code,context);
  return {context,p,body,board,shell,close,opener,get disposed(){return disposed;},dispose:()=>disposed++,get browser(){return browser;},get desktop(){return desktop;}};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('active and owner-wide opening, guard, close, focus and disposal',async()=>{
  for(const active of [true,false]) {
    const h=harness({active}); let session;
    session=await h.context.window.focusExpansion.open({title:'Example',mount(host,s){assert.equal(host,s.host);s.onDispose(h.dispose);}});
    assert.ok(session?.isCurrent()); assert.equal(h.shell.hidden,false);
    session.setBeforeLeave(async()=>false);
    assert.equal(await session.close(),false); assert.ok(session.isCurrent());
    session.setBeforeLeave(async()=>true);
    assert.equal(await session.close(),true); assert.equal(h.disposed,1);assert.equal(h.opener.focused,true);
    assert.equal(await session.close(),false);assert.equal(h.disposed,1);
  }
});

test('Browser and Desktop respect refusal and remain usable after permission',async()=>{
  const h=harness();const session=await h.context.window.focusExpansion.open({mount(){}});
  session.setBeforeLeave(async()=>false);await h.context.agpadSurfaceRequest(h.p,'browser');assert.ok(session.isCurrent());assert.equal(h.browser,0);
  session.setBeforeLeave(async()=>true);await h.context.agpadSurfaceRequest(h.p,'browser');assert.equal(h.browser,1);assert.equal(session.isCurrent(),false);
  await h.context.agpadSurfaceRequest(h.p,'desktop');assert.equal(h.desktop,1);
});

test('record guard refuses displacement and accepts only after record closes',async()=>{
  const h=harness(); let open=true,allow=false;h.p.focusWorkspace={isOpen:()=>open,close:async()=>{if(allow)open=false;}};
  assert.equal(await h.context.window.focusExpansion.open({mount(){throw Error('must not mount');}}),null);
  assert.equal(open,true);allow=true;assert.ok(await h.context.window.focusExpansion.open({mount(){}}));
});

test('stale mount is detached and disposed once; newer content stays',async()=>{
  const h=harness();let release,oldHost;const first=h.context.window.focusExpansion.open({mount(host){oldHost=host;return new Promise(r=>release=r);}});
  await tick();const second=await h.context.window.focusExpansion.open({mount(){}});assert.ok(second.isCurrent());
  release(h.dispose);assert.equal(await first,null);assert.equal(oldHost.isConnected,false);assert.equal(h.disposed,1);assert.ok(second.isCurrent());
});

test('stale navigation does not open after pane changes',async()=>{
  const h=harness({active:false});let release;h.context.loadGlobalAgentProfiles=()=>new Promise(r=>release=r);
  const result=h.context.window.focusExpansion.open({mount(){throw Error('must not mount');}});
  h.context.state.globalTab='calendar';release([{agent_id:'a',role:{id:'ordubashy'}}]);assert.equal(await result,null);
});


test('Escape closes the expansion and respects its leave guard',async()=>{
  const h=harness();const session=await h.context.window.focusExpansion.open({mount(){}});
  const focus=fs.readFileSync(require.resolve('../js/agent-focus.js'),'utf8');
  const start=focus.indexOf('  const escape=event=>{'),end=focus.indexOf("  document.addEventListener('keydown',escape);",start);
  assert.ok(start>=0 && end>start);
  const escape=new Function('p','document','agpadSurfaceClose',focus.slice(start,end)+';return escape;')(h.p,{querySelector:()=>null},h.context.agpadSurfaceClose);
  let prevented=0; const event={key:'Escape',preventDefault(){prevented++;},stopPropagation(){}};
  session.setBeforeLeave(async()=>false);escape(event);await tick();assert.ok(session.isCurrent());
  session.setBeforeLeave(async()=>true);escape(event);await tick();assert.equal(session.isCurrent(),false);assert.equal(prevented,2);
});

test('app navigation uses the same expansion and record guards',async()=>{
  const h=harness();const session=await h.context.window.focusExpansion.open({mount(){}});
  const workspace=fs.readFileSync(require.resolve('../js/agent-workspace.js'),'utf8');
  const start=workspace.indexOf('async function agentWorkspaceCanLeave(p)'),end=workspace.indexOf('async function agentWorkspaceLeaveEmbedded()',start);
  assert.ok(start>=0 && end>start);
  const canLeave=new Function('agpadFocusCanLeave',workspace.slice(start,end)+';return agentWorkspaceCanLeave;')(h.context.agpadFocusCanLeave);
  session.setBeforeLeave(async()=>false);assert.equal(await canLeave(h.p),false);assert.ok(session.isCurrent());
  session.setBeforeLeave(async()=>true);assert.equal(await canLeave(h.p),true);
});

test('exact agent rejects an unavailable identity',async()=>{
  const h=harness();let mounted=false;
  assert.equal(await h.context.window.focusExpansion.open({agentId:'missing',mount(){mounted=true;}}),null);assert.equal(mounted,false);
});

test('repeated Close and switch share a pending discard decision',async()=>{
  const h=harness();let release,calls=0;const session=await h.context.window.focusExpansion.open({mount(){}});
  session.setBeforeLeave(()=>{calls++;return new Promise(r=>release=r);});
  const first=session.close();await tick();const second=session.close();const switchView=h.context.agpadSurfaceRequest(h.p,'browser');
  release(true);await Promise.all([first,second,switchView]);
  assert.equal(calls,1);assert.equal(h.p.surface,null);assert.equal(h.browser,0);assert.equal(session.isCurrent(),false);
});

test('embedded documents reject cross-document mount callbacks',async()=>{
  const h=harness();h.context.embeddedAgentPane=true;let mounted=false;
  assert.equal(await h.context.window.focusExpansion.open({mount(){mounted=true;}}),null);assert.equal(mounted,false);
});

test('request view waits for a guarded surface and reports the shown view',async()=>{
  const h=harness();const s=fs.readFileSync(require.resolve('../js/agentpane.js'),'utf8');
  const a=s.indexOf('function agpadRequestView('),b=s.indexOf('function agpadWorkspaceTick(',a);
  h.context.workspaceType=()=>true;h.context.agpadSetView=(p,view)=>h.context.agpadSurfaceRequest(p,view);
  vm.runInContext(s.slice(a,b),h.context);
  const session=await h.context.window.focusExpansion.open({mount(){}});let release;
  session.setBeforeLeave(()=>new Promise(r=>release=r));const request=h.context.agpadRequestView(h.p,'browser');await tick();release(false);
  assert.equal(await request,false);assert.ok(session.isCurrent());
  session.setBeforeLeave(async()=>true);assert.equal(await h.context.agpadRequestView(h.p,'browser'),true);
});

test('global navigation returns the actual asynchronous result',async()=>{
  const s=fs.readFileSync(require.resolve('../js/global-agents.js'),'utf8');
  const a=s.indexOf('async function globalAgentsNavigate('),b=s.indexOf('const GLOBAL_WORKSPACE_TERMINAL',a);
  let release;const root={openAgent:()=>new Promise(r=>release=r)};
  const nav=new Function('document',s.slice(a,b)+';return globalAgentsNavigate;')({querySelector:()=>root});
  const first=nav('a');release(false);assert.equal(await first,false);
  const second=nav('b');release(true);assert.equal(await second,true);
});
