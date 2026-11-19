'use strict';
// node --test biz-dashboard/tests/*.test.js
// The pure pieces of the Table CSV export (js/tables.js), cut out of their source.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const cut = (file, from, to) => {
  const source = fs.readFileSync(path.join(__dirname, '../js', file), 'utf8');
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `${file} no longer contains ${from}`);
  return source.slice(start, end);
};
const csv = new Function(`const dataValue = value => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
${cut('tables.js', 'function dataCsvCell(value) {', 'async function dataCsvRows')}; return {dataCsvCell, dataCsv, dataCsvName};`)();

test('CSV cells are quoted, formula text is neutralised and numbers stay numbers', () => {
  assert.equal(csv.dataCsvCell('plain'), 'plain');
  assert.equal(csv.dataCsvCell('a, "b"\nc'), '"a, ""b""\nc"');
  assert.equal(csv.dataCsvCell('=SUM(A1)'), "'=SUM(A1)");
  assert.equal(csv.dataCsvCell(-5), '-5');
  assert.equal(csv.dataCsvCell(null), '');
  assert.equal(csv.dataCsvCell(['x', 'y']), '"[""x"",""y""]"');
  assert.equal(csv.dataCsv(['Name', 'Paid'], [['Ada', true]]), '﻿Name,Paid\r\nAda,true\r\n');
  assert.equal(csv.dataCsvName('  Leads / Q4: Türkiye '), 'Leads-Q4-Türkiye.csv');
  assert.equal(csv.dataCsvName(''), 'table.csv');
});
