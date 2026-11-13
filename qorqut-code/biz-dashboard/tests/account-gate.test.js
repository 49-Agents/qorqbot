const test = require('node:test');
const assert = require('node:assert');
const gate = require('../js/account-gate.js');

test('a newer release is announced with its download page', () => {
  const message = gate.updateMessage({update: {version: '1.3.1', url: 'https://qorqbot.com/downloads/'}});
  assert.strictEqual(message.text, 'Qorqbot 1.3.1 is available.');
  assert.strictEqual(message.url, 'https://qorqbot.com/downloads/');
  assert.strictEqual(message.version, '1.3.1');
});

test('nothing is announced without a newer release', () => {
  assert.strictEqual(gate.updateMessage({update: null}), null);
  assert.strictEqual(gate.updateMessage({}), null);
  assert.strictEqual(gate.updateMessage(null), null);
});

test('the license and privacy links point at the website pages', () => {
  assert.strictEqual(gate.LICENSE_URL, 'https://qorqbot.com/license/');
  assert.strictEqual(gate.PRIVACY_URL, 'https://qorqbot.com/privacy/');
});

const view = (license, use = 'work') => ({signed_in: true, account: {email: 'sam@example.com', use}, license});

test('a paid license shows its plan and paid-through date, for work or personal use', () => {
  const paid = {commercial_free_until: '2027-01-01', paid: true, paid_until: '2027-03-15T00:00:00Z', price: 'yearly'};
  assert.deepStrictEqual(gate.licenseMessage(view(paid)), {status: 'paid', text: 'Paid license (yearly) through March 15, 2027.'});
  assert.deepStrictEqual(gate.licenseMessage(view(paid, 'personal')), {status: 'paid', text: 'Paid license (yearly) through March 15, 2027.'});
});

test('the paid-through date is a calendar date whatever the time zone', () => {
  const text = gate.licenseMessage(view({paid: true, paid_until: '2027-03-15', price: 'monthly'})).text;
  assert.strictEqual(text, 'Paid license (monthly) through March 15, 2027.');
});

test('a paid license without a plan or date still reads as paid and invents neither', () => {
  assert.strictEqual(gate.licenseMessage(view({paid: true, price: null, paid_until: null})).text, 'Paid license.');
  assert.strictEqual(gate.licenseMessage(view({paid: true, price: 'weekly', paid_until: 'soon'})).text, 'Paid license.');
});

test('an unpaid work license shows the free date and a personal one says nothing extra', () => {
  const unpaid = {commercial_free_until: '2027-01-01', paid: false, paid_until: null, price: null};
  assert.deepStrictEqual(gate.licenseMessage(view(unpaid)), {status: 'free', text: 'Commercial license free until January 1, 2027.'});
  assert.deepStrictEqual(gate.licenseMessage(view(unpaid, 'personal')), {status: 'free', text: ''});
});

test('an unknown paid status is shown as not known, never as paid', () => {
  const unknown = {commercial_free_until: '2027-01-01', paid: null, paid_until: null, price: null};
  assert.deepStrictEqual(gate.licenseMessage(view(unknown)),
    {status: 'unknown', text: 'Commercial license free until January 1, 2027. Paid status not known yet.'});
  assert.deepStrictEqual(gate.licenseMessage(view(unknown, 'personal')), {status: 'unknown', text: 'Paid status not known yet.'});
  assert.strictEqual(gate.licenseMessage(view({commercial_free_until: '2027-01-01'})).status, 'unknown');
  assert.strictEqual(gate.licenseMessage(view({paid: 'yes'})).status, 'unknown');
});

test('an older reply without a license object shows nothing', () => {
  assert.strictEqual(gate.licenseMessage({signed_in: true, account: {use: 'work'}}), null);
  assert.strictEqual(gate.licenseMessage(null), null);
});
