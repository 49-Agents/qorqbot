// The workspace rail's icon buttons share one size and each names itself on hover.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const source = read('js/global-agents.js');
const buttons = {guide: 'Walkthrough', terminal: 'Terminal', settings: 'Settings'};
const button = name => source.match(new RegExp(`<button class="item agent global-workspace-${name}"[^>]*>`))[0];

for (const [name, label] of Object.entries(buttons)) {
  test(`${name} shows its name through the shared tooltip`, () => {
    const markup = button(name);
    assert.ok(markup.includes(`data-tip="${label}"`), markup);
    assert.ok(markup.includes(`aria-label="${label}"`), markup);
    // A title on a rail button is silenced by the quiet-control rule, so it would show nothing.
    assert.ok(!/\stitle=/.test(markup), markup);
  });

  test(`${name} is covered by both icon size rules`, () => {
    assert.ok(read('css/global.css').includes(`.global-agents .global-workspace-${name} svg`));
    assert.ok(read('css/agent-workspace.css').includes(`.agent-workspace-app .global-agents .global-workspace-${name} svg`));
  });
}
