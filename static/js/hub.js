// Hub windows: separate local services (own process, own web UI) framed in a
// Mabel window. The service list and the URLs live server-side in
// src/hub_services.py; /api/hub/services resolves the right origin for how
// Mabel was reached (loopback or tailnet) and says whether the service is up.
//
// Adding a service: an entry in HUB_SERVICES, a #<id>-modal block in
// index.html (copy gremlin-modal), a sidebar row #tool-<id>-btn, and its id
// in WINDOWS below.

import { makeWindowDraggable } from './windowDrag.js';

const WINDOWS = ['gremlin'];

let _services = null;
async function services(force = false) {
  if (_services && !force) return _services;
  const r = await fetch('/api/hub/services', { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`hub services: HTTP ${r.status}`);
  _services = Object.fromEntries((await r.json()).services.map(s => [s.id, s]));
  return _services;
}

// ── Theme hand-over ──
// Services can't see Mabel's CSS (other origin), so Mabel sends its theme as
// a small generic object: { bg, fg, panel, border, accent, font }. First paint
// gets it on the frame URL (?theme=, base64url JSON), later changes go by
// postMessage { type: 'host-theme', theme } to the service's origin only.
// A service that doesn't know the contract simply ignores both.
function currentTheme() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  return {
    bg: v('--bg'),
    fg: v('--fg'),
    panel: v('--panel') || v('--bg'),
    border: v('--border'),
    accent: v('--accent') || v('--red'),
    font: v('--font-family'),
  };
}

function themeParam(theme) {
  const json = JSON.stringify(theme);
  const b64 = btoa(String.fromCharCode(...new TextEncoder().encode(json)));
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function withTheme(url) {
  const u = new URL(url);
  u.searchParams.set('theme', themeParam(currentTheme()));
  return u.toString();
}

function postTheme(modal) {
  const frame = modal?.querySelector('.hub-frame');
  const src = frame?.getAttribute('src');
  if (!frame?.contentWindow || !src) return;
  try {
    frame.contentWindow.postMessage({ type: 'host-theme', theme: currentTheme() }, new URL(src).origin);
  } catch { /* frame not ready / navigated away: next change or reload catches up */ }
}

let _themeTimer = null;
function watchTheme() {
  // Theme changes land as inline custom properties / classes on <html>.
  new MutationObserver(() => {
    clearTimeout(_themeTimer);
    _themeTimer = setTimeout(() => {
      for (const id of WINDOWS) postTheme(document.getElementById(`${id}-modal`));
    }, 120);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['style', 'class', 'data-theme'] });
}

const _dragWired = new Set();
function wireDrag(modal) {
  if (_dragWired.has(modal.id)) return;
  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.modal-header');
  if (!content || !header) return;
  _dragWired.add(modal.id);
  makeWindowDraggable(modal, {
    content,
    header,
    skipSelector: 'button, a, input, select, label',
    enableDock: true,
    enableLeftDock: true,
  });
}

function showOffline(modal, svc, message) {
  const frame = modal.querySelector('.hub-frame');
  const box = modal.querySelector('.hub-offline');
  frame.hidden = true;
  box.hidden = false;
  box.textContent = message || `${svc?.label || 'The service'} isn't running (nothing answers on its port). Start it, then press reload.`;
}

async function load(id, { force = false } = {}) {
  const modal = document.getElementById(`${id}-modal`);
  if (!modal) return;
  const frame = modal.querySelector('.hub-frame');
  const box = modal.querySelector('.hub-offline');
  let svc;
  try {
    svc = (await services(force))[id];
  } catch (err) {
    return showOffline(modal, null, `Couldn't ask Mabel where this lives: ${err.message}`);
  }
  if (!svc) return showOffline(modal, null, `No hub service called "${id}".`);
  const newTab = modal.querySelector('[data-hub-newtab]');
  if (newTab) newTab.href = svc.url;
  if (!svc.up) return showOffline(modal, svc);
  box.hidden = true;
  frame.hidden = false;
  // Keep the service's own state (open item, scroll) across close/reopen;
  // only (re)load when empty or asked to.
  if (force || !frame.getAttribute('src')) frame.src = withTheme(svc.url);
  // The theme may have changed while the window was closed.
  else postTheme(modal);
}

export function openHub(id) {
  const modal = document.getElementById(`${id}-modal`);
  if (!modal) return;
  modal.classList.remove('hidden', 'modal-minimized');
  modal.style.display = '';
  wireDrag(modal);
  load(id, { force: !modal.querySelector('.hub-frame')?.getAttribute('src') });
}

function closeHub(id) {
  document.getElementById(`${id}-modal`)?.classList.add('hidden');
}

function toggleHub(id) {
  const modal = document.getElementById(`${id}-modal`);
  if (!modal) return;
  if (modal.classList.contains('hidden') || modal.classList.contains('modal-minimized')) openHub(id);
  else closeHub(id);
}

function init() {
  watchTheme();
  for (const id of WINDOWS) {
    const modal = document.getElementById(`${id}-modal`);
    if (!modal) continue;
    document.getElementById(`tool-${id}-btn`)?.addEventListener('click', () => toggleHub(id));
    document.getElementById(`close-${id}-modal`)?.addEventListener('click', () => closeHub(id));
    modal.querySelector('[data-hub-reload]')?.addEventListener('click', () => {
      const frame = modal.querySelector('.hub-frame');
      frame?.removeAttribute('src');
      load(id, { force: true });
    });
    modal.addEventListener('click', (event) => {
      if (event.target === modal) closeHub(id);
    });
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { openHub };
