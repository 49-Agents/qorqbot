#!/usr/bin/env node
// Writes docs/agent-icon-catalog.html: the worker icon catalog rendered from the trusted bundled AICONS.
// `node tools/agent-icons/build_catalog.js` rewrites it; tests/test_agent_icons.js checks it is current.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
// Catalog IDs frozen by the Ordubashy team plan; the host's agents_icons_list advertises the same IDs.
const AGENT_ICON_IDS = ['bot', 'brain', 'sparkles', 'zap', 'rocket', 'flame', 'ghost', 'cat', 'dog', 'bird', 'fish', 'bug', 'cpu', 'terminal',
  'code', 'pen-tool', 'megaphone', 'calculator', 'chart-column', 'database', 'search', 'globe', 'shield', 'wrench', 'triangle', 'circle',
  'square', 'lightbulb', 'flag', 'hand'];
function loadCore() {
  const context = vm.createContext({URLSearchParams, location: {search: ''}});
  vm.runInContext(fs.readFileSync(path.join(root, 'js/departments.js'), 'utf8') + '\n' + fs.readFileSync(path.join(root, 'js/core.js'), 'utf8').split('// lazy:')[0], context);
  return context;
}
const label = id => id.replace(/-/g, ' ').replace(/^./, c => c.toUpperCase());
function catalogHTML() {
  const context = loadCore();
  const cells = AGENT_ICON_IDS.map(id => `<figure><svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${vm.runInContext("AICONS", context)[id]}</svg><figcaption><code>${id}</code> ${label(id)}</figcaption></figure>`).join('\n');
  return `<!doctype html>\n<meta charset="utf-8"><title>Worker icon catalog</title>\n<style>body{font:14px system-ui;background:#16171a;color:#e8e8ea;margin:24px}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}figure{margin:0;padding:12px;border:1px solid #333;border-radius:8px;display:flex;flex-direction:column;align-items:center;gap:8px}figcaption{font-size:12px;text-align:center}</style>\n<h1>Worker icon catalog</h1>\n<p>Generated from the bundled icons in <code>js/core.js</code> by <code>tools/agent-icons/build_catalog.js</code>. A worker's chosen <code>presentation.icon</code> ID renders one of these.</p>\n<main>\n${cells}\n</main>\n`;
}
module.exports = {AGENT_ICON_IDS, catalogHTML, loadCore};
if (require.main === module) fs.writeFileSync(path.join(root, 'docs/agent-icon-catalog.html'), catalogHTML());
