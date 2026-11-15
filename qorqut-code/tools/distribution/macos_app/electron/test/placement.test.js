'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { runsFromTemporaryPlace } = require('../placement');

test('the app offers to move only from the disk image, a quarantine copy, Downloads or the Desktop', () => {
  const home = '/Users/someone';
  for (const bundle of [
    '/Volumes/Qorqbot/Qorqbot.app',
    '/Volumes/Qorqbot 1/Qorqbot.app',
    '/private/var/folders/ab/cd/T/AppTranslocation/1234-5678/d/Qorqbot.app',
    '/Users/someone/Downloads/Qorqbot.app',
    '/Users/someone/Desktop/unpacked/Qorqbot.app',
  ]) assert.equal(runsFromTemporaryPlace(bundle, home), true, bundle);
  for (const bundle of [
    '/Applications/Qorqbot.app',
    '/Users/someone/Applications/Qorqbot.app',
    '/Users/someone/Downloads-kept/Qorqbot.app',
    '/Users/other/code/out/Qorqbot.app',
  ]) assert.equal(runsFromTemporaryPlace(bundle, home), false, bundle);
});
