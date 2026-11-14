'use strict';
// Run with the installed app's Electron in Node mode and a new, short HOME:
//   ELECTRON_RUN_AS_NODE=1 <app-root>/Qorqbot check_engine.cjs <resources> <version> <archive sha256> <result json>
// Exercises the packaged engine and real payload on Linux; it never starts a native agent client.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const [resources, version, archiveSha256, report] = process.argv.slice(2);
assert.equal(process.platform, 'linux');
assert.ok(resources && version && archiveSha256 && report, 'resources, version, archive SHA256 and result path are required');
const { Engine } = require(path.join(resources, 'app.asar', 'engine.js'));
const home = path.join(os.homedir(), 'Qorqbot');
assert.equal(fs.existsSync(home), false, 'Use a fresh HOME for this check');
const engine = new Engine({
  payload: path.join(resources, 'qorqbot-payload'),
  home,
  firstPort: Number(process.env.QORQBOT_PORT || 1949),
});

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
    await engine.stop();
    assert.equal(await engine.status(), null);
    assert.equal(await engine.start(), url, 'Restart must retain the server address');
    assert.equal((await fetch(url)).status, 200);
    await engine.stop();
    assert.equal(await engine.status(), null);
    fs.writeFileSync(report, JSON.stringify([
      'embedded version and checksum', 'prerequisites', 'fresh-home install',
      'installed integrity', 'dashboard HTTP 200', 'stop',
      'restart on the same port', 'final stopped status',
    ], null, 2) + '\n');
    console.log('Installed engine checks passed:', report);
  } finally {
    if (engine.server) await engine.stop();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
