'use strict';
const $ = (id) => document.getElementById(id);
const MARKS = { done: '✓', failed: '✕' };

function checkItem(status, text, note) {
  const li = document.createElement('li');
  li.className = status;
  const mark = document.createElement('span');
  mark.className = 'mark';
  mark.textContent = { done: '✓', failed: '✕', optional: '–' }[status];
  const label = document.createElement('span');
  label.textContent = text;
  li.append(mark, label);
  if (note) {
    const small = document.createElement('span');
    small.className = 'note';
    small.textContent = note;
    li.append(small);
  }
  return li;
}

function render(state) {
  if (!state) return;
  $('title').textContent = state.title;
  $('subtitle').textContent = {
    error: 'Nothing was changed that you need to undo.',
    stopped: 'Start it again whenever you like.',
    stopping: 'Waiting for the local server to finish.',
    setup: 'A few programs are missing. Install them and Qorqbot continues by itself.',
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
  // The checklist replaces the progress steps while programs are missing.
  $('steps-card').hidden = state.steps.length === 0 || state.phase === 'setup';

  const checklist = state.phase === 'setup' ? state.checklist : null;
  $('checklist-card').hidden = !checklist;
  if (checklist) {
    $('checklist-tools').replaceChildren(...checklist.tools.map((tool) =>
      checkItem(tool.ok ? 'done' : 'failed', tool.label, tool.ok ? '' : 'missing')));
    $('checklist-agents').replaceChildren(...checklist.agents.map((agent) => {
      const item = checkItem(agent.ok ? 'done' : 'optional', `${agent.label} (${agent.command})`, agent.ok ? '' : 'not found');
      if (!agent.ok) {
        const link = document.createElement('a');
        link.href = '#';
        link.textContent = 'How to install';
        link.dataset.action = 'agent-docs:' + agent.id;
        item.append(link);
      }
      return item;
    }));
    $('homebrew-block').hidden = checklist.homebrew;
    $('checklist-command-block').hidden = !checklist.command;
    $('checklist-command').textContent = checklist.command;
  }

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
