// Per-conversation Active Directories selector (composer indicator).
import Storage, { KEYS } from './storage.js';
import uiModule from './ui.js?v=20260916largetoolscroll1';

const API = window.location.origin;

let _grants = [];
let _activeIds = [];
let _primaryLocalId = null;
let _modal = null;

function _isChatMode() {
  const b = document.getElementById('mode-chat-btn');
  return !!(b && b.classList.contains('active'));
}

export function getActiveDirectoryIds() {
  return [..._activeIds];
}

export function getPrimaryLocalDirectoryId() {
  return _primaryLocalId;
}

export function syncActiveDirectoriesIndicator() {
  const chat = _isChatMode();
  const pill = document.getElementById('workspace-indicator-btn');
  const name = document.getElementById('workspace-indicator-name');
  const overflow = document.getElementById('overflow-workspace-btn');
  const label = _activeIds.length
    ? `${_activeIds.length} dir${_activeIds.length === 1 ? '' : 's'}`
    : '';
  if (pill) {
    pill.style.display = (label && !chat) ? '' : 'none';
    pill.classList.toggle('active', !!label);
    pill.title = label
      ? `Active directories (${_activeIds.length}). Click to change. Primary shell: ${_primaryLocalId || 'none'}`
      : 'Active Directories';
  }
  if (name) name.textContent = label;
  if (overflow) {
    overflow.style.display = chat ? 'none' : '';
    overflow.classList.toggle('active', !!label);
    const span = overflow.querySelector('span');
    if (span) span.textContent = 'Active Directories';
  }
  try { document.dispatchEvent(new CustomEvent('overflow-state-change')); } catch (_) {}
}

export function applyMode() {
  syncActiveDirectoriesIndicator();
}

