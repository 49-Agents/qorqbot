// PR4 (HANDOFF-ui-speedup-stage2, 4.4): js/core.js api()'s per-URL {etag,body}
// cache. A synthetic API stands in for the server: it fully controls the
// ETag and body of one route, so the test can assert on real network
// behaviour (does a second read actually send If-None-Match and skip the
// body?) rather than on internals alone.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
const businesses=['alpha','beta'].map(slug=>({id:'business-'+slug,slug,name:slug[0].toUpperCase()+slug.slice(1),agents:[]}));
let etag='v1',etagBody={n:1};
const counts={fresh:0,revalidated:0,noEtag:0,postSawINM:false,postCount:0};
const server=http.createServer((req,res)=>{
  const pathname=new URL(req.url,'http://localhost').pathname;
  if(pathname==='/api/test/etag'){
    if(req.method==='POST'){
      counts.postCount++;if(req.headers['if-none-match'])counts.postSawINM=true;
      res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({posted:true}));return;
    }
    if(req.headers['if-none-match']===etag){counts.revalidated++;res.writeHead(304,{'ETag':etag});res.end();return;}
    counts.fresh++;res.writeHead(200,{'Content-Type':'application/json','ETag':etag});res.end(JSON.stringify(etagBody));return;
  }
  if(pathname==='/api/test/no-etag'){
    counts.noEtag++;res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({seen:counts.noEtag}));return;
  }
  if(pathname.startsWith('/api/')){
    let body=[];
    if(pathname==='/api/biz')body=businesses;
    if(pathname==='/api/session')body={status:'ok',data:{principal_id:'owner'}};
    if(pathname==='/api/me/tasks')body={principal_id:'owner',tasks:[]};
    // Deliberately no ETag on any of the above (or on any /live-suffixed
    // route a real business tab reads): nothing this test does not control
    // can ever land in the cache, so later assertions cannot race a
    // fire-and-forget refetch from selectBiz's own rendering.
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(body));return;
  }
  const file=path.resolve(root,'.'+(pathname==='/'?'/index.html':pathname));
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){res.writeHead(404).end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
  res.end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const url=`http://127.0.0.1:${server.address().port}/#/global/overview`;
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE||undefined});
  try{
    const page=await browser.newPage({viewport:{width:1100,height:800}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url);
    await page.getByRole('heading',{name:'Your projects'}).waitFor();

    let result=await page.evaluate(()=>api('/test/etag'));
    assert.equal(result.n,1,'fresh read returns the server body');
    assert.equal(await page.evaluate(()=>etagCache.size),1);
    assert.equal(await page.evaluate(()=>etagCache.get('/api/test/etag').etag),'v1');
    assert.equal(counts.fresh,1);assert.equal(counts.revalidated,0);

    result=await page.evaluate(()=>api('/test/etag'));
    assert.equal(result.n,1,'a 304 must be served from the cached body');
    assert.equal(counts.fresh,1,'a 304 must not cost a second body fetch');
    assert.equal(counts.revalidated,1,'the repeat read must send If-None-Match');

    etag='v2';etagBody={n:2};
    result=await page.evaluate(()=>api('/test/etag'));
    assert.equal(result.n,2,'a changed resource must never be served from the stale cache entry');
    assert.equal(counts.fresh,2);
    assert.equal(await page.evaluate(()=>etagCache.get('/api/test/etag').etag),'v2');

    await page.evaluate(()=>api('/test/no-etag'));
    await page.evaluate(()=>api('/test/no-etag'));
    assert.equal(counts.noEtag,2,'a route with no ETag is always a fresh read');
    assert.equal(await page.evaluate(()=>etagCache.has('/api/test/no-etag')),false,'a route with no ETag is never cached');

    await page.evaluate(()=>post('/test/etag',{}));
    assert.equal(counts.postCount,1);
    assert.equal(counts.postSawINM,false,'POST must never send If-None-Match, even for a URL the cache already knows');

    const lru=await page.evaluate(()=>{
      for(let i=0;i<201;i++)etagCacheTouch('/api/lru/'+i,{etag:'e'+i,body:{i}});
      return {size:etagCache.size,hasOldest:etagCache.has('/api/lru/0'),hasNewest:etagCache.has('/api/lru/200')};
    });
    assert.equal(lru.size,200,'the cache is bounded at 200 entries');
    assert.equal(lru.hasOldest,false,'the oldest entry is evicted once the bound is exceeded');
    assert.equal(lru.hasNewest,true,'the most recently written entry survives');

    assert((await page.evaluate(()=>etagCache.size))>0);
    await page.evaluate(slug=>selectBiz(slug),'alpha');
    assert.equal(await page.evaluate(()=>etagCache.size),0,'selectBiz must clear the ETag cache');

    await page.evaluate(()=>api('/test/etag'));
    assert((await page.evaluate(()=>etagCache.size))>0);
    await page.evaluate(()=>selectGlobal());
    assert.equal(await page.evaluate(()=>etagCache.size),0,'selectGlobal must clear the ETag cache');

    assert.deepEqual(errors,[]);
    console.log('Dashboard ETag cache: 304 revalidation, change detection, no-etag passthrough, POST unaffected, LRU bound and business-switch clear passed');
  }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
