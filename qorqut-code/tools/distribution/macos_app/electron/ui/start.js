'use strict';
document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-action]');
  if (target) {
    event.preventDefault();
    window.qorqbot.action(target.dataset.action);
  }
});
