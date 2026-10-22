// The app's own page: first-run setup, starting the engine, and errors.
// It renders the status the app sends (`app_status`, `status` events); once
// the engine runs, the app replaces this page with the editor.
'use strict';

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const $ = (id) => document.getElementById(id);

function show(section) {
  for (const id of ['starting', 'intro', 'installing', 'failed']) $(id).hidden = id !== section;
}

function gigabytes(value) {
  return `${value.toFixed(1).replace(/\.0$/, '')} GB`;
}

function renderSteps(list, steps) {
  list.replaceChildren(
    ...steps.map((step) => {
      const item = document.createElement('li');
      item.className = step.state;
      const icon = document.createElement('span');
      icon.className = 'icon';
      const text = document.createElement('span');
      const label = document.createElement('span');
      label.className = 'label';
      label.textContent = step.label;
      const detail = document.createElement('span');
      detail.className = 'detail';
      detail.textContent = step.detail;
      text.append(label, detail);
      const percent = document.createElement('span');
      percent.className = 'percent';
      if (step.state === 'running' && step.fraction !== null) {
        percent.textContent = `${Math.round(step.fraction * 100)}%`;
      }
      item.append(icon, text, percent);
      return item;
    }),
  );
}

function renderLog(element, lines) {
  const atBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 4;
  element.textContent = (lines || []).join('\n');
  if (atBottom) element.scrollTop = element.scrollHeight;
}

let started = Date.now();

function render(status) {
  $('version').textContent = `Version ${status.version}`;
  const setup = status.setup;
  switch (status.phase) {
    case 'checking':
      $('starting-text').textContent = 'Starting VoiceCut…';
      show('starting');
      break;
    case 'starting':
      $('starting-text').textContent = 'Starting VoiceCut…';
      show('starting');
      break;
    case 'ready':
      $('starting-text').textContent = 'Opening the editor…';
      show('starting');
      break;
    case 'needs_setup': {
      $('download-size').textContent = `About ${gigabytes(setup.download_gb)}`;
      $('installed-size').textContent = `About ${gigabytes(setup.installed_gb)}`;
      $('data-dir').textContent = status.data_dir.replace(/^\/Users\/[^/]+/, '~');
      const short = setup.free_gb !== null && setup.free_gb < setup.needed_gb;
      $('space-warning').hidden = !short;
      $('space-warning').textContent = short
        ? `Only ${gigabytes(setup.free_gb)} is free; setup needs about ${gigabytes(setup.needed_gb)}.`
        : '';
      show('intro');
      break;
    }
    case 'installing': {
      if (!setup) break;
      $('installing-title').textContent = setup.light ? 'Updating VoiceCut' : 'Setting up VoiceCut';
      $('meter-fill').style.width = `${Math.round(setup.progress * 100)}%`;
      const minutes = Math.floor((Date.now() - started) / 60000);
      $('meter-text').textContent =
        `${Math.round(setup.progress * 100)}%` + (minutes > 0 ? ` · ${minutes} min` : '');
      renderSteps($('steps'), setup.steps);
      renderLog($('log'), setup.log);
      $('cancel').hidden = setup.light;
      show('installing');
      break;
    }
    case 'failed': {
      const duringSetup = status.during === 'setup';
      $('failed-title').textContent = duringSetup ? 'Setup did not finish' : 'VoiceCut’s engine stopped';
      $('failed-message').textContent = status.message;
      $('failed-hint').textContent = duringSetup
        ? 'Try again to continue where it stopped; finished parts are kept.'
        : 'Your projects are safe: everything is saved as you edit.';
      $('retry').textContent = duringSetup ? 'Try Again' : 'Restart VoiceCut';
      $('failed-steps').hidden = !(duringSetup && setup);
      if (duringSetup && setup) renderSteps($('failed-steps'), setup.steps);
      renderLog($('failed-log'), setup ? setup.log : []);
      show('failed');
      break;
    }
  }
}

$('install').addEventListener('click', () => {
  started = Date.now();
  void invoke('start_setup');
});
$('cancel').addEventListener('click', () => void invoke('cancel_setup'));
$('retry').addEventListener('click', () => {
  started = Date.now();
  void invoke('restart_engine');
});
$('logs').addEventListener('click', () => void invoke('reveal_logs'));

void listen('status', (event) => render(event.payload));
invoke('app_status').then(render);
