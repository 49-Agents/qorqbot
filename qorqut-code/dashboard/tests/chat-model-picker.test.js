'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const picker = require('../js/chat-model-picker.js');

const claude = {client: 'claude', models: [
  {id: 'default', label: 'Default (recommended)', description: 'Opus 5.5', efforts: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: null, default: true, older: false},
  {id: 'opus', label: 'Opus 5.5', description: '', efforts: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: null, default: false, older: false},
  {id: 'claude-opus-4-6', label: 'Opus 4.6', description: '', efforts: ['low', 'medium', 'high', 'max'], default_effort: null, default: false, older: true},
  {id: 'claude-haiku-4-5', label: 'Haiku 4.5', description: '', efforts: [], default_effort: null, default: false, older: true}]};
const codex = {client: 'codex', models: [
  {id: 'gpt-6-luna', label: 'GPT-6-Luna', description: '', efforts: ['low', 'medium', 'high', 'xhigh', 'max'], default_effort: 'medium', default: true, older: false},
  {id: 'gpt-5.6-terra', label: 'GPT-5.6-Terra', description: '', efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], default_effort: 'medium', default: false, older: false}]};

test('the pill names the chosen model and effort; the efforts are the chosen model’s own', () => {
  const view = picker.pickerModel({model: 'opus', effort: 'high'}, claude);
  assert.equal(view.pill, 'Opus 5.5 · High');
  assert.deepEqual(view.main.map(row => [row.id, row.selected]), [['default', false], ['opus', true]]);
  assert.deepEqual(view.older.map(row => row.id), ['claude-opus-4-6', 'claude-haiku-4-5']);
  assert.deepEqual(view.efforts.map(effort => effort.label), ['Low', 'Medium', 'High', 'Extra high', 'Max']);
  assert.equal(picker.pickerModel({model: null, effort: null}, claude).pill, 'Default');
  assert.equal(picker.pickerModel({model: 'claude-haiku-4-5', effort: 'high'}, claude).pill, 'Haiku 4.5', 'a model without efforts shows none');
  assert.equal(picker.pickerModel({model: 'gpt-5.6-terra', effort: 'ultra'}, codex).pill, 'GPT-5.6-Terra · Ultra');
  assert.equal(picker.pickerModel({model: null, effort: null}, codex).pill, 'GPT-6-Luna · Medium', 'Codex’s own default effort');
  assert.equal(picker.pickerModel({model: 'opus', model_fits: false}, claude).pill, 'Default', 'a model of the other client is not shown');
  assert.equal(picker.pickerModel({model: 'opus'}, {models: []}), null, 'nothing to pick: no pill');
});

test('picking the default clears the model, and an effort the new model lacks is cleared with it', () => {
  assert.deepEqual(picker.changeFor('model', 'default', {model: 'opus'}, claude), {model: null});
  assert.deepEqual(picker.changeFor('model', 'claude-opus-4-6', {model: 'opus', effort: 'xhigh'}, claude), {model: 'claude-opus-4-6', effort: null});
  assert.deepEqual(picker.changeFor('model', 'opus', {model: null, effort: 'high'}, claude), {model: 'opus'});
  assert.deepEqual(picker.changeFor('model', 'gpt-6-luna', {model: 'gpt-5.6-terra', effort: 'ultra'}, codex), {model: 'gpt-6-luna', effort: null});
  assert.deepEqual(picker.changeFor('effort', 'ultra', {}, codex), {effort: 'ultra'});
});
