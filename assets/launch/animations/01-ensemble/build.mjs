#!/usr/bin/env node
// Refreshes the generated blocks of index.html in place so the film stays one
// self-contained file: embedded Zen Kaku Gothic New fonts, the reused 49IDE GSAP
// runtime, and the DeepSeek mark cut from approved slide 02. Authored markup,
// CSS and the film script are edited directly in index.html; rerunning is idempotent.
//   node build.mjs            (QB_49IDE_ANIM overrides the 49IDE film folder)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const anim = process.env.QB_49IDE_ANIM || '/home/alp/businesses/49IDE/code/landing/anim';
const b64 = (file) => fs.readFileSync(file).toString('base64');
const text = (file) => {
  const s = fs.readFileSync(file, 'utf8');
  if (/<\/script/i.test(s)) throw new Error(`${file} contains a closing script tag`);
  return s.trim();
};

const FONTS = [['Light', 300], ['Regular', 400], ['Medium', 500]];
const blocks = {
  fonts: () => '<style>\n' + FONTS.map(([name, weight]) =>
    `@font-face{font-family:"Zen Kaku Gothic New";font-style:normal;font-weight:${weight};font-display:block;` +
    `src:url(data:font/woff2;base64,${b64(path.join(anim, 'assets/fonts', `ZenKakuGothicNew-${name}.woff2`))}) format("woff2")}`).join('\n') +
    '\n</style>',
  vendor: () =>
    `<script>\n/* Reused from the 49IDE launch film runtime (runtime/gsap.min.js); licence banner below, see LICENSE-gsap.txt. */\n${text(path.join(anim, 'runtime/gsap.min.js'))}\n</script>\n` +
    `<script>\n/* Reused from the 49IDE launch film runtime (runtime/CustomEase.min.js). */\n${text(path.join(anim, 'runtime/CustomEase.min.js'))}\n</script>`,
  deepseek: () =>
    `<img id="mk-deepseek" class="mark" alt="" style="left:854px;top:655px;width:164px;height:126px;opacity:0" ` +
    `src="data:image/png;base64,${b64(path.join(here, 'sources/deepseek-mark-from-02.png'))}">`,
};

const file = path.join(here, 'index.html');
let html = fs.readFileSync(file, 'utf8');
for (const [name, render] of Object.entries(blocks)) {
  const re = new RegExp(`(<!-- build:${name} -->)[\\s\\S]*?(<!-- /build:${name} -->)`);
  if (!re.test(html)) throw new Error(`missing build:${name} markers`);
  const body = render();
  html = html.replace(re, (_, open, close) => `${open}\n${body}\n${close}`);
}
fs.writeFileSync(file, html);
console.log(`index.html: ${(Buffer.byteLength(html) / 1024).toFixed(1)} KiB`);
