'use strict';
// What the Server menu and the menu bar item say and offer. Plain data and no Electron imports, so the wording and
// the enabled states are tested with node --test, and the two menus cannot drift apart.

/** The one status line. `running` already means the server is up and its address is known. */
function statusLabel({ busy, running, phase, url }) {
  if (busy) return 'Status: working…';
  if (running) return `Status: running at ${url}`;
  return phase === 'error' ? 'Status: needs attention' : 'Status: stopped';
}

/** A server started from Terminal belongs to that Terminal; quitting the app leaves it running. */
function quitLabel({ ownsServer, running }) {
  return !ownsServer && running ? 'Quit Qorqbot (server keeps running in Terminal)' : 'Quit Qorqbot (stops the server)';
}

function attentionLabel(count) {
  return count === 1 ? '1 item needs you' : `${count} items need you`;
}

/** The menu bar item's menu. `model` is { busy, running, phase, url, ownsServer, attention }; `actions` holds the
 * click handlers show, openInBrowser, start, stop, restart and quit. Same wording and enabled states as the Server menu. */
function trayMenuTemplate(model, actions) {
  const { busy, running, attention } = model;
  const items = [{ label: statusLabel(model), enabled: false }];
  if (attention > 0) items.push({ label: attentionLabel(attention), click: actions.show });
  items.push(
    { type: 'separator' },
    { label: 'Show Qorqbot', click: actions.show },
    { label: 'Open in Browser', enabled: running, click: actions.openInBrowser },
    { type: 'separator' },
    running
      ? { label: 'Stop Server', enabled: !busy, click: actions.stop }
      : { label: 'Start Server', enabled: !busy, click: actions.start },
    { label: 'Restart Server', enabled: running && !busy, click: actions.restart },
    { type: 'separator' },
    { label: quitLabel(model), click: actions.quit },
  );
  return items;
}

module.exports = { statusLabel, quitLabel, attentionLabel, trayMenuTemplate };
