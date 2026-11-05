#!/usr/bin/env node
// Exact Signoff proposal content (WhatsApp, account email, generic changes) and Review-before-Approve. Synthetic fixtures only: no live API, recipient or provider.
// PLAYWRIGHT_MODULE=/path/to/playwright SIGNOFF_PREVIEW_SCREENSHOTS=/dir node tests/test_signoff_previews_browser.js
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const ROOT = path.join(__dirname, "..");
const SHOTS = process.env.SIGNOFF_PREVIEW_SCREENSHOTS || "";
const clone = value => JSON.parse(JSON.stringify(value));
const OWNER = "40000000-0000-4000-8000-000000000001";
const BIZ = "10000000-0000-4000-8000-000000000001";
const session = { principal_id: OWNER, principal_kind: "human", display_name: "Owner", is_owner: true };
const envelope = data => ({ request_id: "fixture", status: "ok", data, warnings: [] });
const reply = (res, body, status = 200) => { res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" }); res.end(JSON.stringify(body)); };
const id = n => `30000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const fixtureField = (p, after, before = null) => ({ path: p, before, after });
const resource = (type, label) => ({ resource_type: type, resource_id: type + "-1", label });
function record(n, title, operation, changes, effects = []) {
  return { id: id(n), review_business_id: null, proposal_revision: 1, payload_hash: String(n).repeat(64).slice(0, 64), status_revision: 1, state: "pending", decision_state: "pending",
    operation_name: operation, operation_version: 1, category: "internal_change", proposer_principal_id: OWNER, proposer_display_name: "Onbashy", proposer_agent_id: null,
    title, affected_scope: { kind: "global" }, created_at: "2026-09-07T12:00:00Z", updated_at: "2026-09-07T12:00:00Z", updated_sequence: n, decision: null, supersedes_id: null, superseded_by_id: null,
    preview: { title, rationale: "Owner asked for this.", affected_scope: { kind: "global" }, resources: [], changes, effects }, outcome: null };
}
const wa = (chatId, name, text, extra = []) => record(1, "Send a WhatsApp message", "whatsapp_send", [{ kind: "structured", resource: resource("whatsapp_account", "WhatsApp account"),
  fields: [fixtureField("chat_id", chatId), ...(name === undefined ? [] : [fixtureField("chat_name", name)]), fixtureField("text", text), ...extra] }]);
let records, posts, errors, nextId, amendMode='ok';
const savedEdits=new Map();
function reset() { records = []; posts = []; errors = []; }
const waRecord = (chatId, name, text) => { const r = wa(chatId, name, text); r.id = id(++nextId); r.payload_hash = String(nextId).padStart(64, "a"); return r; };
async function handle(req, res) {
  const url = new URL(req.url, "http://localhost"), route = url.pathname;
  if (route.endsWith("/operations/email_mailboxes_list")) return reply(res, envelope({ mailboxes: [], agents: [] }));
  if (route.endsWith("/operations/context_reviews_list")) return reply(res, envelope({ items: [] }));
  if (route === "/api/session") return reply(res, envelope(session));
  if (route === "/api/me/tasks") return reply(res, { principal_id: OWNER, tasks: [] });
  if (route === "/api/me/attention") return reply(res, envelope({ businesses: [], urgent: [] }));
  if (route === "/api/me/attention/deliveries") return reply(res, envelope({ items: [] }));
  if (route === "/api/biz") return reply(res, [{ id: BIZ, slug: "alpha", name: "Atlas Research", owner: "owner", agents: [], agent_meta: {} }]);
  if (route.endsWith("/agents")) return reply(res, []);
  if (route.endsWith("/env")) return reply(res, { pairs: [] });
  if (route.endsWith("/tasks") || route.endsWith("/log")) return reply(res, []);
  if (route.endsWith("/live")) return reply(res, { sessions: [], requests: [], routine_failures: [] });
  const m = /^\/api\/root\/signoffs(?:\/([^/]+)(?:\/(approve|reject|amend))?)?$/.exec(route);
  if (m) {
    if (!m[1]) {
      const items = records.map(s => { const { preview, ...row } = s; return row; });
      return reply(res, envelope({ items, counts: { pending: items.length }, next_cursor: null, updated_cursor: "c", has_more: false }));
    }
    const item = records.find(s => s.id === m[1]);
    if (!item) return reply(res, { status: "error", error: { code: "not_found", message: "missing" } }, 404);
    if (m[2]) {
      let raw = ""; for await (const chunk of req) raw += chunk;
      posts.push({ id: item.id, action: m[2], body: JSON.parse(raw) });
      if(m[2]==='amend') {
        const body=JSON.parse(raw);
        if(savedEdits.has(body.idempotency_key))return reply(res,envelope({signoff:savedEdits.get(body.idempotency_key)}));
        if(amendMode==='conflict')return reply(res,{status:'error',error:{code:'stale_signoff',message:'This proposal changed; reload it before editing'}},409);
        const revised=clone(item);revised.id=id(++nextId);revised.proposal_revision++;revised.payload_hash='b'.repeat(64);
        revised.supersedes_id=item.id;revised.preview.owner_edit={by:OWNER,previous_id:item.id};
        for(const [key,value] of Object.entries(body.changes))revised.preview.changes[0].fields.find(f=>f.path===key).after=value;
        item.superseded_by_id=revised.id;item.state='conflicted';item.status_revision++;records.push(revised);
        savedEdits.set(body.idempotency_key,revised);
        if(amendMode==='lost-once'){amendMode='ok';res.writeHead(200,{'Content-Type':'application/json'});res.end('{');return;}
        return reply(res,envelope({signoff:revised}));
      }
      item.state = m[2] === "approve" ? "applied" : "rejected"; item.status_revision++;
      return reply(res, envelope({ signoff: clone(item), replayed: false }), 200);
    }
    return reply(res, envelope({ signoff: clone(item) }));
  }
  if (route === "/api/biz/alpha/signoffs") return reply(res, envelope({ review_business_id: BIZ, items: [], counts: { pending: 0 }, next_cursor: null, updated_cursor: "c", has_more: false }));
  if (route.startsWith("/api/root/")) return reply(res, envelope({ items: [] }));
  if (route.startsWith("/api/")) { errors.push(`Unexpected API ${route}`); return reply(res, { status: "error", error: { code: "not_found", message: "no fixture" } }, 404); }
  const target = path.resolve(ROOT, route === "/" ? "index.html" : route.slice(1));
  if (!target.startsWith(ROOT + path.sep)) { res.writeHead(403); return res.end(); }
  try { res.writeHead(200, { "Content-Type": target.endsWith(".css") ? "text/css" : target.endsWith(".js") ? "text/javascript" : "text/html" }); res.end(fs.readFileSync(target)); }
  catch { res.writeHead(404); res.end(); }
}
(async()=>{
  reset();nextId=100;
  const initial=waRecord('friend@s.whatsapp.net','Aylin','Original message');initial.editable_fields=['text'];records.push(initial);
  const originalId=initial.id;
  const server=http.createServer((req,res)=>handle(req,res).catch(e=>{console.error(e);res.writeHead(500).end();}));
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  try{
    const page=await browser.newPage({viewport:{width:1100,height:850}});page.setDefaultTimeout(10000);
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',r=>new URL(r.request().url()).origin===origin?r.continue():r.abort());
    await page.addInitScript(()=>{window.EventSource=class{addEventListener(){}close(){}};});
    await page.goto(origin+'/#alpha');
    await page.waitForFunction(()=>typeof sgCreateView==='function' && state.bizs?.length);
    const mount=()=>page.evaluate(id=>{
      window.editView?.dispose();document.querySelector('#edit-host')?.remove();
      const host=document.createElement('div');host.id='edit-host';host.style.cssText='position:fixed;inset:20px;z-index:99999;background:#111;overflow:auto;padding:12px';document.body.append(host);
      window.editView=sgCreateView({active:()=>host.isConnected,body:()=>host,workspace:()=>'scope:global',global:true,inline:true,execution:true,item:()=>({id,businessId:null})});editView.paint();
    },originalId);
    await mount();
    const field=page.locator('#edit-host .sg-message-editor');await field.waitFor();
    await field.click();await field.fill('Edited <message> & exact content');
    assert.equal(await page.locator('#edit-host .sg-actionarea').isVisible(),true);
    assert.deepEqual(await page.locator('#edit-host .sg-actionarea button').allTextContents(),['Save','Discard']);
    assert.equal(await page.locator('#edit-host .sg-message-controls, #edit-host .sg-conversation textarea').count(),0);
    assert.equal(await field.getAttribute('title'),null);
    assert.equal(await field.evaluate(el=>getComputedStyle(el).padding),'1.5px');
    assert.equal(await field.evaluate(el=>getComputedStyle(el).outlineStyle),'none');
    await page.evaluate(()=>editView.refresh());
    assert.equal(await field.inputValue(),'Edited <message> & exact content','poll does not discard the draft');
    await page.locator('[data-sg=cancel-message]').click();
    assert.equal(await field.inputValue(),'Original message');assert.equal(posts.length,0);
    await field.click();await field.fill('Edited <message> & exact content');
    amendMode='lost-once';
    await page.locator('[data-sg=save-message]').click();
    await page.getByRole('button',{name:'Retry save',exact:true}).waitFor();
    await page.evaluate(()=>editView.refresh());
    await page.getByRole('button',{name:'Retry save',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#edit-host textarea')?.readOnly===true && !!document.querySelector('#edit-host [data-sg=approve]'));
    assert.equal(posts.length,2);assert.equal(posts[0].action,'amend');
    assert.equal(posts[0].body.idempotency_key,posts[1].body.idempotency_key,'lost response retries the same edit');
    assert.equal(records.at(-1).state,'pending','Save is not approval');
    assert.equal(await field.inputValue(),'Edited <message> & exact content');
    assert.equal(initial.preview.changes[0].fields.find(f=>f.path==='text').after,'Original message');
    await mount();await field.waitFor();
    assert.equal(await field.inputValue(),'Edited <message> & exact content','old Chat reference follows the saved revision');
    await field.click();await field.fill('Draft retained after conflict');amendMode='conflict';
    await page.locator('[data-sg=save-message]').click();
    await page.getByText('This proposal changed; reload it before editing',{exact:true}).waitFor();
    assert.equal(await field.inputValue(),'Draft retained after conflict');
    await page.locator('[data-sg=cancel-message]').click();
    await page.locator('#edit-host [data-sg=approve]').click();
    await page.waitForFunction(()=>document.querySelector('#edit-host .sg-row')?.dataset.state==='applied');
    assert.equal(posts.at(-1).id,records.at(-1).id,'approval binds to the replacement');
    assert.equal(posts.at(-1).body.payload_hash,records.at(-1).payload_hash);
    // Email uses the very same editor and action area, with two editable fields.
    const email=record(++nextId,'Reply by email','fastmail_reply',[{kind:'structured',resource:resource('fastmail_account','Mail'),fields:[
      fixtureField('from_address','owner@example.test'),fixtureField('to',['friend@example.test']),fixtureField('subject','Original subject'),fixtureField('body','Original email body'),fixtureField('reply_message_id','m-1')]}]);
    email.editable_fields=['subject','body'];records.push(email);
    await page.evaluate(id=>{
      editView.dispose();const host=document.querySelector('#edit-host');host.replaceChildren();
      window.editView=sgCreateView({active:()=>host.isConnected,body:()=>host,workspace:()=>'scope:global',global:true,inline:true,execution:true,item:()=>({id,businessId:null})});editView.paint();
    },email.id);
    const subject=page.getByRole('textbox',{name:'Email subject',exact:true});
    const body=page.getByRole('textbox',{name:'Email body',exact:true});
    await subject.waitFor();
    assert.equal(await page.locator('#edit-host .sg-reference-icon').innerHTML(),await page.evaluate(()=>{const el=document.createElement('div');el.innerHTML=aicon('mail','currentColor',17);return el.innerHTML;}));
    assert.equal(await page.locator('#edit-host .sg-email-source').count(),0,'no empty reply context section');
    if(SHOTS){fs.mkdirSync(SHOTS,{recursive:true});await page.locator('#edit-host').screenshot({path:path.join(SHOTS,'email-signoff.png')});}
    await subject.click();await subject.fill('Updated subject');
    await body.fill('New email body <safe> & exact');
    assert.deepEqual(await page.locator('#edit-host .sg-actionarea button').allTextContents(),['Save','Discard']);
    await page.evaluate(()=>editView.refresh());
    assert.equal(await subject.inputValue(),'Updated subject');
    assert.equal(await body.inputValue(),'New email body <safe> & exact');
    await page.locator('[data-sg=cancel-message]').click();
    assert.equal(await subject.inputValue(),'Original subject');assert.equal(await body.inputValue(),'Original email body');
    await subject.click();await subject.fill('Updated subject');await body.fill('New email body <safe> & exact');
    amendMode='ok';await page.locator('[data-sg=save-message]').click();
    await page.waitForFunction(()=>!!document.querySelector('#edit-host [data-sg=approve]'));
    assert.deepEqual(posts.at(-1).body.changes,{subject:'Updated subject',body:'New email body <safe> & exact'});
    assert.equal(records.at(-1).state,'pending');
    assert.equal(await subject.inputValue(),'Updated subject');assert.equal(await body.inputValue(),'New email body <safe> & exact');
    const revisedEmail=records.at(-1);
    await page.locator('#edit-host [data-sg=approve]').click();
    await page.waitForFunction(()=>document.querySelector('#edit-host .sg-row')?.dataset.state==='applied');
    assert.equal(posts.at(-1).id,revisedEmail.id);assert.equal(posts.at(-1).body.payload_hash,revisedEmail.payload_hash);
    assert.deepEqual(errors,[]);
    console.log('Inline editing: click, exact text, cancel, poll, revision follow, conflict draft retention and separate approval passed.');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
