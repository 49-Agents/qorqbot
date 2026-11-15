'use strict';
// Marks the page with the desktop it runs on, so style.css can show the matching wording.
document.documentElement.dataset.platform = (window.qorqbot && window.qorqbot.platform) || 'darwin';
