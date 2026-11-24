'use strict';
// /tables/view with the id alone reads through the root route without a scope, so the table is found from its id; a stated project uses its own route.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');
const TABLE = '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f', PROJECT = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

async function visit(search) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage(), requests = [];
    await page.route('http://dashboard.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/tables/view') return route.fulfill({contentType: 'text/html', body: fs.readFileSync(path.join(root, 'tables-view.html'), 'utf8')});
      if (url.pathname.startsWith('/js/')) return route.fulfill({contentType: 'text/javascript', body: fs.readFileSync(path.join(root, url.pathname), 'utf8')});
      if (url.pathname === '/api/project') return route.fulfill({contentType: 'application/json', body: JSON.stringify([{id: PROJECT, slug: 'acme'}])});
      if (url.pathname.startsWith('/api/')) {
        requests.push([url.pathname, JSON.parse(route.request().postData() || '{}')]);
        const body = url.pathname.endsWith('tables_get')
          ? {status: 'ok', data: {table: {id: TABLE, title: 'Creator list', columns: [{id: 'c', name: 'Name'}], table_kind: 'general'}}}
          : {status: 'ok', data: {items: [{cells: {c: 'Ada'}}], next_cursor: null}};
        return route.fulfill({contentType: 'application/json', body: JSON.stringify(body)});
      }
      return route.fulfill({status: 204, body: ''});
    });
    await page.goto(`http://dashboard.test/tables/view?${search}`);
    await page.waitForFunction(() => document.getElementById('tv-count')?.textContent === '1 row');
    return requests;
  } finally { await browser.close(); }
}

test('a table link with the id alone reads from the root route with no scope', {skip: !available}, async () => {
  assert.deepEqual(await visit(`table_id=${TABLE}`), [
    ['/api/root/operations/tables_get', {table_id: TABLE}],
    ['/api/root/operations/tables_rows_list', {table_id: TABLE, limit: 50}]]);
});

test('a table link that states its project reads from that project', {skip: !available}, async () => {
  assert.deepEqual(await visit(`table_id=${TABLE}&project_id=${PROJECT}`), [
    ['/api/project/acme/operations/tables_get', {table_id: TABLE}],
    ['/api/project/acme/operations/tables_rows_list', {table_id: TABLE, limit: 50}]]);
});
