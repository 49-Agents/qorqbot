'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const picker = require('../js/chat-model-picker.js');

const FIVE = ['low', 'medium', 'high', 'xhigh', 'max'];
// What Claude Code 2.1.329 reports (shortened): "default" is an alias of Opus 5.5, while the settings make it run Sonnet 5.5 at
// medium effort when nothing is chosen, and keep xhigh for Fable.
const claude = {client: 'claude', models: [
  {id: 'default', label: 'Default (recommended)', description: 'Opus 5.5 · Best for everyday, complex tasks', efforts: FIVE, default_effort: null, default: true, older: false, resolved: 'claude-opus-5-5'},
  {id: 'opus', label: 'Opus 5.5', description: '', efforts: FIVE, default_effort: null, default: false, older: false, resolved: 'claude-opus-5-5'},
  {id: 'fable', label: 'Fable 5.2', description: '', efforts: FIVE, default_effort: null, default: false, older: false, resolved: 'claude-fable-5-2'},
  {id: 'sonnet', label: 'Sonnet 5.5', description: '', efforts: FIVE, default_effort: null, default: false, older: false, resolved: 'claude-sonnet-5-5'},
  {id: 'claude-opus-4-6', label: 'Opus 4.6', description: '', efforts: ['low', 'medium', 'high', 'max'], default_effort: null, default: false, older: true, resolved: 'claude-opus-4-6'},
  {id: 'claude-haiku-4-5', label: 'Haiku 4.5', description: '', efforts: [], default_effort: null, default: false, older: true, resolved: 'claude-haiku-4-5'}],
current: {model: 'claude-sonnet-5-5', effort: 'medium', efforts: {'claude-sonnet-5-5': 'medium', 'claude-fable-5-2': 'xhigh'}}};
const codex = {client: 'codex', models: [
  {id: 'gpt-6-luna', label: 'GPT-6-Luna', description: '', efforts: FIVE, default_effort: 'medium', default: true, older: false, resolved: null},
  {id: 'gpt-5.6-terra', label: 'GPT-5.6-Terra', description: '', efforts: [...FIVE, 'ultra'], default_effort: 'medium', default: false, older: false, resolved: null}],
current: {model: null, effort: null, efforts: {}}};

test('the pill names the chosen model and effort; the efforts are the chosen model’s own', () => {
  const view = picker.pickerModel({model: 'opus', effort: 'high'}, claude);
  assert.equal(view.pill, 'Opus 5.5 · High');
  assert.deepEqual(view.main.map(row => [row.id, row.selected]), [['opus', true], ['fable', false], ['sonnet', false]]);
  assert.deepEqual(view.older.map(row => row.id), ['claude-opus-4-6', 'claude-haiku-4-5']);
  assert.deepEqual(view.efforts.map(effort => effort.label), ['Low', 'Medium', 'High', 'Extra high', 'Max']);
  assert.equal(picker.pickerModel({model: 'claude-haiku-4-5', effort: 'high'}, claude).pill, 'Haiku 4.5', 'a model without efforts shows none');
  assert.equal(picker.pickerModel({model: 'gpt-5.6-terra', effort: 'ultra'}, codex).pill, 'GPT-5.6-Terra · Ultra');
  assert.equal(picker.pickerModel({model: 'opus'}, {models: []}), null, 'nothing to pick: no pill');
});

test('with nothing chosen the pill names the model and effort the client actually uses, never "Default"', () => {
  const view = picker.pickerModel({model: null, effort: null}, claude);
  assert.equal(view.pill, 'Sonnet 5.5 · Medium');
  assert.deepEqual(view.main.find(row => row.selected).id, 'sonnet');
  assert.deepEqual(view.efforts.filter(effort => effort.selected).map(effort => effort.value), ['medium']);
  assert.ok(!view.main.some(row => row.id === 'default'), 'the "Default" alias is not a row of its own');
  assert.equal(picker.pickerModel({model: 'fable', effort: null}, claude).pill, 'Fable 5.2 · Extra high', 'the effort kept for that model');
  assert.equal(picker.pickerModel({model: null, effort: 'high'}, claude).pill, 'Sonnet 5.5 · High', 'a chosen effort with the unchosen model');
  assert.equal(picker.pickerModel({model: 'opus', model_fits: false}, claude).pill, 'Sonnet 5.5 · Medium', 'a model of the other client is not shown');
  assert.equal(picker.pickerModel({model: null, effort: null}, codex).pill, 'GPT-6-Luna · Medium', 'Codex’s own default model and effort');
  const unknown = {...claude, current: {model: null, effort: null, efforts: {}}};
  assert.equal(picker.pickerModel({model: null, effort: null}, unknown).pill, 'Opus 5.5', 'unsaid: the model the client’s default stands for');
});

test('picking the model in use clears the stored one, and an effort the new model lacks is cleared with it', () => {
  assert.deepEqual(picker.changeFor('model', 'sonnet', {model: 'opus'}, claude), {model: null}, 'the model Claude Code uses unchosen');
  assert.deepEqual(picker.changeFor('model', 'default', {model: 'opus'}, claude), {model: null});
  assert.deepEqual(picker.changeFor('model', 'claude-opus-4-6', {model: 'opus', effort: 'xhigh'}, claude), {model: 'claude-opus-4-6', effort: null});
  assert.deepEqual(picker.changeFor('model', 'opus', {model: null, effort: 'high'}, claude), {model: 'opus'});
  assert.deepEqual(picker.changeFor('model', 'gpt-6-luna', {model: 'gpt-5.6-terra', effort: 'ultra'}, codex), {model: 'gpt-6-luna', effort: null});
  assert.deepEqual(picker.changeFor('model', 'gpt-6-luna', {model: null}, codex), {model: null});
  assert.deepEqual(picker.changeFor('effort', 'ultra', {}, codex), {effort: 'ultra'});
});
