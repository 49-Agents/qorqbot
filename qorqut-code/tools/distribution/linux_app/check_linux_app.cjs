'use strict';
// Run with the packaged app's Electron in Node mode and a new, short HOME:
//
//   ELECTRON_RUN_AS_NODE=1 squashfs-root/qorqbot-desktop check_linux_app.cjs squashfs-root/resources VERSION ARCHIVE_SHA256 report.json
//
// (squashfs-root comes from `Qorqbot-VERSION-x86_64.AppImage --appimage-extract`; for the .deb use /opt/Qorqbot.)
// Exercises the packaged engine and real payload; it does not test the desktop windows, the AppArmor profile or the
// Chromium sandbox, and never starts a native agent client. The fifth argument, --allow-system-tools, accepts an
// archive that is not self-contained.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const [resourcesArgument, version, archiveSha256, report, allowSystemTools] = process.argv.slice(2);
assert.equal(process.platform, 'linux');
assert.ok(resourcesArgument && version && archiveSha256 && report, 'resources, version, archive SHA256 and report are required');
const resources = path.resolve(resourcesArgument);
const payload = path.join(resources, 'qorqbot-payload');
const { Engine } = require(path.join(resources, 'app.asar', 'engine.js'));
const home = path.join(os.homedir(), 'Qorqbot');
assert.equal(fs.existsSync(home), false, 'Use a fresh HOME for this check');
const engine = new Engine({ payload, home });

function digest(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

(async () => {
  try {
    // The payload is what was asked for: the named archive, unchanged, with its recorded version and checksum.
    assert.equal(engine.bundledVersion, version);
    assert.equal(engine.archiveSha256, archiveSha256);
    assert.equal(digest(engine.archive), archiveSha256, 'The archive inside the app differs from the recorded checksum');
    for (const notice of [path.join(resources, 'THIRD-PARTY-NOTICES.md'),
      path.join(resources, '..', 'LICENSES.chromium.html')]) {
      assert.ok(fs.statSync(notice).size > 0, notice + ' is missing');
    }
    await engine.install();
    assert.equal(engine.installedVersion(), version);
    const installed = JSON.parse(fs.readFileSync(path.join(engine.root, 'runtime', 'PAYLOAD.json'), 'utf8'));
    assert.equal(installed.version, version);
    assert.equal(installed.platform, 'ubuntu24-x86_64');
    const selfContained = Object.keys(installed.bundled || {}).length > 0;
    assert.ok(selfContained || allowSystemTools === '--allow-system-tools', 'The archive is not self-contained');
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
    fs.writeFileSync(report, JSON.stringify({
      version, source_commit: process.env.GITHUB_SHA, archive_sha256: archiveSha256, self_contained: selfContained,
      checks: ['embedded version, checksum and notices', 'fresh-home install', 'installed integrity',
        'dashboard HTTP 200', 'stop', 'restart on the same port', 'final stopped status'],
      not_tested: ['interactive desktop windows', 'tray and desktop menu entry', 'Chromium sandbox and AppArmor profile',
        'authenticated agent clients'],
    }, null, 2) + '\n');
    console.log('Packaged launcher checks passed:', report);
  } finally {
    if (engine.server) await engine.stop();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
