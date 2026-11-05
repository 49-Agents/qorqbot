const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:1280,height:800}}),root=path.resolve(__dirname,'..'),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.setContent('<div class="global-context"><div id="host" class="pane ctxview global-learning" style="padding:30px"></div></div>');
 for(const m of fs.readFileSync(root+'/index.html','utf8').matchAll(/<link rel="stylesheet" href="\/([^"]+)"/g))await page.addStyleTag({path:root+'/'+m[1]});
 const core=fs.readFileSync(root+'/js/core.js','utf8')+'\n'+fs.readFileSync(root+'/js/san-juan-time.js','utf8'),context=fs.readFileSync(root+'/js/context.js','utf8'),global=fs.readFileSync(root+'/js/global.js','utf8');
 await page.addScriptTag({content:`const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');const coreData=x=>x;let calls=0;const api=async p=>p.includes('?kind=')?{items:Array.from({length:35},(_,i)=>({id:String(i),title:'Research note '+i}))}:{document:{title:'A material',body:'# Heading\\n- A bullet\\n**Bold** <img src=x onerror=alert(1)>',revision:1,kind:'context_note'}};`+core.slice(core.indexOf('function newRequestId()'),core.indexOf('function coreData('))+context.slice(context.indexOf('const mdInline'),context.indexOf('\n',context.indexOf('const mdLines')))+global.slice(global.indexOf('async function renderGlobalLibrary'),global.indexOf('let globalRender'))});
 await page.evaluate(()=>{Object.defineProperty(crypto,'randomUUID',{value:undefined});return renderGlobalLibrary(document.querySelector('#host'));});
 await page.locator('.global-library-list button').first().click();await page.getByRole('heading',{name:'A material'}).waitFor();
 assert.equal(await page.locator('.global-library-list').isVisible(),false);assert.equal(await page.locator('.library-body img').count(),0);assert.equal(await page.locator('.library-body strong').textContent(),'Bold');
 assert((await page.locator('.library-back').boundingBox()).y<200);await page.screenshot({path:'/tmp/context-library-reader.png'});
 await page.getByRole('button',{name:'← Back',exact:true}).click();assert.equal(await page.locator('.global-library-list button').count(),35);await page.screenshot({path:'/tmp/context-library-list.png'});
 await page.setViewportSize({width:430,height:850});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert.deepEqual(errors,[]);
 console.log('Library: HTTP ID fallback, list/detail/back, safe formatting and mobile passed');
 }finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
