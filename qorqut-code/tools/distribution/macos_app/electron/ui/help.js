'use strict';
window.qorqbot.paths().then((paths) => {
  if (!paths) return;
  document.getElementById('version').textContent = paths.version;
  document.getElementById('root').textContent = paths.root;
  document.getElementById('log').textContent = paths.log;
  document.getElementById('applog').textContent = paths.appLog;
});
document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-action]');
  if (target) {
    event.preventDefault();
    window.qorqbot.action(target.dataset.action);
  }
});
