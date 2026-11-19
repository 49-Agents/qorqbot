'use strict';
// The pages must stay the same look as the Mac app's and must not talk about Mac things.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const ui = path.join(__dirname, '..', 'ui');
const mac = path.join(__dirname, '..', '..', '..', 'macos_app', 'electron', 'ui');
const pages = ['status.html', 'help.html', 'start.html'];

test('every page loads only files that exist (mark.svg is added by the build)', () => {
  const buildAdded = new Set(['mark.svg']);
  for (const page of pages) {
    const html = fs.readFileSync(path.join(ui, page), 'utf8');
    for (const [, file] of html.matchAll(/(?:src|href)="([^"#]+)"/g)) {
      if (buildAdded.has(file)) continue;
      assert.ok(fs.existsSync(path.join(ui, file)), `${page} references ${file}`);
    }
    assert.match(html, /Content-Security-Policy/);
  }
});

test('pages and scripts have no macOS wording', () => {
  for (const file of [...pages, 'help.js', 'start.js', 'status.js']) {
    const text = fs.readFileSync(path.join(ui, file), 'utf8');
    for (const word of [/\bMac\b/, /macOS/, /Finder/, /\bDock\b/, /⌘/, /&#8984;/, /Cmd\+/, /Terminal\.app/, /Homebrew/]) {
      assert.doesNotMatch(text, word, `${file} mentions ${word}`);
    }
  }
});

test('the stylesheet is the Mac one, so the pages look the same', { skip: !fs.existsSync(path.join(mac, 'style.css')) }, () => {
  assert.equal(fs.readFileSync(path.join(ui, 'style.css'), 'utf8'), fs.readFileSync(path.join(mac, 'style.css'), 'utf8'));
});

test('help page covers tray, AppImage, .deb and removal', () => {
  const text = fs.readFileSync(path.join(ui, 'help.html'), 'utf8');
  for (const needle of ['system tray', 'AppImage', '.deb', 'sudo apt remove qorqbot', '~/Qorqbot', 'Ctrl+Q']) {
    assert.ok(text.includes(needle), needle);
  }
});
