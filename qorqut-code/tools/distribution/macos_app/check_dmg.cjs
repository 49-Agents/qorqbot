'use strict';
// Run with the mounted app's Electron in Node mode and a new, short HOME.
// Exercises the packaged engine and real payload; it does not test Gatekeeper
// or a separate macOS user account, and never starts a native agent client.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const [resources, version, archiveSha256, report] = process.argv.slice(2);
assert.equal(process.platform, 'darwin');
assert.ok(resources && version && archiveSha256 && report, 'resources, version, archive SHA256 and report are required');
const { Engine } = require(path.join(resources, 'app.asar', 'engine.js'));
const home = path.join(os.homedir(), 'Qorqbot');
assert.equal(fs.existsSync(home), false, 'Use a fresh HOME for this check');
const engine = new Engine({ payload: path.join(resources, 'qorqbot-payload'), home });

(async () => {
  try {
    assert.equal(engine.bundledVersion, version);
    assert.equal(engine.archiveSha256, archiveSha256);
    await engine.requirePrerequisites();
    await engine.install();
    assert.equal(engine.installedVersion(), version);
    await engine.run(['verify']);
    const url = await engine.start();
    assert.equal((await fetch(url)).status, 200);
    assert.equal(await engine.status(), url);
    assert.equal(engine.claimFirstRunGuide(), true);
    assert.equal(engine.claimFirstRunGuide(), false);
    await engine.stop();
    assert.equal(await engine.status(), null);
    assert.equal(await engine.start(), url, 'Restart must retain the server address');
    assert.equal((await fetch(url)).status, 200);
    await engine.stop();
    assert.equal(await engine.status(), null);
    fs.writeFileSync(report, JSON.stringify({
      version, source_commit: process.env.GITHUB_SHA, archive_sha256: archiveSha256,
      checks: ['embedded version and checksum', 'prerequisites', 'fresh-home install',
        'installed integrity', 'dashboard HTTP 200', 'first-run guide marker',
        'stop', 'restart on the same port', 'final stopped status'],
      not_tested: ['Gatekeeper / Open Anyway', 'separate macOS user account',
        'interactive desktop windows', 'authenticated agent clients'],
    }, null, 2) + '\n');
    console.log('Packaged launcher checks passed:', report);
  } finally {
    if (engine.server) await engine.stop();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
