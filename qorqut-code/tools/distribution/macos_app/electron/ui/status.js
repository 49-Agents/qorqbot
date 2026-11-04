'use strict';
const $ = (id) => document.getElementById(id);
const MARKS = { done: '✓', failed: '✕' };

function render(state) {
  if (!state) return;
  $('title').textContent = state.title;
  $('subtitle').textContent = {
    error: 'Nothing was changed that you need to undo.',
    stopped: 'Start it again whenever you like.',
    stopping: 'Waiting for the local server to finish.',
  }[state.phase] || 'Qorqbot runs on this Mac. Nothing is sent anywhere during set-up.';

  const steps = $('steps');
  steps.replaceChildren(...state.steps.map((item) => {
    const li = document.createElement('li');
    li.className = item.status;
    const mark = document.createElement('span');
    mark.className = 'mark';
    if (item.status === 'active') {
      const spinner = document.createElement('span');
      spinner.className = 'spinner';
      mark.append(spinner);
    } else {
      mark.textContent = MARKS[item.status] || '';
    }
    const label = document.createElement('span');
    label.textContent = item.label;
    li.append(mark, label);
    return li;
  }));
  $('steps-card').hidden = state.steps.length === 0;

  const choice = state.choice;
  $('choice-card').hidden = !choice;
  if (choice) {
    $('choice-message').textContent = choice.message;
    $('choice-buttons').replaceChildren(...choice.buttons.map((button) => {
      const element = document.createElement('button');
      element.textContent = button.label;
      if (button.primary) element.className = 'primary';
      element.dataset.action = 'choice:' + button.id;
      return element;
    }));
  }

  const error = state.phase === 'error' ? state.error : null;
  $('error-card').hidden = !error;
  if (error) {
    $('error-message').textContent = error.message;
    $('command-block').hidden = !error.command;
    $('copy-command').hidden = !error.command;
    $('open-terminal').hidden = !error.command;
    $('command').textContent = error.command || '';
    $('detail-block').hidden = !error.detail;
    $('detail').textContent = error.detail || '';
  }
  $('stopped-card').hidden = state.phase !== 'stopped';

  $('log-card').hidden = state.log.length === 0;
  $('log').textContent = state.log.join('\n');
  $('log').scrollTop = $('log').scrollHeight;
}

document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  event.preventDefault();
  const action = target.dataset.action;
  if (action === 'copy-command' || action === 'copy-details') {
    const original = target.textContent;
    target.textContent = 'Copied';
    setTimeout(() => { target.textContent = original; }, 1200);
  }
  window.qorqbot.action(action);
});

window.qorqbot.onState(render);
window.qorqbot.getState().then(render);
