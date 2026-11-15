'use strict';
// Where the app bundle is running from. Opened straight from the disk image, from Downloads or from the Desktop,
// the app would stop working once the image is ejected or the download is cleaned up, so it offers to move itself.
const path = require('node:path');

/** True when BUNDLE (the Qorqbot.app folder) sits on a mounted disk image, in a macOS quarantine copy, or in the
 * user's Downloads or Desktop folder. */
function runsFromTemporaryPlace(bundle, home) {
  const location = path.resolve(bundle);
  if (location.startsWith('/Volumes/')) return true;
  if (location.includes('/AppTranslocation/')) return true;
  return ['Downloads', 'Desktop'].some((folder) => location.startsWith(path.join(home, folder) + path.sep));
}

module.exports = { runsFromTemporaryPlace };
