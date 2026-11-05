#!/usr/bin/env node
// Synthetic Chat endpoints only; no real agents are messaged.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..'),calls=[],errors=[];
let failSend=false,refuse=null,holdSend=false,releaseSend=null,noBinding=false;
const bindings={chief:'bind-chief-1','global-worker':'bind-marketing-1'};
const ecosystem={kind:'ecosystem',allowed_business_ids:[],global_capabilities:[]};
const globalAgents=[
 {agent_id:'chief',name:'ordubashy',display_name:'Ordubashy',role:{id:'ordubashy'},scope:ecosystem},
 {agent_id:'global-worker',name:'marketing',display_name:'Marketing',role_id:'global-worker',scope:ecosystem},
 {agent_id:'worker-research',name:'research',display_name:'Research',role:{id:'global-worker'},scope:ecosystem},
 {agent_id:'founder',name:'founder',display_name:'Founder',role:{id:'founder'},scope:ecosystem},
 {agent_id:'applier',name:'learning-applier',display_name:'Learning Applier',role:{id:'learning-applier'},scope:ecosystem},
 {agent_id:'global-other',name:'helper',display_name:'Helper',role:{id:'coder'},scope:ecosystem},
 {agent_id:'stale-business',name:'stale',display_name:'Stale business record',role:{id:'ordubashy'},home_business_id:'b1',scope:{kind:'business',business_id:'b1'}},
];
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname.startsWith('/api/')){
  let raw='';for await(const chunk of req)raw+=chunk;
  const body=raw?JSON.parse(raw):null;calls.push({path:url.pathname,body});
  if(url.pathname==='/api/test/hold'){holdSend=true;res.writeHead(200,{'Content-Type':'application/json'}).end('{}');return;}
  if(url.pathname==='/api/test/state'){res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify({pending:!!releaseSend}));return;}
  if(url.pathname==='/api/test/release'){holdSend=false;releaseSend?.();releaseSend=null;res.writeHead(200,{'Content-Type':'application/json'}).end('{}');return;}
  if(url.pathname.endsWith('/stream')){res.writeHead(200,{'Content-Type':'text/event-stream'});res.write(': connected\n\n');return;}
  let result={status:'ok',data:{}};
  const chat=/^\/api\/root\/agents\/([^/]+)\/chat$/.exec(url.pathname);
  if(url.pathname==='/api/root/agents')result.data={agents:globalAgents};
  else if(chat)result.data={session:noBinding?null:{state:'live',can_send:true,binding:{binding_id:bindings[chat[1]] || 'bind-'+chat[1]}},conversation:null,entries:[]};
  else if(url.pathname.endsWith('/chat/messages')){
   if(holdSend)await new Promise(resolve=>{releaseSend=resolve;});
   result=refuse?{status:'error',error:{code:refuse,message:'Backend detail that must not be shown'}}
    :failSend?{status:'error',error:{code:'unavailable',message:'Lost response with backend detail'}}:{status:'ok',data:{input:{id:'input-1'}}};
  }
  res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(result));return;
 }
 if(url.pathname==='/'){
  res.setHeader('Content-Type','text/html');res.end(`<!doctype html><meta charset="utf-8"><button id="wsbtn" hidden></button><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/agents.css"><link rel="stylesheet" href="/css/xterm.css"><link rel="stylesheet" href="/css/workspace-composer.css"><style>#desktop{position:fixed;top:10px;right:10px;width:400px;height:120px;z-index:100}</style><div id="toasts"></div><div id="content"></div><div id="main-terminal" style="width:600px;height:300px"></div><iframe id="desktop" title="Desktop" srcdoc="<input aria-label='Desktop input'>"></iframe><script src="/js/xterm.js"></script><script src="/js/core.js"></script><script src="/js/san-juan-time.js"></script><script src="/js/agents.js"></script><script src="/js/terminal.js"></script><script src="/js/global-chat.js"></script><script src="/js/global-agents.js"></script><script src="/js/workspace-composer.js"></script><script>state.scope='business';state.biz='studio';workspaceComposerRefresh();</script>`);return;
 }
 const file=path.resolve(root,'.'+url.pathname);if(!file.startsWith(root+'/') || !fs.existsSync(file)){res.writeHead(404).end();return;}
 res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/plain');res.end(fs.readFileSync(file));
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}});page.setDefaultTimeout(7000);page.on('pageerror',e=>errors.push(e.message));
  const origin='http://127.0.0.1:'+server.address().port;await page.route('**/*',r=>r.request().url().startsWith(origin)?r.continue():r.abort());
  const messages=()=>calls.filter(c=>c.path.endsWith('/chat/messages'));
  await page.goto(origin);const dock=page.locator('#workspace-composer'),input=dock.getByRole('textbox',{name:'Message agent'}),picker=dock.locator('[data-recipient]');
  await page.waitForFunction(()=>!document.querySelector('[data-recipient]').disabled);
  // Business pages offer the registered global agents, every worker included, and nothing else: no business roster read, no unsupported or business-scoped record.
  await picker.click();
  assert.deepEqual(await page.getByRole('option').allInnerTexts(),['Ordubashy','Marketing','Research','Founder','Learning Applier']);
  assert(!calls.some(c=>/^\/api\/biz\/[^/]+\/agents/.test(c.path)));
  await page.getByRole('option').filter({hasText:'Marketing'}).click();
  await input.fill('Check campaign');await input.press('Control+Enter');assert.equal(messages().length,0);
  await input.press('Enter');await dock.getByText('Sent',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>document.activeElement===document.querySelector('#workspace-composer input')),true);
  // The message carries the binding just read from the agent's current Chat state, like the Chat view.
  assert.equal(messages()[0].path,'/api/root/agents/global-worker/chat/messages');
  assert.equal(messages()[0].body.text,'Check campaign');
  assert.equal(messages()[0].body.expected_binding,'bind-marketing-1');
  assert(calls.some(c=>c.path==='/api/root/agents/global-worker/chat'));
  // Drafts belong to their target.
  await input.fill('Marketing draft');await picker.click();await page.getByRole('option').filter({hasText:'Ordubashy'}).click();assert.equal(await input.inputValue(),'');
  // An unconfirmed send keeps its text, key and original binding; the retry reuses both even after the binding moved.
  await input.fill('Global note');failSend=true;await dock.getByRole('button',{name:'Send',exact:true}).click();
  await dock.getByText("Couldn't confirm. Try again",{exact:true}).waitFor();assert.equal(await input.inputValue(),'Global note');
  const first=messages().at(-1);assert.equal(first.body.expected_binding,'bind-chief-1');
  failSend=false;bindings.chief='bind-chief-2';await input.press('Enter');await dock.getByText('Sent',{exact:true}).waitFor();
  const retry=messages().at(-1);
  assert.equal(retry.body.idempotency_key,first.body.idempotency_key);assert.equal(retry.body.expected_binding,'bind-chief-1');
  // A new text reads the binding again; without one nothing is sent.
  noBinding=true;const beforeMissing=messages().length;
  await input.fill('No session yet');await input.press('Enter');
  await dock.getByText('Connecting. Try again in a moment.',{exact:true}).waitFor();
  assert.equal(messages().length,beforeMissing);noBinding=false;
  // A refusal shows short text, never the backend message, and the next send starts fresh.
  refuse='terminal_offline';await input.press('Enter');
  await dock.getByText('Not running. Start or resume it in Terminal, then send again.',{exact:true}).waitFor();
  assert.equal(await dock.locator('[role=status]').textContent(),'Not running. Start or resume it in Terminal, then send again.');
  const refused=messages().at(-1);refuse=null;await input.press('Enter');await dock.getByText('Sent',{exact:true}).waitFor();
  assert.notEqual(messages().at(-1).body.idempotency_key,refused.body.idempotency_key);
  assert.equal(messages().at(-1).body.expected_binding,'bind-chief-2');
  // Viewing the terminal stays opt-in and read-only from the composer: it never starts or types into one.
  await page.evaluate(()=>{mountTerminal('main',document.querySelector('#main-terminal'),null,'/root/terminal/main');window.mainTerm=_term;});
  await dock.getByRole('button',{name:'Show terminal'}).click();await page.waitForFunction(()=>workspaceComposer.dispose && document.querySelector('.workspace-terminal .xterm'));
  assert(calls.some(c=>c.path==='/api/root/terminal/chief/stream'));
  assert.equal(await page.evaluate(()=>_term===window.mainTerm),true);
  // A delayed delivery preserves a focus handoff to the desktop iframe.
  await input.fill('bottom handoff');
  await page.evaluate(()=>fetch('/api/test/hold',{method:'POST'}));
  await input.press('Enter');
  await page.waitForFunction(async()=>{const response=await fetch('/api/test/state');return (await response.json()).pending;});
  const desktopInput=page.frameLocator('#desktop').getByRole('textbox',{name:'Desktop input'});
  await desktopInput.click();
  await desktopInput.pressSequentially('typed in desktop');
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'desktop');
  await page.evaluate(()=>fetch('/api/test/release',{method:'POST'}));
  await dock.getByText('Sent',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>document.activeElement?.id),'desktop');
  assert.equal(await desktopInput.inputValue(),'typed in desktop');
  const handle=dock.getByRole('separator',{name:'Terminal height'}),terminal=dock.locator('.workspace-terminal');
  const before=(await terminal.boundingBox()).height,box=await handle.boundingBox();
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2,box.y+box.height/2-80);await page.mouse.up();
  assert(Math.abs((await terminal.boundingBox()).height-before-80)<2);
  await handle.focus();await handle.press('ArrowDown');assert(Math.abs((await terminal.boundingBox()).height-before-60)<2);
  assert.equal(Math.round((await dock.boundingBox()).width),728);
  await dock.getByRole('button',{name:'Hide terminal'}).click();assert.equal(await page.evaluate(()=>_term===window.mainTerm && workspaceComposer.dispose===null),true);
  await picker.click();await page.getByRole('option').filter({hasText:'Marketing'}).click();assert.equal(await input.inputValue(),'Marketing draft');
  // Global pages offer the same agents; the Agents page hides the composer and keeps drafts.
  await page.evaluate(()=>{state.scope='global';state.globalTab='costs';state.biz=null;workspaceComposerRefresh();});await page.waitForFunction(()=>!document.querySelector('[data-recipient]').disabled);
  await picker.click();assert.equal(await page.getByRole('option').count(),5);await page.getByRole('option').filter({hasText:'Founder'}).click();
  await input.fill('Keep global draft');
  await page.evaluate(()=>{state.globalTab='agents';workspaceComposerRefresh();});
  assert.equal(await dock.count(),0);
  await page.evaluate(()=>{state.globalTab='costs';workspaceComposerRefresh();});
  await page.waitForFunction(()=>!document.querySelector('#workspace-composer [data-recipient]').disabled);
  assert.equal(await input.inputValue(),'Keep global draft');
  assert(!calls.some(c=>/\/terminal\/.*\/(start|input)$/.test(c.path)),'the composer never starts or types into a terminal');
  assert(!calls.some(c=>c.path.startsWith('/api/biz/')));
  await page.setViewportSize({width:390,height:844});assert(await dock.evaluate(el=>el.getBoundingClientRect().right<=innerWidth && el.getBoundingClientRect().left>=0));
  await page.screenshot({path:'/tmp/workspace-composer-mobile.png'});assert.deepEqual(errors,[]);
  console.log('Workspace composer: global agents and workers, exact Chat binding, retry key and binding reuse, missing binding, terse refusals, drafts, focus handoff, opt-in terminal view and mobile layout passed.');
 }finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
