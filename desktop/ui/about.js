// About VoiceCut: the version, and links that open in the browser.
'use strict';

const { invoke } = window.__TAURI__.core;

invoke('app_status').then((status) => {
  document.getElementById('version').textContent = `Version ${status.version}`;
});

document.addEventListener('click', (event) => {
  const link = event.target.closest('a[href]');
  if (!link) return;
  event.preventDefault();
  void invoke('open_external', { url: link.href });
});
