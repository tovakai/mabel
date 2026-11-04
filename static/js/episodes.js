// Memory sidebar → Episodes tab (Phase 00.5 episodic memory).
//
// Pending candidates are saved conversation turns that are NOT yet memories.
// "Keep" promotes one into Mabel's vault (memory/episodes/) so later chats can
// recall it; "Later" defers it; "Discard" rejects it. Kept episodes are listed
// below. The capture / recall switches are global settings (admin only).

import uiModule from './ui.js?v=20260916largetoolscroll1';

const esc = uiModule.esc;
const toast = (msg) => uiModule.showToast(msg);
const API = `${window.location.origin}/api/memory/episodic`;
let busy = false;

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function excerpt(text, max = 280) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function card(item, actions) {
  const buttons = actions.map(([action, label, cls]) =>
    `<button type="button" class="${cls}" data-episode-action="${action}" data-episode-id="${esc(item.id)}">${label}</button>`,
  ).join('');
  return `
    <div class="memory-item episode-item" data-episode-id="${esc(item.id)}" style="flex-direction:column;max-height:none;gap:6px">
      <div class="admin-toggle-sub" style="margin:0;opacity:0.6">${esc(when(item.created_at))}</div>
      <div style="font-size:12px"><strong>You said:</strong> ${esc(excerpt(item.user_excerpt))}</div>
      <div style="font-size:12px"><strong>Mabel replied:</strong> ${esc(excerpt(item.assistant_excerpt))}</div>
      ${buttons ? `<div style="display:flex;gap:6px;justify-content:flex-end">${buttons}</div>` : ''}
    </div>`;
}

function section(title, hint, items, actions, empty) {
  const body = items.length
    ? items.map((item) => card(item, actions)).join('')
    : `<div class="admin-toggle-sub" style="opacity:0.6">${empty}</div>`;
  return `
    <div class="admin-card">
      <h2 style="margin:0 0 4px">${title} <span class="memory-count" style="font-size:0.8em;opacity:0.6;font-weight:normal">${items.length}</span></h2>
      <span class="admin-toggle-sub" style="display:block;margin-bottom:8px;opacity:0.6">${hint}</span>
      <div style="display:flex;flex-direction:column;gap:6px">${body}</div>
    </div>`;
}

function switchRow(id, title, hint, checked, disabled) {
  return `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:32px">
      <h2 style="margin:0">${title}</h2>
      <label class="admin-switch" style="flex-shrink:0"${disabled ? ' title="Only an admin can change this"' : ''}>
        <input type="checkbox" id="${id}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''} /><span class="admin-slider"></span>
      </label>
    </div>
    <span class="admin-toggle-sub" style="display:block;margin:2px 0 8px;opacity:0.6">${hint}</span>`;
}

function render(root, data) {
  const locked = !data.can_manage_settings;
  root.innerHTML = `
    <div class="admin-card">
      ${switchRow('episodic-capture-toggle', 'Save conversation turns',
        'Keep finished turns as pending candidates for review. No model calls; nothing is remembered until you keep it.',
        data.capture_enabled, locked)}
      ${switchRow('episodic-recall-toggle', 'Recall kept episodes',
        'Let Mabel see up to three relevant kept episodes from other chats, as quoted excerpts.',
        data.recall_enabled, locked)}
    </div>
    ${section('Pending', 'Turns waiting for review. Keep the ones worth remembering.',
      data.pending, [['keep', 'Keep', 'admin-btn-add'], ['later', 'Later', 'admin-btn-sm'], ['discard', 'Discard', 'admin-btn-delete']],
      data.capture_enabled ? 'Nothing pending. New chat turns will appear here.' : 'Saving is off.')}
    ${data.deferred.length ? section('Later', 'Set aside for another look.',
      data.deferred, [['keep', 'Keep', 'admin-btn-add'], ['discard', 'Discard', 'admin-btn-delete']], '') : ''}
    ${section('Kept episodes', 'Stored in memory/episodes/ in Mabel’s vault. Deleting the original chat forgets them.',
      data.episodes, [], 'No kept episodes yet.')}`;
}

async function request(path, options = {}) {
  const res = await fetch(`${API}${path}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  let body = {};
  try { body = await res.json(); } catch { body = {}; }
  if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `Request failed (${res.status})`);
  return body;
}

export async function loadEpisodes() {
  const root = document.getElementById('episodes-panel-body');
  if (!root) return;
  try {
    render(root, await request(''));
  } catch (error) {
    root.innerHTML = `<div class="admin-card"><span class="admin-toggle-sub">${esc(error.message)}</span></div>`;
  }
}

function wire() {
  const root = document.getElementById('episodes-panel-body');
  if (!root || root.dataset.wired) return;
  root.dataset.wired = '1';
  root.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-episode-action]');
    if (!button || busy) return;
    const { episodeAction: action, episodeId: id } = button.dataset;
    if (action === 'discard' && !await uiModule.styledConfirm('Discard this turn? It will not be remembered.',
      { confirmText: 'Discard', danger: true })) return;
    busy = true;
    button.disabled = true;
    try {
      await request(`/${encodeURIComponent(id)}/${action}`, { method: 'POST' });
      toast(action === 'keep' ? 'Kept — Mabel can recall this now' : action === 'later' ? 'Saved for later' : 'Discarded');
      await loadEpisodes();
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    } finally {
      busy = false;
    }
  });
  root.addEventListener('change', async (event) => {
    const input = event.target;
    if (!['episodic-capture-toggle', 'episodic-recall-toggle'].includes(input.id)) return;
    const key = input.id === 'episodic-capture-toggle' ? 'capture_enabled' : 'recall_enabled';
    try {
      await request('/settings', { method: 'PUT', body: JSON.stringify({ [key]: input.checked }) });
      toast(`${key === 'capture_enabled' ? 'Saving turns' : 'Episode recall'} ${input.checked ? 'on' : 'off'}`);
      await loadEpisodes();
    } catch (error) {
      input.checked = !input.checked;
      toast(error.message);
    }
  });
}

export function openEpisodesTab() {
  wire();
  return loadEpisodes();
}

export default { loadEpisodes, openEpisodesTab };
