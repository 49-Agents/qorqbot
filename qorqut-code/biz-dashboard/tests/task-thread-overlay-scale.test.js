'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const available=!!chromium && fs.existsSync(executable);

test('task thread covers the whole Chat at every text size and after resize', {skip:!available && 'Local Playwright Chromium is unavailable'}, async()=>{
  const browser=await chromium.launch({headless:true,executablePath:executable});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    const root=path.join(__dirname,'..');
    await page.setContent('<body class="agent-workspace-app"><div class="app"><section class="main"><div class="content"><div class="split global-agents agsplit"><nav class="listcol"></nav><section class="global-agent-session"><textarea id="native-draft">Unsent Chat draft</textarea></section><div class="agent-pane-divider"></div><aside class="tasks agpad"><section class="agfocus" data-attention-board><div class="agfocus-layout"><div>Todo</div><aside class="agfocus-dock"><div class="agfocus-dock-content"><textarea aria-label="Task message"></textarea></div></aside></div></section></aside></div></div></section></div></body>');
    for(const [,href] of fs.readFileSync(path.join(root,'index.html'),'utf8').matchAll(/<link rel="stylesheet" href="([^"]+)"/g))await page.addStyleTag({path:path.join(root,href)});
    const source=fs.readFileSync(path.join(root,'js/agent-focus.js'),'utf8');
    await page.addScriptTag({content:source.slice(source.indexOf('function agfocusOverlayClose('))});
    await page.addScriptTag({path:path.join(root,'js/appearance.js')});
    await page.evaluate(()=>{
      window.pane={el:document.querySelector('.agpad')};
      const board=document.querySelector('[data-attention-board]'),dock=board.querySelector('.agfocus-dock');
      agfocusOverlayOpen(pane,board,dock,()=>agfocusOverlayClose(pane));
    });
    for(const textSize of ['small','default','large']) {
      await page.evaluate(textSize=>appearance.set({textSize}),textSize);
      for(const viewport of [{width:1440,height:900},{width:1000,height:700},{width:390,height:844}]) {
        await page.setViewportSize(viewport);
        await page.waitForFunction(()=>{
          const chat=document.querySelector('.global-agent-session').getBoundingClientRect();
          const thread=document.querySelector('.agfocus-chat-overlay').getBoundingClientRect();
          return ['left','top','right','bottom'].every(edge=>Math.abs(chat[edge]-thread[edge])<1);
        },null,{timeout:3000});
        const dock=page.locator('.agfocus-chat-overlay');
        await dock.locator('textarea').fill('Task draft');
        assert.equal(await page.inputValue('#native-draft'),'Unsent Chat draft');
        assert.equal(await page.locator('.global-agent-session').evaluate(el=>el.inert),true);
      }
    }
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.agfocus-chat-overlay').count(),0);
    assert.equal(await page.locator('.global-agent-session').evaluate(el=>el.inert),false);
    assert.equal(await page.inputValue('#native-draft'),'Unsent Chat draft');
  } finally { await browser.close(); }
});
