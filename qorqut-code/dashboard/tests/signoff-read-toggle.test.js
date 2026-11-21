'use strict';
const {test}=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm'), path=require('node:path');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('right-click on a Signoff chip toggles read and unread through the attention queue', async () => {
  const signoff={id:'s',title:'Review',state:'pending',proposal_revision:2,payload_hash:'h'},key='signoff:s:2:h';
  const reply={id:'reply:1',kind:'thread',source:{type:'signoff',id:'s'},viewed_ids:['reply:1']};
  let viewed=new Set();const posts=[];const last=()=>JSON.parse(JSON.stringify(posts.at(-1)));
  const rows=()=>({items:[{id:key,kind:'signoff',signoff_id:'s',viewed_ids:[key]},reply].filter(row=>!viewed.has(row.id)),side_chats:[]});
  const host={isConnected:true,innerHTML:'',hidden:false,listeners:{},contains:()=>true,addEventListener(type,fn){this.listeners[type]=fn;},removeEventListener(type,fn){if(this.listeners[type]===fn)delete this.listeners[type];}};
  const context={document:{addEventListener(){}},window:{addEventListener(){}},remAttempts:new Map(),remVerifiedOwner:async()=> 'owner',
    api:async url=>url.startsWith('/root/signoffs')?{items:[signoff]}:rows(),
    post:async(url,change)=>{posts.push(change);(change.viewed||[]).forEach(id=>viewed.add(id));(change.unviewed||[]).forEach(id=>viewed.delete(id));return rows();},
    AbortController,coreData:x=>x,esc:String,URLSearchParams,setTimeout:()=>0,clearTimeout(){},accessLost:()=>false,toast(){},Date};
  vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/attention-queue.js'),'utf8')+'\nthis.queue=attentionQueue;',context);
  const dispose=context.queue.mountSignoffs(host);await tick();await tick();
  const chip={dataset:{signoffKey:'s'},closest:selector=>selector==='[data-signoff-key]'?chip:null};
  const rightClick=()=>{const event={target:chip,currentTarget:host,preventDefault(){this.prevented=true;}};host.listeners.contextmenu(event);return event;};
  assert.match(host.innerHTML,/data-unread="true"/);assert.match(host.innerHTML,/Right-click to mark read/);
  // Read: clears the Signoff and the reply on it in one write, without deciding anything.
  assert.equal(rightClick().prevented,true);assert.match(host.innerHTML,/data-unread="false"/);await tick();await tick();
  assert.deepEqual(last(),{viewed:[key,'reply:1']});assert.match(host.innerHTML,/Right-click to mark unread/);
  // Unread: shown at once, and the server forgets only the Signoff's current version.
  rightClick();assert.match(host.innerHTML,/data-unread="true"/);await tick();await tick();
  assert.deepEqual(last(),{unviewed:[key]});assert.match(host.innerHTML,/data-unread="true"/);
  // And read again.
  rightClick();await tick();await tick();assert.deepEqual(last(),{viewed:[key]});assert.match(host.innerHTML,/data-unread="false"/);
  // A right-click elsewhere in the strip is left to the browser.
  const other={target:{closest:()=>null},currentTarget:host,preventDefault(){throw Error('intercepted');}};host.listeners.contextmenu(other);
  dispose();
});
