'use strict';
// node --test biz-dashboard/tests/*.test.js
// The pure pieces of the chat thumbs-up (js/chat-view.js) and the Table CSV export (js/tables.js), cut out of their sources.
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
const reactions = new Function(`${cut('chat-view.js', "const CHAT_REACTION = '👍';", '// ---- the view')}; return {chatReactionText, chatReactionFold};`)();
const csv = new Function(`const dataValue = value => value == null ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
${cut('tables.js', 'function dataCsvCell(value) {', 'async function dataCsvRows')}; return {dataCsvCell, dataCsv, dataCsvName};`)();

const agent = (key, text, extra = {}) => ({key, speaker: 'agent', text, ...extra});
const user = (key, text, extra = {}) => ({key, speaker: 'user', text, ...extra});

test('a bare thumbs-up marks the agent message before it and is not shown as a bubble', () => {
  const rows = reactions.chatReactionFold([agent('a', 'Shall I send it?'), user('u', ' 👍 '), agent('b', 'Sent.')]);
  assert.deepEqual(rows.map(row => [row.key, row.reaction || null]), [['a', '👍'], ['b', null]]);
});

test('a thumbs-up for an older message names it and lands on that message', () => {
  const older = agent('a', 'Plan:\n  1. draft the email to the supplier and wait for the reply before paying the invoice');
  const text = reactions.chatReactionText(older, false);
  assert.equal(text, '👍 to: “Plan: 1. draft the email to the supplier and wait for the re”');
  assert.equal(reactions.chatReactionText(older, true), '👍');
  const rows = reactions.chatReactionFold([older, agent('b', 'Anything else?'), user('u', text)]);
  assert.deepEqual(rows.map(row => [row.key, row.reaction || null]), [['a', '👍'], ['b', null]]);
});

test('a thumbs-up with nothing to land on stays an ordinary message', () => {
  const rows = reactions.chatReactionFold([user('u1', '👍'), agent('a', 'Working…', {partial: true}), user('u2', '👍'),
    user('u3', '👍 to: “no such message”'), user('u4', '👍 great'), user('u5', '👍', {collapsedSource: 'telegram'})]);
  assert.deepEqual(rows.map(row => row.key), ['u1', 'a', 'u2', 'u3', 'u4', 'u5']);
  assert.ok(rows.every(row => !row.reaction));
});

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
