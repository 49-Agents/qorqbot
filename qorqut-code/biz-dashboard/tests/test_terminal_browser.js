// Isolated real-xterm fixture: never connects to a real terminal or agent.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const server = http.createServer((req, res) => {
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/agents.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/xterm.css">
      <style>body{margin:0}#pane{display:flex;height:650px;width:100%;padding:30px;box-sizing:border-box}</style>
      <div id="pane"></div><script src="/js/xterm.js"></script><script>
      const state={biz:'one'}, $=(s,p=document)=>p.querySelector(s), evUrl=p=>p;
      window.calls=[];window.hold=false;window.fail=false;window.messages=[];const toast=message=>messages.push(message);
      const post=async(p,b)=>{if(!p.endsWith('/input'))return {};calls.push({p,data:b.data});if(hold)await new Promise(r=>window.release=r);return fail?{error:'failed'}:{ok:true};};
      window.EventSource=class{addEventListener(){}close(){}};
      </script><script src="/js/terminal.js"></script><script>mountTerminal('agent',document.querySelector('#pane'));</script>`);
  }
  const file = path.join(root, req.url);
  if (!file.startsWith(root + '/') || !fs.existsSync(file)) { res.statusCode=404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
  res.end(fs.readFileSync(file));
});
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page=await browser.newPage({viewport:{width:1100,height:750}});
    const errors=[]; page.on('pageerror',e=>errors.push(e.stack));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    assert.equal(await page.locator('.termcomposer, .termtools').count(), 0);
    await page.evaluate(()=>_term.focus());
    await page.keyboard.type('hello');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Control+c');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>calls.map(c=>c.data).join('')==='hello\r\x03\x1b');
    await page.evaluate(()=>{calls=[];});
    // Keystrokes arriving while one POST is in flight coalesce into a single request.
    await page.evaluate(()=>{fail=false;hold=true;release=null;_term.focus();});
    await page.keyboard.type('a');
    await page.waitForFunction(()=>!!window.release);
    await page.keyboard.type('bcd');
    assert.equal(await page.evaluate(()=>calls.length),1);
    await page.evaluate(()=>{hold=false;release();});
    await page.waitForFunction(()=>calls.length===2);
    assert.deepEqual(await page.evaluate(()=>calls.slice(-2).map(c=>c.data)),['a','bcd']);
    await page.setViewportSize({width:390,height:750});
    await page.waitForTimeout(150);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    assert(await page.evaluate(()=>{
      const host=document.querySelector('.termhost');
      const cell=_term._core._renderService.dimensions.css.cell;
      return _term.rows*cell.height <= host.clientHeight && _term.cols*cell.width <= host.clientWidth;
    }));
    await page.evaluate(()=>mountTerminal('other',document.querySelector('#pane')));
    await page.waitForTimeout(100);
    await page.evaluate(()=>teardownTerm());
    await page.waitForTimeout(150);
    assert.deepEqual(errors,[]);
    console.log('Terminal browser checks passed');
  } finally {await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
