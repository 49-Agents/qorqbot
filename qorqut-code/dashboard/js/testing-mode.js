// Owner Testing mode. The dashboard reads this server-side preference before
// offering Overseer controls; an unknown or failed read stays closed.
const TESTING_MODE_ENDPOINT = '/root/testing-mode';
const testingMode = {status: 'unknown', enabled: false, error: null, pending: null};
let testingModeRefreshQueued = false;
const testingModeListeners = new Set();
let testingModeChannel = null;
try { testingModeChannel = new BroadcastChannel('qorqut-testing-mode'); } catch (_) { testingModeChannel = null; }

function testingModeEnabled() { return testingMode.status === 'on' && testingMode.enabled === true; }
function testingModeFailed() { return testingMode.status === 'error'; }
function testingModeMessage() {
  return testingMode.error?.message || 'Testing mode preference is unavailable. Overseer stays off.';
}
function testingModeSubscribe(listener) {
  if (typeof listener !== 'function') return () => {};
  testingModeListeners.add(listener);
  return () => testingModeListeners.delete(listener);
}
function testingModeChanged() {
  for (const listener of [...testingModeListeners]) {
    try { listener(testingMode); } catch (_) { /* one observer must not block the rest */ }
  }
}
function testingModeSetState(enabled, error = null) {
  const changed = testingMode.status !== (enabled ? 'on' : 'off') || testingMode.enabled !== enabled || testingMode.error !== error;
  testingMode.status = enabled ? 'on' : 'off';
  testingMode.enabled = enabled === true;
  testingMode.error = error;
  if (changed) testingModeChanged();
}
async function testingModeLoad(force = false) {
  if (!force && ['on', 'off'].includes(testingMode.status)) return testingMode;
  if (testingMode.pending) {
    // A change announced while a read is in flight still gets one fresh read
    // after it settles, so a later OFF cannot be hidden by an older ON reply.
    if (force) testingModeRefreshQueued = true;
    return testingMode.pending;
  }
  testingMode.pending = (async () => {
    try {
      const data = coreData(await api(TESTING_MODE_ENDPOINT, {cache: 'no-store'}));
      if (typeof data?.enabled !== 'boolean') throw new Error('The Testing mode preference is unavailable.');
      testingModeSetState(data.enabled);
    } catch (error) {
      // Fail closed: a preference that cannot be read never enables Overseer.
      testingMode.status = 'error';
      testingMode.enabled = false;
      testingMode.error = error;
      testingModeChanged();
    } finally {
      testingMode.pending = null;
      if (testingModeRefreshQueued) {
        testingModeRefreshQueued = false;
        void testingModeLoad(true);
      }
    }
    return testingMode;
  })();
  return testingMode.pending;
}
function testingModeReady() { return testingModeLoad(false); }
async function testingModeSet(enabled) {
  if (typeof enabled !== 'boolean') throw new Error('Testing mode requires an explicit boolean.');
  const data = coreData(await post(TESTING_MODE_ENDPOINT, {enabled}));
  if (typeof data?.enabled !== 'boolean') throw new Error('The saved Testing mode preference is unavailable.');
  testingModeSetState(data.enabled);
  try { testingModeChannel?.postMessage({enabled: data.enabled}); } catch (_) { /* optional tab sync */ }
  return data.enabled;
}
if (testingModeChannel) testingModeChannel.onmessage = () => { void testingModeLoad(true); };