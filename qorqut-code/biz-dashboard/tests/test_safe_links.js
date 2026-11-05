#!/usr/bin/env node
// The shared safe-link renderer: which links it recognises, what it refuses, and that it never emits markup from text.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const listeners=[];
const sandbox={URL,URLSearchParams,Element:class{},document:{addEventListener:(type,handler,capture)=>listeners.push({type,handler,capture})}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/safe-links.js'),'utf8')+';this.api={renderSafeLinks,safeLinkUrl,plainSafeLinks,tableViewRoute};',sandbox);
const {renderSafeLinks:render,safeLinkUrl:url,plainSafeLinks:plain,tableViewRoute}=sandbox.api;
const anchor=(href,label)=>`<a class="safe-link" href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`;

// Recognised: Markdown links to http(s) and in-app viewer URLs, and bare http(s) URLs.
assert.equal(render('See [the brief](/files/view?path=widget/files/report.md).'),`See ${anchor('/files/view?path=widget/files/report.md','the brief')}.`);
assert.equal(render('[v3](/files/view?document_id=2b1f0c6e-0000-4000-8000-000000000000&business_id=2b1f0c6e-0000-4000-8000-000000000001&version=3)'),
  anchor('/files/view?document_id=2b1f0c6e-0000-4000-8000-000000000000&amp;business_id=2b1f0c6e-0000-4000-8000-000000000001&amp;version=3','v3'));
assert.equal(render('[site](https://example.com/a?b=1&c=2)'),anchor('https://example.com/a?b=1&amp;c=2','site'));
assert.equal(render('go to https://example.com/page, then stop.'),`go to ${anchor('https://example.com/page','https://example.com/page')}, then stop.`);
assert.equal(render('(https://example.com/x)'),`(${anchor('https://example.com/x','https://example.com/x')})`);
assert.equal(render('a\nb\n\nhttp://example.com\nc'),`a\nb\n\n${anchor('http://example.com/','http://example.com')}\nc`,'newlines are kept');
// Labels keep their own escapes and never become markup.
assert.equal(render('[a \\[b\\] \\_c\\_ <i>](https://example.com)'),anchor('https://example.com/','a [b] _c_ &lt;i&gt;'));

// Native absolute references use the host's reader, including escaped spaces and Markdown image syntax.
const mockup='/absolute/path/to/businesses/project-a/content/launch/media/landing-01-A.png';
const fileHref='/files/view?path='+encodeURIComponent(mockup);
assert.equal(render(`[Centered terminals](${mockup})`),anchor(fileHref,'Centered terminals'));
assert.equal(render(`![Centered terminals](${mockup})`),anchor(fileHref,'Centered terminals'));
assert.equal(plain(`[Centered terminals](${mockup})`),'Centered terminals');
assert.equal(render('[draft](</absolute/path/to/businesses/demo/draft v2.png>)'),anchor('/files/view?path='+encodeURIComponent('/absolute/path/to/businesses/demo/draft v2.png'),'draft'));
assert.equal(url('/absolute/path/to/businesses/demo/draft%20v2.png'),'/files/view?path='+encodeURIComponent('/absolute/path/to/businesses/demo/draft v2.png'));
assert.equal(render('[costs](/tables/view?table_id=abc)'),anchor('/tables/view?table_id=abc','costs'));
for(const bad of ['/etc/passwd','/absolute/path/to/businesses/../secret.txt','/absolute/path/to/businesses/%2e%2e/secret.txt','//evil.example/businesses/secret.png','/absolute/path/to/businesses/demo/%00x'])assert.equal(url(bad),null,bad);

// Everything else is escaped text.
for(const text of ['<b>x</b>','<img src=x onerror="alert(1)">',`a & b "q" 'r'`,'`code` *bold* _it_','[x](<https://example.com>)'])
  assert.ok(!/<(?!\/?a\b)/.test(render(text).replace(/&lt;|&gt;/g,'')), text);
assert.equal(render('<b>x</b> & "y"'),'&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;');
assert.equal(render(null),'');assert.equal(render(undefined),'');assert.equal(render(42),'42');

// Refused links stay literal, escaped text.
for(const bad of ['javascript:alert(1)','JaVaScRiPt:alert(1)','data:text/html,<b>','file:///etc/passwd','//evil.example/x','/api/root/files/content?path=a','/files/view','/files/viewx?path=a',
  'ftp://example.com','https://user:pass@example.com/','https:///nohost','http://','mailto:a@b.c','vbscript:x','/files/view?path=\\x']){
  assert.equal(url(bad),null,bad);
  const out=render(`[x](${bad})`);
  assert.ok(!out.includes('<a'),`${bad} -> ${out}`);
}
assert.equal(render('[x](javascript:alert(1))'),'[x](javascript:alert(1))');
assert.equal(render('[x](//evil.example/x)'),'[x](//evil.example/x)');
for(const bad of ['https://example.com/a b','https://exa mple.com','  https://example.com','/files/view?path=a b'])assert.equal(url(bad),null,bad);
assert.equal(url('/files/view?path=a%2Fb&x=1'),'/files/view?path=a%2Fb&x=1');
const table='11111111-1111-4111-8111-111111111111', business='22222222-2222-4222-8222-222222222222';
assert.equal(JSON.stringify(tableViewRoute(`?table_id=${table}`)),JSON.stringify({tableId:table,businessId:null}),'omitted scope explicitly means Global');
assert.equal(JSON.stringify(tableViewRoute(`?table_id=${table}&business_id=${business}`)),JSON.stringify({tableId:table,businessId:business}));
for(const query of [`?table_id=${table}&business_id=bad`,`?table_id=bad`,`?table_id=${table}&table_id=${table}`,`?table_id=${table}&scope=global`])
  assert.equal(tableViewRoute(query),null,query);

// A preview shows labels, not Markdown, and leaves refused links alone.
assert.equal(plain('Read [the brief](/files/view?path=a/b.md) or [x](javascript:alert(1))'),'Read the brief or [x](javascript:alert(1))');

// Clicks and Enter/Space on a safe link are stopped before any row, summary or editor handler; nothing else is.
const make=(type,key,onLink)=>{let stopped=false;return {type,key,target:{closest:selector=>onLink&&selector==='a.safe-link'?{}:null},stopPropagation(){stopped=true;},get stopped(){return stopped;}};};
sandbox.Element=Object;
const fire=(type,key,onLink)=>{const event=make(type,key,onLink);for(const l of listeners.filter(l=>l.type===type))l.handler(event);return event.stopped;};
assert.ok(listeners.every(l=>l.capture===true),'handlers run in the capture phase');
assert.equal(fire('click',undefined,true),true);assert.equal(fire('click',undefined,false),false);
assert.equal(fire('keydown','Enter',true),true);assert.equal(fire('keydown',' ',true),true);assert.equal(fire('keydown','a',true),false);assert.equal(fire('keydown','Enter',false),false);
console.log('safe link checks passed');