export async function loadGrants() {
  try {
    const res = await fetch(`${API}/api/allowed-directories`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const data = await res.json();
    _grants = (data.directories || []).filter((g) => g.enabled !== false);
  } catch (_) {
    _grants = [];
  }
}

export async function loadSessionScope(sessionId) {
  if (!sessionId) {
    _activeIds = [];
    _primaryLocalId = null;
    // One-time legacy workspace preselect only when a matching grant exists.
    const legacy = Storage.get(KEYS.WORKSPACE, '') || '';
    if (legacy) {
      await loadGrants();
      const match = _grants.find((g) => g.device_id === 'device:local' && g.path === legacy);
      if (match) {
        _activeIds = [match.id];
        if (match.permission === 'rw') _primaryLocalId = match.id;
      }
    }
    syncActiveDirectoriesIndicator();
    return;
  }
  try {
    const res = await fetch(`${API}/sessions`, { credentials: 'same-origin' });
    if (!res.ok) return;
    const sessions = await res.json();
    const sess = (sessions || []).find((s) => s.id === sessionId);
    if (sess) {
      _activeIds = Array.isArray(sess.active_directory_ids) ? [...sess.active_directory_ids] : [];
      _primaryLocalId = sess.primary_local_directory_id || null;
    }
  } catch (_) {}
  syncActiveDirectoriesIndicator();
}

export async function persistSessionScope(sessionId) {
  if (!sessionId) return;
  const fd = new FormData();
  fd.append('active_directories', JSON.stringify(_activeIds));
  fd.append('primary_local_directory_id', _primaryLocalId || '');
  await fetch(`${API}/session/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    credentials: 'same-origin',
    body: fd,
  });
}

function _closeModal() {
  if (_modal) {
    _modal.remove();
    _modal = null;
  }
}

export async function openActiveDirectoriesPicker() {
  await loadGrants();
  _closeModal();
  _modal = document.createElement('div');
  _modal.id = 'active-directories-modal';
  _modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:12000;display:flex;align-items:center;justify-content:center;';
  const panel = document.createElement('div');
  panel.style.cssText = 'background:var(--panel,#111);border:1px solid var(--border);padding:16px;max-width:520px;width:92%;max-height:80vh;overflow:auto;';
  panel.innerHTML = '<h3 style="margin:0 0 8px;">Active Directories</h3>'
    + '<p style="opacity:.75;font-size:12px;margin:0 0 12px;">Authorized directories are the permission scope and are optional to select. A preferred shell working directory is optional and is not required for tools.</p>';
  const list = document.createElement('div');
  if (!_grants.length) {
    list.textContent = 'No allowed directories configured. Open Settings → Devices to authorize folders.';
  }
  _grants.forEach((grant) => {
    const row = document.createElement('label');
    row.style.cssText = 'display:flex;gap:8px;align-items:flex-start;margin:6px 0;';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = _activeIds.includes(grant.id);
    cb.addEventListener('change', () => {
      if (cb.checked) {
        if (!_activeIds.includes(grant.id)) _activeIds.push(grant.id);
      } else {
        _activeIds = _activeIds.filter((id) => id !== grant.id);
        if (_primaryLocalId === grant.id) _primaryLocalId = null;
      }
      syncPrimaryRadios();
    });
    const text = document.createElement('span');
    text.innerHTML = `<strong>${grant.display_name}</strong><br><span style="opacity:.7;font-size:12px;">${grant.device_name || grant.device_id} · ${grant.permission} · ${grant.path}</span>`;
    row.appendChild(cb);
    row.appendChild(text);
    if (grant.device_id === 'device:local' && grant.permission === 'rw') {
      const primary = document.createElement('label');
      primary.style.cssText = 'display:block;margin-left:24px;font-size:12px;opacity:.85;';
      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'primary-local-dir';
      radio.value = grant.id;
      radio.checked = _primaryLocalId === grant.id;
      radio.dataset.grantId = grant.id;
      radio.addEventListener('change', () => {
        if (radio.checked) {
          _primaryLocalId = grant.id;
          if (!_activeIds.includes(grant.id)) _activeIds.push(grant.id);
        }
      });
      primary.appendChild(radio);
      primary.appendChild(document.createTextNode(' Preferred shell working directory (optional)'));
      row.appendChild(primary);
    }
    list.appendChild(row);
  });
  function syncPrimaryRadios() {
    panel.querySelectorAll('input[name="primary-local-dir"]').forEach((radio) => {
      radio.disabled = !_activeIds.includes(radio.value);
      if (radio.disabled && radio.checked) {
        radio.checked = false;
        _primaryLocalId = null;
      }
    });
  }
  panel.appendChild(list);
  const actions = document.createElement('div');
  actions.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;margin-top:12px;';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'confirm-btn-secondary';
  cancel.textContent = 'Cancel';
  cancel.onclick = _closeModal;
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'confirm-btn-primary';
  save.textContent = 'Save';
  save.onclick = async () => {
    const sid = Storage.get(KEYS.CURRENT_SESSION, '') || '';
    try {
      await persistSessionScope(sid);
      syncActiveDirectoriesIndicator();
      uiModule.showToast('Active directories updated');
      _closeModal();
    } catch (err) {
      alert(err.message || String(err));
    }
  };
  actions.appendChild(cancel);
  actions.appendChild(save);
  panel.appendChild(actions);
  _modal.appendChild(panel);
  _modal.addEventListener('click', (ev) => { if (ev.target === _modal) _closeModal(); });
  document.body.appendChild(_modal);
  syncPrimaryRadios();
}

export function appendActiveDirectoriesToForm(fd) {
  fd.append('active_directories', JSON.stringify(_activeIds));
  if (_primaryLocalId) fd.append('primary_local_directory_id', _primaryLocalId);
}

export function initActiveDirectories() {
  const pill = document.getElementById('workspace-indicator-btn');
  const overflow = document.getElementById('overflow-workspace-btn');
  const open = (ev) => {
    if (ev && ev.target && ev.target.closest && ev.target.closest('.tool-indicator-x')) {
      _activeIds = [];
      _primaryLocalId = null;
      const sid = Storage.get(KEYS.CURRENT_SESSION, '') || '';
      persistSessionScope(sid).finally(syncActiveDirectoriesIndicator);
      return;
    }
    openActiveDirectoriesPicker();
  };
  if (pill) pill.addEventListener('click', open);
  if (overflow) overflow.addEventListener('click', open);
  const sid = Storage.get(KEYS.CURRENT_SESSION, '') || '';
  loadSessionScope(sid);
  document.addEventListener('session-changed', (ev) => {
    loadSessionScope(ev.detail && ev.detail.sessionId);
  });
}

export default {
  initActiveDirectories,
  getActiveDirectoryIds,
  getPrimaryLocalDirectoryId,
  appendActiveDirectoriesToForm,
  openActiveDirectoriesPicker,
  loadSessionScope,
  applyMode,
  syncActiveDirectoriesIndicator,
};
